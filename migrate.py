"""Upgrade an existing POS database to support user accounts and shifts.

Safe to run repeatedly. It:
  1. creates the new `users` and `shifts` tables (if missing),
  2. adds `user_id` / `shift_id` columns to the existing `sales` table,
  3. creates a default admin account if no users exist yet.

Run:  python migrate.py
"""
import sys

# Force UTF-8 so the ✓/✗ status lines don't crash on a legacy cp1252 console.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import sqlalchemy
from sqlalchemy import text

from config import Config
from app import app
from models import db
from init_db import ensure_default_admin, DEFAULT_ADMIN


# MariaDB supports "ADD COLUMN IF NOT EXISTS", so these are idempotent.
SALES_COLUMNS = [
    "ADD COLUMN IF NOT EXISTS user_id INT NULL",
    "ADD COLUMN IF NOT EXISTS shift_id INT NULL",
    # Multi-currency: sale stays in base currency; these record the tender.
    "ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT ''",
    "ADD COLUMN IF NOT EXISTS exchange_rate DECIMAL(18,8) NOT NULL DEFAULT 1",
    "ADD COLUMN IF NOT EXISTS tendered_foreign DECIMAL(14,2) NOT NULL DEFAULT 0",
    "ADD COLUMN IF NOT EXISTS change_foreign DECIMAL(14,2) NOT NULL DEFAULT 0",
    # Non-cash tenders: M-Pesa receipt code or bank/transfer reference.
    "ADD COLUMN IF NOT EXISTS payment_ref VARCHAR(64) NOT NULL DEFAULT ''",
    # Loyalty: the buyer (if any) and points awarded by this sale.
    "ADD COLUMN IF NOT EXISTS customer_id INT NULL",
    "ADD COLUMN IF NOT EXISTS points_earned INT NOT NULL DEFAULT 0",
]
SALES_INDEXES = [
    "ADD INDEX IF NOT EXISTS ix_sales_user_id (user_id)",
    "ADD INDEX IF NOT EXISTS ix_sales_shift_id (shift_id)",
    "ADD INDEX IF NOT EXISTS ix_sales_customer_id (customer_id)",
]


def upgrade_sales_table():
    engine = sqlalchemy.create_engine(Config.SQLALCHEMY_DATABASE_URI)
    with engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE sales {', '.join(SALES_COLUMNS)}"))
        for idx in SALES_INDEXES:
            try:
                conn.execute(text(f"ALTER TABLE sales {idx}"))
            except Exception:
                pass  # index already present on older MariaDB without IF NOT EXISTS
        conn.commit()
    engine.dispose()
    print("✓ sales table has user_id / shift_id / loyalty columns.")


def upgrade_users_table():
    engine = sqlalchemy.create_engine(Config.SQLALCHEMY_DATABASE_URI)
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE users "
            "ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS assigned_shift VARCHAR(64) NULL"
        ))
        conn.commit()
    engine.dispose()
    print("✓ users table has must_change_password / assigned_shift columns.")


def upgrade_products_table():
    engine = sqlalchemy.create_engine(Config.SQLALCHEMY_DATABASE_URI)
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE products "
            "ADD COLUMN IF NOT EXISTS image VARCHAR(255) NULL"
        ))
        conn.commit()
    engine.dispose()
    print("✓ products table has image column.")


def create_new_tables():
    with app.app_context():
        # Creates any missing tables (users, shifts, exchange_rates, settings,
        # mpesa_transactions, …); existing tables are left untouched.
        db.create_all()
        # Make sure the base currency exists with a rate of 1.
        import rates as fx
        fx.cached_rates()
    print("✓ users, shifts, exchange_rates, settings and mpesa_transactions tables are ready.")


if __name__ == "__main__":
    try:
        create_new_tables()
        upgrade_sales_table()
        upgrade_users_table()
        upgrade_products_table()
        if ensure_default_admin():
            print(f"✓ Created default admin '{DEFAULT_ADMIN['username']}' "
                  f"(password '{DEFAULT_ADMIN['password']}'). Change it after first login!")
        print("\nDone. Restart the app and sign in.")
    except Exception as e:
        print("\n✗ Migration failed.")
        print(f"  {type(e).__name__}: {e}")
        raise SystemExit(1)
