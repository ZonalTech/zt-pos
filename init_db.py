"""Create the MariaDB database (if missing) and all tables.

Run once before first launch:  python init_db.py
"""
import sys

# The status lines below use ✓/✗; force UTF-8 so they don't crash on a
# Windows console using the legacy cp1252 codepage.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import sqlalchemy
from sqlalchemy import text

from config import Config, server_uri
from app import app
from models import db, User


# Default credentials for the first admin. CHANGE THE PASSWORD after first login
# (Users page). Only created when the users table is empty.
DEFAULT_ADMIN = {"username": "admin", "name": "Administrator", "password": "admin"}


def ensure_default_admin():
    """Create a starter admin account if there are no users yet."""
    with app.app_context():
        if User.query.count() > 0:
            return False
        admin = User(
            username=DEFAULT_ADMIN["username"],
            name=DEFAULT_ADMIN["name"],
            role="admin",
            must_change_password=True,  # prompt to set a real password at first login
        )
        admin.set_password(DEFAULT_ADMIN["password"])
        db.session.add(admin)
        db.session.commit()
        return True


def create_database():
    """Connect to the MariaDB server (no DB selected) and CREATE DATABASE."""
    engine = sqlalchemy.create_engine(server_uri())
    with engine.connect() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{Config.DB_NAME}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
        conn.commit()
    engine.dispose()
    print(f"✓ Database '{Config.DB_NAME}' is ready.")


def create_tables():
    with app.app_context():
        db.create_all()
    print("✓ Tables created.")


if __name__ == "__main__":
    try:
        create_database()
        create_tables()
        if ensure_default_admin():
            print(f"✓ Created default admin '{DEFAULT_ADMIN['username']}' "
                  f"(password '{DEFAULT_ADMIN['password']}'). Change it after first login!")
        print("\nDone. Next: (optional) python seed.py   then   python app.py")
    except Exception as e:
        print("\n✗ Could not initialize the database.")
        print(f"  {type(e).__name__}: {e}")
        print("\nChecklist:")
        print("  • Is MariaDB installed and running? (services.msc → MariaDB)")
        print(f"  • Do the DB_USER/DB_PASSWORD in your .env match? (host {Config.DB_HOST}:{Config.DB_PORT})")
        raise SystemExit(1)
