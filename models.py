"""Database models for the POS system (MariaDB via SQLAlchemy).

Timestamps (created_at / updated_at / shift times / sale times) use
``datetime.now()`` — the machine's local system clock — so receipts, shift
reports and the Currencies "Updated" column match the wall-clock time the
operator sees. This is a single-site local POS, so storing naive local time is
intentional (not UTC).
"""
from datetime import datetime
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# Work-schedule shifts an admin can assign to a user. The chosen label is
# stored verbatim on ``User.assigned_shift`` and copied onto the till session's
# ``note`` when the cashier opens their shift, so receipts/Z-reports keep it.
SHIFT_TYPES = [
    "Morning Shift (08:00–16:00)",
    "Afternoon Shift (16:00–00:00)",
    "Night Shift (00:00–08:00)",
]


class User(db.Model):
    """A staff account that logs in to run the POS."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)          # display name on receipts
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="cashier")  # admin | cashier
    is_active = db.Column(db.Boolean, default=True)
    # Force a password change on next login (set for the default admin account).
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    # The work shift an admin assigned to this user (one of SHIFT_TYPES); NULL
    # until assigned. Pre-fills the till session note so the cashier no longer
    # picks a shift each time they open one.
    assigned_shift = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self):
        return self.role == "admin"

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "role": self.role,
            "is_active": self.is_active,
            "assigned_shift": self.assigned_shift or "",
        }


class Shift(db.Model):
    """A cashier's till session. Sales attach to the open shift."""
    __tablename__ = "shifts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    opened_at = db.Column(db.DateTime, default=datetime.now, index=True)
    closed_at = db.Column(db.DateTime)                        # NULL while the shift is open
    opening_float = db.Column(db.Numeric(12, 2), nullable=False, default=0)  # cash in drawer at start
    closing_cash = db.Column(db.Numeric(12, 2))              # counted cash at close (cash-up)
    note = db.Column(db.String(255))

    user = db.relationship("User")

    @property
    def is_open(self):
        return self.closed_at is None

    def summary(self):
        """Z-report figures for this shift."""
        sales = list(self.sales)
        by_method = {}
        total = Decimal("0")
        for s in sales:
            by_method[s.payment_method] = by_method.get(s.payment_method, Decimal("0")) + s.total
            total += s.total
        cash_sales = by_method.get("cash", Decimal("0"))
        opening = self.opening_float or Decimal("0")
        expected_cash = opening + cash_sales
        over_short = None
        if self.closing_cash is not None:
            over_short = self.closing_cash - expected_cash
        return {
            "count": len(sales),
            "total": total,
            "by_method": by_method,
            "cash_sales": cash_sales,
            "opening_float": opening,
            "expected_cash": expected_cash,
            "closing_cash": self.closing_cash,
            "over_short": over_short,
        }

    def to_dict(self):
        return {
            "id": self.id,
            "user": self.user.name if self.user else "",
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "is_open": self.is_open,
        }


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(512))
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)        # selling price
    cost_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)   # buying price
    quantity = db.Column(db.Integer, nullable=False, default=0)            # stock on hand
    reorder_level = db.Column(db.Integer, nullable=False, default=5)
    image = db.Column(db.String(255))                                      # uploaded photo filename
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def low_stock(self):
        return self.quantity <= self.reorder_level

    def to_dict(self):
        return {
            "id": self.id,
            "barcode": self.barcode,
            "name": self.name,
            "description": self.description,
            "price": float(self.price),
            "cost_price": float(self.cost_price),
            "quantity": self.quantity,
            "reorder_level": self.reorder_level,
            "low_stock": self.low_stock,
            "image": self.image or None,
            # Convenience URL for the client; None when there's no photo.
            "image_url": (f"/uploads/products/{self.image}" if self.image else None),
        }


class Customer(db.Model):
    """A loyalty customer, identified by phone number. Earns points on sales."""
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    # Phone number is the customer's identity — required and unique.
    phone = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, default="")
    points = db.Column(db.Integer, nullable=False, default=0)  # current balance
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "phone": self.phone,
            "name": self.name,
            "points": self.points,
            "is_active": self.is_active,
        }


class Sale(db.Model):
    __tablename__ = "sales"

    id = db.Column(db.Integer, primary_key=True)
    sale_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    tax = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    payment_method = db.Column(db.String(20), nullable=False, default="cash")  # cash | mpesa | bank
    amount_tendered = db.Column(db.Numeric(12, 2), nullable=False, default=0)  # in base currency
    change_due = db.Column(db.Numeric(12, 2), nullable=False, default=0)       # in base currency
    # For non-cash tenders: the M-Pesa receipt code or the bank/transfer reference.
    payment_ref = db.Column(db.String(64), nullable=False, default="")

    # --- Multi-currency ---
    # Money above is always stored in the store's base currency so reports and
    # Z-reports stay single-currency. These fields record the currency the
    # customer actually paid in (when it differs from base) for the receipt.
    currency = db.Column(db.String(3), nullable=False, default="")  # tender currency; "" => base
    exchange_rate = db.Column(db.Numeric(18, 8), nullable=False, default=1)  # foreign units per 1 base
    tendered_foreign = db.Column(db.Numeric(14, 2), nullable=False, default=0)  # handed over, in `currency`
    change_foreign = db.Column(db.Numeric(14, 2), nullable=False, default=0)    # change given, in `currency`

    cashier = db.Column(db.String(80), default="cashier")  # display name snapshot (for receipts)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)   # who rang it up
    shift_id = db.Column(db.Integer, db.ForeignKey("shifts.id"), index=True)  # which till session

    # --- Loyalty ---
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)  # buyer, if attached
    points_earned = db.Column(db.Integer, nullable=False, default=0)  # points this sale awarded

    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

    items = db.relationship(
        "SaleItem", backref="sale", cascade="all, delete-orphan", lazy="joined"
    )
    user = db.relationship("User")
    shift = db.relationship("Shift", backref="sales")
    customer = db.relationship("Customer")

    def to_dict(self):
        return {
            "id": self.id,
            "sale_number": self.sale_number,
            "subtotal": float(self.subtotal),
            "tax": float(self.tax),
            "total": float(self.total),
            "payment_method": self.payment_method,
            "amount_tendered": float(self.amount_tendered),
            "change_due": float(self.change_due),
            "payment_ref": self.payment_ref or "",
            "currency": self.currency or "",
            "exchange_rate": float(self.exchange_rate),
            "tendered_foreign": float(self.tendered_foreign),
            "change_foreign": float(self.change_foreign),
            "cashier": self.cashier,
            "user_id": self.user_id,
            "shift_id": self.shift_id,
            "customer_id": self.customer_id,
            "customer_name": self.customer.name if self.customer else "",
            "customer_phone": self.customer.phone if self.customer else "",
            "points_earned": self.points_earned,
            "customer_points": self.customer.points if self.customer else None,
            "created_at": self.created_at.isoformat(),
            "items": [i.to_dict() for i in self.items],
        }


class SaleItem(db.Model):
    __tablename__ = "sale_items"

    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    barcode = db.Column(db.String(64))
    name = db.Column(db.String(255), nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    line_total = db.Column(db.Numeric(12, 2), nullable=False)

    def to_dict(self):
        return {
            "name": self.name,
            "barcode": self.barcode,
            "unit_price": float(self.unit_price),
            "quantity": self.quantity,
            "line_total": float(self.line_total),
        }


class StockMovement(db.Model):
    """Audit trail of every change to stock-on-hand."""
    __tablename__ = "stock_movements"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    change_qty = db.Column(db.Integer, nullable=False)   # +received, -sold/adjusted down
    movement_type = db.Column(db.String(20), nullable=False)  # receive, sale, adjust
    reference = db.Column(db.String(64))                  # e.g. sale_number
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

    product = db.relationship("Product")

    def to_dict(self):
        return {
            "id": self.id,
            "product": self.product.name if self.product else "",
            "barcode": self.product.barcode if self.product else "",
            "change_qty": self.change_qty,
            "movement_type": self.movement_type,
            "reference": self.reference,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
        }


class ExchangeRate(db.Model):
    """Cached FX rate for one currency, stored as *foreign-per-base*: how many
    units of `currency` equal one unit of the store's base currency.

    e.g. base = KES, currency = USD, rate = 0.0077  →  1 KES = 0.0077 USD.
    The base currency itself always has a rate of exactly 1.
    """
    __tablename__ = "exchange_rates"

    id = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.String(3), unique=True, nullable=False, index=True)
    rate = db.Column(db.Numeric(18, 8), nullable=False, default=1)
    source = db.Column(db.String(10), nullable=False, default="live")  # live | manual
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            "currency": self.currency,
            "rate": float(self.rate),
            "source": self.source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Setting(db.Model):
    """A simple key/value store for admin-managed configuration that doesn't
    belong in .env — e.g. payment-gateway credentials the admin edits at
    runtime from the Payment Settings page. Values are stored as text; helpers
    in payments.py handle typing (bool/int) per key.
    """
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class MpesaTransaction(db.Model):
    """One Lipa na M-Pesa Online (STK Push) attempt.

    Created when the cashier sends a prompt to the customer's phone. Its status
    advances from `pending` to `success`/`failed` either via the Daraja result
    callback or by polling the STK Push Query API. A successful row is linked to
    the Sale it paid for once the sale is finalized.
    """
    __tablename__ = "mpesa_transactions"

    id = db.Column(db.Integer, primary_key=True)
    checkout_request_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    merchant_request_id = db.Column(db.String(64))
    phone = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)   # base currency (KES)
    status = db.Column(db.String(10), nullable=False, default="pending")  # pending|success|failed
    result_code = db.Column(db.String(10))
    result_desc = db.Column(db.String(255))
    mpesa_receipt = db.Column(db.String(32))   # confirmation code, e.g. "QGR7…"
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), index=True)  # set once consumed
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            "checkout_request_id": self.checkout_request_id,
            "phone": self.phone,
            "amount": float(self.amount),
            "status": self.status,
            "result_desc": self.result_desc,
            "mpesa_receipt": self.mpesa_receipt,
        }
