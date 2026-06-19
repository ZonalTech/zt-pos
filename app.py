"""POS — Flask application.

Local point-of-sale system: barcode scanning, stock tracking, payments
(cash / M-Pesa STK Push / bank transfer), and sales reporting. Backed by a
local MariaDB database.

Run:  python app.py   (then open http://127.0.0.1:5000)
"""
import os
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, flash, abort,
    session, g, send_from_directory
)
from werkzeug.utils import secure_filename
from sqlalchemy import func, create_engine, text
from sqlalchemy.exc import OperationalError, InterfaceError

from config import Config, resource_path, app_dir
from models import (
    db, Product, Sale, SaleItem, StockMovement, User, Shift, ExchangeRate,
    Setting, MpesaTransaction, Customer, SHIFT_TYPES,
)
import rates as fx
import payments as pay
import updates


def create_app():
    # resource_path makes templates/ and static/ resolve correctly both from
    # source and from inside a PyInstaller bundle.
    app = Flask(
        __name__,
        template_folder=resource_path("templates"),
        static_folder=resource_path("static"),
    )
    app.config.from_object(Config)
    db.init_app(app)

    # Resolve the logged-in user (and their open shift) once per request.
    @app.before_request
    def load_current_user():
        g.user = None
        g.shift = None
        uid = session.get("user_id")
        if uid:
            # Never let a DB outage crash request handling — that would stop the
            # db-error/troubleshoot pages from rendering. Degrade to "logged out".
            try:
                g.user = User.query.get(uid)
                if g.user and not g.user.is_active:
                    session.clear()
                    g.user = None
                if g.user:
                    g.shift = current_open_shift(g.user)
            except (OperationalError, InterfaceError):
                g.user = None
                g.shift = None

    # Make store settings + auth state available in every template.
    @app.context_processor
    def inject_settings():
        return dict(
            STORE_NAME=app.config["STORE_NAME"],
            CURRENCY=app.config["CURRENCY"],
            BASE_CURRENCY=app.config["BASE_CURRENCY"],
            CURRENCIES=fx.supported_currencies(),
            TAX_RATE=app.config["TAX_RATE"],
            PAYMENT_METHODS=pay.enabled_methods(),
            LOYALTY_KES_PER_POINT=app.config["LOYALTY_KES_PER_POINT"],
            APP_VERSION=updates.installed_version(),
            current_user=g.get("user"),
            current_shift=g.get("shift"),
        )

    register_routes(app)
    return app


# ------------------------------- Auth helpers ----------------------------
def current_open_shift(user):
    """The user's currently-open shift, if any."""
    if not user:
        return None
    return (
        Shift.query.filter_by(user_id=user.id, closed_at=None)
        .order_by(Shift.opened_at.desc())
        .first()
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not signed in"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = g.get("user")
        if not user:
            return redirect(url_for("login", next=request.path))
        if not user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _dec(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _next_sale_number():
    """SALE-YYYYMMDD-#### sequential per day."""
    today = date.today()
    prefix = f"SALE-{today:%Y%m%d}-"
    count = Sale.query.filter(Sale.sale_number.like(prefix + "%")).count()
    return f"{prefix}{count + 1:04d}"


def _loyalty_points(total):
    """Whole loyalty points earned on a base-currency `total`.

    1 point per LOYALTY_KES_PER_POINT spent, rounded down. A zero/negative
    rate disables accrual.
    """
    per = _dec(Config.LOYALTY_KES_PER_POINT)
    if per <= 0:
        return 0
    return int((_dec(total) / per).to_integral_value(rounding=ROUND_DOWN))


def _normalize_phone(raw):
    """Trim a typed phone number to a storable form (keep leading +, digits)."""
    raw = (raw or "").strip()
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    return cleaned[:32]


def _allowed_image(filename):
    """True if `filename` has a permitted image extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in Config.ALLOWED_IMAGE_EXTENSIONS


def _save_product_image(file, product):
    """Persist an uploaded product photo and return its stored filename.

    Saves under Config.UPLOAD_DIR with a collision-proof name derived from the
    product id. Raises ValueError on a disallowed type. Returns None if no file
    was provided.
    """
    if not file or not file.filename:
        return None
    if not _allowed_image(file.filename):
        raise ValueError("Image must be a PNG, JPG, GIF or WEBP file.")
    ext = file.filename.rsplit(".", 1)[-1].lower()
    os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
    # Name by product id + a short timestamp so a re-upload busts the browser
    # cache and never clashes with the previous file.
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    fname = secure_filename(f"product_{product.id}_{stamp}.{ext}")
    file.save(os.path.join(Config.UPLOAD_DIR, fname))
    return fname


def _delete_product_image(filename):
    """Best-effort removal of a stored product photo (ignore if missing)."""
    if not filename:
        return
    try:
        os.remove(os.path.join(Config.UPLOAD_DIR, filename))
    except OSError:
        pass


class PricingError(Exception):
    """Cart validation/pricing failure carrying a staff-facing message."""


def _price_cart(cart):
    """Validate cart lines against current stock and price them in base currency.

    Returns (resolved, subtotal, tax, total) where `resolved` is a list of
    (product, qty, line_total). Raises PricingError(message) on any problem.
    Shared by the STK Push initiator and final checkout so both agree on the
    amount the customer is charged.
    """
    if not cart:
        raise PricingError("Cart is empty")
    tax_rate = _dec(Config.TAX_RATE)
    resolved = []
    subtotal = Decimal("0")
    for line in cart:
        product = Product.query.get(line.get("id"))
        if not product or not product.is_active:
            raise PricingError(f"Product not found: {line.get('name')}")
        qty = int(line.get("quantity", 0))
        if qty <= 0:
            raise PricingError(f"Invalid quantity for {product.name}")
        if qty > product.quantity:
            raise PricingError(
                f"Not enough stock for {product.name} "
                f"(have {product.quantity}, need {qty})"
            )
        line_total = product.price * qty
        subtotal += line_total
        resolved.append((product, qty, line_total))
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
    total = subtotal + tax
    return resolved, subtotal, tax, total


def register_routes(app):

    # ------------------------------- Auth --------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.get("user"):
            return redirect(url_for("home"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username, is_active=True).first()
            if not user or not user.check_password(password):
                flash("Invalid username or password.", "error")
                return render_template("login.html", username=username), 401
            session.clear()
            session["user_id"] = user.id
            nxt = request.args.get("next")
            # Only allow same-site relative redirects.
            if nxt and nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("home"))
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        flash("Signed out.", "success")
        return redirect(url_for("login"))

    # While a user is flagged to change their password, send every page (except
    # the change form and signing out) to the change-password screen.
    @app.before_request
    def enforce_password_change():
        user = g.get("user")
        if not user or not user.must_change_password:
            return
        if request.endpoint in ("change_password", "logout", "static"):
            return
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Password change required"}), 403
        return redirect(url_for("change_password"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if not g.user.check_password(current):
                flash("Current password is incorrect.", "error")
                return render_template("change_password.html"), 400
            if len(new) < 8:
                flash("New password must be at least 8 characters.", "error")
                return render_template("change_password.html"), 400
            if new != confirm:
                flash("New passwords do not match.", "error")
                return render_template("change_password.html"), 400
            if new == current:
                flash("New password must be different from the current one.", "error")
                return render_template("change_password.html"), 400
            g.user.set_password(new)
            g.user.must_change_password = False
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("home"))
        return render_template("change_password.html")

    @app.route("/home")
    @login_required
    def home():
        """Send admins to the dashboard, cashiers straight to the POS."""
        return redirect(url_for("dashboard") if g.user.is_admin else url_for("pos"))

    # ------------------------------- Shifts ------------------------------
    @app.route("/shift")
    @login_required
    def shift_page():
        open_shift = g.shift
        recent = (
            Shift.query.filter_by(user_id=g.user.id)
            .order_by(Shift.opened_at.desc())
            .limit(10)
            .all()
        )
        return render_template(
            "shift.html",
            open_shift=open_shift,
            summary=open_shift.summary() if open_shift else None,
            recent=recent,
        )

    @app.route("/shift/open", methods=["POST"])
    @login_required
    def shift_open():
        if g.shift:
            flash("You already have an open shift.", "error")
            return redirect(url_for("shift_page"))
        shift = Shift(
            user_id=g.user.id,
            opening_float=_dec(request.form.get("opening_float", 0)),
            # The shift schedule is assigned by an admin on the user page, so
            # the cashier no longer picks it here — copy their assignment.
            note=(g.user.assigned_shift or None),
        )
        db.session.add(shift)
        db.session.commit()
        flash("Shift opened.", "success")
        return redirect(url_for("pos"))

    @app.route("/shift/close", methods=["POST"])
    @login_required
    def shift_close():
        if not g.shift:
            flash("No open shift to close.", "error")
            return redirect(url_for("shift_page"))
        shift = g.shift
        raw_cash = request.form.get("closing_cash")
        shift.closing_cash = _dec(raw_cash) if raw_cash not in (None, "") else None
        shift.closed_at = datetime.now()  # local system clock
        db.session.commit()
        flash("Shift closed.", "success")
        return redirect(url_for("shift_summary", shift_id=shift.id))

    @app.route("/shift/<int:shift_id>")
    @login_required
    def shift_summary(shift_id):
        shift = Shift.query.get_or_404(shift_id)
        # Owners see their own shift; admins see anyone's.
        if shift.user_id != g.user.id and not g.user.is_admin:
            abort(403)
        return render_template(
            "shift_summary.html", shift=shift, summary=shift.summary()
        )

    # ------------------------------- Users -------------------------------
    @app.route("/users")
    @admin_required
    def users():
        people = User.query.order_by(User.is_active.desc(), User.name).all()
        return render_template("users.html", users=people, shift_types=SHIFT_TYPES)

    @app.route("/users/save", methods=["POST"])
    @admin_required
    def user_save():
        uid = request.form.get("id")
        username = request.form.get("username", "").strip()
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "cashier")
        password = request.form.get("password", "")
        assigned_shift = request.form.get("assigned_shift", "").strip()
        if role not in ("admin", "cashier"):
            role = "cashier"
        # Only accept a known shift label; blank clears any assignment.
        if assigned_shift not in SHIFT_TYPES:
            assigned_shift = None
        if not username or not name:
            flash("Username and name are required.", "error")
            return redirect(url_for("users"))

        if uid:
            user = User.query.get_or_404(int(uid))
            clash = User.query.filter(User.username == username, User.id != user.id).first()
            if clash:
                flash(f"Username '{username}' is already taken.", "error")
                return redirect(url_for("users"))
            user.username = username
            user.name = name
            user.role = role
            user.assigned_shift = assigned_shift
            if password:
                user.set_password(password)
        else:
            if User.query.filter_by(username=username).first():
                flash(f"Username '{username}' is already taken.", "error")
                return redirect(url_for("users"))
            if not password:
                flash("A password is required for a new user.", "error")
                return redirect(url_for("users"))
            user = User(username=username, name=name, role=role,
                        assigned_shift=assigned_shift)
            user.set_password(password)
            db.session.add(user)

        db.session.commit()
        flash("User saved.", "success")
        return redirect(url_for("users"))

    @app.route("/users/deactivate/<int:uid>", methods=["POST"])
    @admin_required
    def user_deactivate(uid):
        user = User.query.get_or_404(uid)
        if user.id == g.user.id:
            flash("You can't deactivate your own account.", "error")
            return redirect(url_for("users"))
        if user.is_admin and User.query.filter_by(role="admin", is_active=True).count() <= 1:
            flash("Can't deactivate the last active admin.", "error")
            return redirect(url_for("users"))
        user.is_active = not user.is_active
        db.session.commit()
        flash(f"{user.name} {'reactivated' if user.is_active else 'deactivated'}.", "success")
        return redirect(url_for("users"))

    # ----------------------------- Dashboard -----------------------------
    @app.route("/")
    @admin_required
    def dashboard():
        today = date.today()
        start = datetime.combine(today, datetime.min.time())

        sales_today = Sale.query.filter(Sale.created_at >= start).all()
        revenue_today = sum((s.total for s in sales_today), Decimal("0"))
        items_sold_today = (
            db.session.query(func.coalesce(func.sum(SaleItem.quantity), 0))
            .join(Sale)
            .filter(Sale.created_at >= start)
            .scalar()
        )

        total_products = Product.query.filter_by(is_active=True).count()
        stock_value = db.session.query(
            func.coalesce(func.sum(Product.quantity * Product.price), 0)
        ).scalar()
        low_stock = (
            Product.query.filter(
                Product.is_active.is_(True),
                Product.quantity <= Product.reorder_level,
            )
            .order_by(Product.quantity.asc())
            .all()
        )

        # last 7 days revenue for a simple chart
        chart = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            d_start = datetime.combine(d, datetime.min.time())
            d_end = d_start + timedelta(days=1)
            rev = (
                db.session.query(func.coalesce(func.sum(Sale.total), 0))
                .filter(Sale.created_at >= d_start, Sale.created_at < d_end)
                .scalar()
            )
            chart.append({"day": d.strftime("%a"), "revenue": float(rev)})

        return render_template(
            "dashboard.html",
            revenue_today=revenue_today,
            sales_count_today=len(sales_today),
            items_sold_today=items_sold_today,
            total_products=total_products,
            stock_value=stock_value,
            low_stock=low_stock,
            chart=chart,
        )

    # ------------------------------- POS ---------------------------------
    @app.route("/pos")
    @login_required
    def pos():
        # Bank account details to show the cashier when "Bank" is selected.
        bank = {
            "name": pay.get("bank_name"),
            "account_name": pay.get("bank_account_name"),
            "account_number": pay.get("bank_account_number"),
            "branch": pay.get("bank_branch"),
            "paybill": pay.get("bank_paybill"),
        }
        return render_template("pos.html", bank_details=bank)

    @app.route("/api/product/<barcode>")
    @login_required
    def api_product(barcode):
        """Look up a product by scanned/typed barcode."""
        product = Product.query.filter_by(barcode=barcode.strip(), is_active=True).first()
        if not product:
            return jsonify({"found": False}), 404
        return jsonify({"found": True, "product": product.to_dict()})

    @app.route("/api/products/search")
    @login_required
    def api_products_search():
        q = request.args.get("q", "").strip()
        query = Product.query.filter_by(is_active=True)
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Product.name.like(like), Product.barcode.like(like)))
        products = query.order_by(Product.name).limit(25).all()
        return jsonify([p.to_dict() for p in products])

    @app.route("/api/products/grid")
    @login_required
    def api_products_grid():
        """All active products for the POS click-to-add grid."""
        products = (
            Product.query.filter_by(is_active=True)
            .order_by(Product.name)
            .all()
        )
        return jsonify([p.to_dict() for p in products])

    @app.route("/api/checkout", methods=["POST"])
    @login_required
    def api_checkout():
        """Finalize a sale: validate stock, persist sale + items, decrement stock."""
        # Sales must be rung up against an open shift so they attribute correctly.
        if not g.shift:
            return jsonify({
                "ok": False, "error": "no_open_shift",
                "message": "Open a shift before making a sale.",
            }), 409

        data = request.get_json(silent=True) or {}
        cart = data.get("items", [])
        cashier = g.user.name[:80]  # the signed-in cashier; ignore any client value

        payment_method = data.get("payment_method", "cash")
        allowed = {m["key"] for m in pay.enabled_methods() if m["ready"]}
        if payment_method not in allowed:
            return jsonify({"ok": False, "error": "That payment method isn't available."}), 400

        # Validate every line against current stock and price it (base currency).
        try:
            resolved, subtotal, tax, total = _price_cart(cart)
        except PricingError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        # --- Currency / tender -------------------------------------------
        # The customer may pay cash in a foreign currency. We always store the
        # sale in base currency; `*_foreign` fields record what was actually
        # handed over. The rate is resolved server-side — never trust a client
        # rate. M-Pesa and bank settle in the base currency only.
        currency = (data.get("currency") or fx.BASE).upper()
        if payment_method in ("mpesa", "bank") and currency != fx.BASE:
            return jsonify({
                "ok": False,
                "error": f"{payment_method.title()} payments are accepted in {fx.BASE} only.",
            }), 400
        rates = fx.cached_rates()
        if currency != fx.BASE and currency not in rates:
            return jsonify({"ok": False, "error": f"No exchange rate for {currency}."}), 400
        rate = rates.get(currency, Decimal("1"))  # foreign units per 1 base

        payment_ref = ""
        mpesa_txn = None
        tendered_foreign = _dec(data.get("amount_tendered", 0))
        if payment_method == "cash":
            # Convert what the customer gave into base currency for accounting.
            amount_tendered = (tendered_foreign / rate) if currency != fx.BASE else tendered_foreign
            # Allow a one-cent tolerance: a foreign amount tendered is rounded to
            # 2dp, so its base equivalent can fall a fraction below the exact total.
            if amount_tendered < total - Decimal("0.01"):
                return jsonify({"ok": False, "error": "Amount tendered is less than total"}), 400
            change_due = max(amount_tendered - total, Decimal("0"))
        elif payment_method == "mpesa":
            # The customer paid via an STK Push that must already be confirmed.
            checkout_id = (data.get("mpesa_checkout_id") or "").strip()
            mpesa_txn = MpesaTransaction.query.filter_by(
                checkout_request_id=checkout_id
            ).first() if checkout_id else None
            if not mpesa_txn or mpesa_txn.status != "success":
                return jsonify({"ok": False, "error": "M-Pesa payment is not confirmed yet."}), 400
            if mpesa_txn.sale_id is not None:
                return jsonify({"ok": False, "error": "That M-Pesa payment is already used."}), 400
            # Guard against ringing up a different total than was charged.
            if mpesa_txn.amount < total.quantize(Decimal("1")):
                return jsonify({"ok": False, "error": "M-Pesa amount doesn't match the sale total."}), 400
            payment_ref = mpesa_txn.mpesa_receipt or mpesa_txn.checkout_request_id
            amount_tendered = total
            tendered_foreign = total
            change_due = Decimal("0")
        else:  # bank — settled out-of-band; the cashier records the reference
            payment_ref = (data.get("payment_ref") or "").strip()[:64]
            if not payment_ref:
                return jsonify({"ok": False, "error": "Enter the bank/transfer reference."}), 400
            amount_tendered = total
            tendered_foreign = total * rate
            change_due = Decimal("0")
        change_foreign = change_due * rate

        amount_tendered = amount_tendered.quantize(Decimal("0.01"))
        change_due = change_due.quantize(Decimal("0.01"))
        tendered_foreign = tendered_foreign.quantize(Decimal("0.01"))
        change_foreign = change_foreign.quantize(Decimal("0.01"))

        # --- Loyalty -----------------------------------------------------
        # An optional customer (attached by phone at the till) earns 1 point
        # per LOYALTY_KES_PER_POINT of the base-currency total.
        customer = None
        points_earned = 0
        customer_id = data.get("customer_id")
        if customer_id:
            customer = Customer.query.filter_by(id=customer_id, is_active=True).first()
            if not customer:
                return jsonify({"ok": False, "error": "Selected customer not found."}), 400
            points_earned = _loyalty_points(total)

        sale = Sale(
            sale_number=_next_sale_number(),
            subtotal=subtotal,
            tax=tax,
            total=total,
            payment_method=payment_method,
            amount_tendered=amount_tendered,
            change_due=change_due,
            payment_ref=payment_ref,
            currency=currency,
            exchange_rate=rate,
            tendered_foreign=tendered_foreign,
            change_foreign=change_foreign,
            cashier=cashier,
            user_id=g.user.id,
            shift_id=g.shift.id,
            customer_id=customer.id if customer else None,
            points_earned=points_earned,
        )
        db.session.add(sale)
        db.session.flush()  # get sale.id / sale_number
        if mpesa_txn is not None:
            mpesa_txn.sale_id = sale.id  # mark the payment as consumed
        if customer is not None and points_earned:
            customer.points += points_earned  # credit the loyalty balance

        for product, qty, line_total in resolved:
            db.session.add(SaleItem(
                sale_id=sale.id,
                product_id=product.id,
                barcode=product.barcode,
                name=product.name,
                unit_price=product.price,
                quantity=qty,
                line_total=line_total,
            ))
            product.quantity -= qty
            db.session.add(StockMovement(
                product_id=product.id,
                change_qty=-qty,
                movement_type="sale",
                reference=sale.sale_number,
            ))

        db.session.commit()
        return jsonify({"ok": True, "sale": sale.to_dict()})

    @app.route("/receipt/<int:sale_id>")
    @login_required
    def receipt(sale_id):
        sale = Sale.query.get_or_404(sale_id)
        return render_template("receipt.html", sale=sale)

    # ----------------------------- Customers -----------------------------
    @app.route("/api/customer/lookup")
    @login_required
    def api_customer_lookup():
        """Find a loyalty customer by phone number (used at the till)."""
        phone = _normalize_phone(request.args.get("phone", ""))
        if not phone:
            return jsonify({"found": False, "error": "Enter a phone number."}), 400
        customer = Customer.query.filter_by(phone=phone, is_active=True).first()
        if not customer:
            return jsonify({"found": False, "phone": phone})
        return jsonify({"found": True, "customer": customer.to_dict()})

    @app.route("/api/customer/create", methods=["POST"])
    @login_required
    def api_customer_create():
        """Register a new loyalty customer by phone (and optional name)."""
        data = request.get_json(silent=True) or {}
        phone = _normalize_phone(data.get("phone", ""))
        name = (data.get("name") or "").strip()[:120]
        if not phone:
            return jsonify({"ok": False, "error": "A phone number is required."}), 400
        existing = Customer.query.filter_by(phone=phone).first()
        if existing:
            # Reactivate / reuse rather than erroring on a known number.
            if not existing.is_active:
                existing.is_active = True
            if name and not existing.name:
                existing.name = name
            db.session.commit()
            return jsonify({"ok": True, "customer": existing.to_dict()})
        customer = Customer(phone=phone, name=name)
        db.session.add(customer)
        db.session.commit()
        return jsonify({"ok": True, "customer": customer.to_dict()})

    @app.route("/customers")
    @admin_required
    def customers():
        q = request.args.get("q", "").strip()
        query = Customer.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Customer.name.like(like), Customer.phone.like(like)))
        people = query.order_by(Customer.is_active.desc(), Customer.points.desc()).all()
        return render_template("customers.html", customers=people, q=q)

    @app.route("/customers/save", methods=["POST"])
    @admin_required
    def customer_save():
        cid = request.form.get("id")
        phone = _normalize_phone(request.form.get("phone", ""))
        name = request.form.get("name", "").strip()[:120]
        if not phone:
            flash("A phone number is required.", "error")
            return redirect(url_for("customers"))

        if cid:
            customer = Customer.query.get_or_404(int(cid))
            clash = Customer.query.filter(Customer.phone == phone, Customer.id != customer.id).first()
            if clash:
                flash(f"Phone {phone} already belongs to another customer.", "error")
                return redirect(url_for("customers"))
            customer.phone = phone
            customer.name = name
            # Admins may correct a points balance directly.
            pts = request.form.get("points")
            if pts not in (None, ""):
                try:
                    customer.points = max(0, int(pts))
                except ValueError:
                    pass
        else:
            if Customer.query.filter_by(phone=phone).first():
                flash(f"A customer with phone {phone} already exists.", "error")
                return redirect(url_for("customers"))
            customer = Customer(phone=phone, name=name)
            db.session.add(customer)

        db.session.commit()
        flash("Customer saved.", "success")
        return redirect(url_for("customers"))

    @app.route("/customers/deactivate/<int:cid>", methods=["POST"])
    @admin_required
    def customer_deactivate(cid):
        customer = Customer.query.get_or_404(cid)
        customer.is_active = not customer.is_active
        db.session.commit()
        flash(
            f"{customer.name or customer.phone} "
            f"{'reactivated' if customer.is_active else 'deactivated'}.",
            "success",
        )
        return redirect(url_for("customers"))

    # ------------------------------- M-Pesa ------------------------------
    @app.route("/api/mpesa/stk", methods=["POST"])
    @login_required
    def api_mpesa_stk():
        """Send an STK Push prompt to the customer's phone for the cart total."""
        if not g.shift:
            return jsonify({"ok": False, "error": "Open a shift before charging."}), 409
        if not (pay.get_bool("mpesa_enabled") and pay.mpesa_configured()):
            return jsonify({"ok": False, "error": "M-Pesa isn't configured."}), 400

        data = request.get_json(silent=True) or {}
        # Re-price server-side so the amount prompted is the amount owed.
        try:
            _, _, _, total = _price_cart(data.get("items", []))
        except PricingError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        try:
            phone = pay.normalize_phone(data.get("phone", ""))
            resp = pay.stk_push(phone, total, description=f"{app.config['STORE_NAME']} sale")
        except pay.MpesaError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        txn = MpesaTransaction(
            checkout_request_id=resp.get("CheckoutRequestID", ""),
            merchant_request_id=resp.get("MerchantRequestID"),
            phone=phone,
            amount=total.quantize(Decimal("1")),
            status="pending",
        )
        db.session.add(txn)
        db.session.commit()
        return jsonify({
            "ok": True,
            "checkout_id": txn.checkout_request_id,
            "message": "Prompt sent. Ask the customer to enter their M-Pesa PIN.",
        })

    @app.route("/api/mpesa/status/<checkout_id>")
    @login_required
    def api_mpesa_status(checkout_id):
        """Report whether a push has been paid. Polls Daraja if still pending,
        so confirmation works even when the public callback can't reach us."""
        txn = MpesaTransaction.query.filter_by(checkout_request_id=checkout_id).first()
        if not txn:
            return jsonify({"ok": False, "error": "Unknown transaction."}), 404

        if txn.status == "pending":
            try:
                res = pay.stk_query(checkout_id)
            except pay.MpesaError:
                res = {"pending": True}  # transient — let the till keep polling
            if not res.get("pending"):
                code = str(res.get("ResultCode", ""))
                txn.result_code = code
                txn.result_desc = (res.get("ResultDesc") or "")[:255]
                txn.status = "success" if code == "0" else "failed"
                db.session.commit()

        return jsonify({"ok": True, **txn.to_dict()})

    @app.route("/api/mpesa/callback", methods=["POST"])
    def api_mpesa_callback():
        """Public endpoint Daraja POSTs the STK result to. Unauthenticated by
        design (Safaricom calls it), so it only updates a known transaction by
        its CheckoutRequestID and never trusts the body for anything else."""
        body = request.get_json(silent=True) or {}
        stk = (body.get("Body") or {}).get("stkCallback") or {}
        checkout_id = stk.get("CheckoutRequestID")
        if not checkout_id:
            return jsonify({"ResultCode": 0, "ResultDesc": "Ignored"})
        txn = MpesaTransaction.query.filter_by(checkout_request_id=checkout_id).first()
        if txn and txn.status == "pending":
            code = str(stk.get("ResultCode", ""))
            txn.result_code = code
            txn.result_desc = (stk.get("ResultDesc") or "")[:255]
            if code == "0":
                items = (stk.get("CallbackMetadata") or {}).get("Item") or []
                receipt = next(
                    (i.get("Value") for i in items if i.get("Name") == "MpesaReceiptNumber"),
                    None,
                )
                txn.mpesa_receipt = str(receipt) if receipt else None
                txn.status = "success"
            else:
                txn.status = "failed"
            db.session.commit()
        # Always ACK so Safaricom stops retrying.
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

    # -------------------------- Payment settings -------------------------
    @app.route("/payment-settings")
    @admin_required
    def payment_settings():
        return render_template(
            "payment_settings.html",
            cfg=pay.public_settings(),
            base_currency=fx.BASE,
            mpesa_ready=pay.mpesa_configured(),
            bank_ready=pay.bank_configured(),
        )

    @app.route("/payment-settings/mpesa", methods=["POST"])
    @admin_required
    def payment_settings_mpesa():
        f = request.form
        values = {
            "mpesa_enabled": "1" if f.get("mpesa_enabled") else "0",
            "mpesa_env": f.get("mpesa_env", "sandbox").strip().lower(),
            "mpesa_consumer_key": f.get("mpesa_consumer_key", "").strip(),
            "mpesa_shortcode": f.get("mpesa_shortcode", "").strip(),
            "mpesa_tx_type": "till" if f.get("mpesa_tx_type") == "till" else "paybill",
            "mpesa_account_ref": f.get("mpesa_account_ref", "").strip() or "POS",
            "mpesa_callback_url": f.get("mpesa_callback_url", "").strip(),
        }
        # Secrets: keep the stored value when the field is left blank.
        for key in ("mpesa_consumer_secret", "mpesa_passkey"):
            submitted = f.get(key, "").strip()
            if submitted:
                values[key] = submitted
        if values["mpesa_env"] not in pay.DARAJA_HOSTS:
            values["mpesa_env"] = "sandbox"
        pay.set_many(values)
        flash("M-Pesa settings saved.", "success")
        return redirect(url_for("payment_settings"))

    @app.route("/payment-settings/bank", methods=["POST"])
    @admin_required
    def payment_settings_bank():
        f = request.form
        values = {
            "bank_enabled": "1" if f.get("bank_enabled") else "0",
            "bank_name": f.get("bank_name", "").strip(),
            "bank_account_name": f.get("bank_account_name", "").strip(),
            "bank_account_number": f.get("bank_account_number", "").strip(),
            "bank_branch": f.get("bank_branch", "").strip(),
            "bank_paybill": f.get("bank_paybill", "").strip(),
            "bank_api_base_url": f.get("bank_api_base_url", "").strip(),
        }
        submitted = f.get("bank_api_key", "").strip()
        if submitted:
            values["bank_api_key"] = submitted
        pay.set_many(values)
        flash("Bank settings saved.", "success")
        return redirect(url_for("payment_settings"))

    @app.route("/payment-settings/mpesa/test", methods=["POST"])
    @admin_required
    def payment_settings_mpesa_test():
        """Verify the M-Pesa credentials by requesting a Daraja access token."""
        try:
            pay.get_access_token()
            flash("M-Pesa connection OK — credentials accepted.", "success")
        except pay.MpesaError as e:
            flash(f"M-Pesa test failed: {e}", "error")
        return redirect(url_for("payment_settings"))

    # ----------------------------- Currencies ----------------------------
    @app.route("/api/rates")
    @login_required
    def api_rates():
        """Live FX rates for the till. Refreshes from the online source when
        stale, falling back to the cached values when offline."""
        rates, err = fx.refresh()
        return jsonify({
            "base": fx.BASE,
            "currencies": fx.supported_currencies(),
            "rates": {c: float(r) for c, r in rates.items()},
            "error": err,
        })

    def _currency_rows():
        """Detailed per-currency rows for the Currencies page / live API."""
        rows = {r.currency: r for r in ExchangeRate.query.all()}
        listing = []
        for cur in fx.supported_currencies():
            row = rows.get(cur)
            rate = float(row.rate) if row else (1.0 if cur == fx.BASE else None)
            listing.append({
                "currency": cur,
                "is_base": cur == fx.BASE,
                "rate": rate,
                "per_unit": (1.0 / rate) if (rate and cur != fx.BASE) else None,
                "source": row.source if row else None,
                "updated_at": row.updated_at if row else None,
            })
        return listing

    @app.route("/currencies")
    @admin_required
    def currencies():
        _, err = fx.refresh()  # opportunistic refresh on page open
        if err:
            flash(f"Couldn't reach the live rate source ({err}). Showing last cached rates.", "error")
        return render_template("currencies.html", rows=_currency_rows(), base=fx.BASE)

    @app.route("/api/currencies")
    @admin_required
    def api_currencies():
        """Live rates for the Currencies page poller. `?force=1` forces a fetch;
        otherwise a normal stale-aware refresh runs. Always returns the rows."""
        force = request.args.get("force") == "1"
        _, err = fx.refresh(force=force)
        return jsonify({
            "ok": err is None,
            "base": fx.BASE,
            "error": err,
            "rows": [
                {**r, "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}
                for r in _currency_rows()
            ],
        })

    @app.route("/currencies/refresh", methods=["POST"])
    @admin_required
    def currencies_refresh():
        # No-JS fallback; the page normally refreshes live via /api/currencies.
        _, err = fx.refresh(force=True)
        if err:
            flash(f"Live update failed ({err}). Showing last cached rates.", "error")
        else:
            flash("Rates updated from the live source.", "success")
        return redirect(url_for("currencies"))

    @app.route("/currencies/save", methods=["POST"])
    @admin_required
    def currencies_save():
        """Pin a currency's rate by hand (foreign units per 1 base unit)."""
        cur = request.form.get("currency", "").strip().upper()
        rate = _dec(request.form.get("rate", "0"))
        if not cur or cur == fx.BASE:
            flash("Choose a non-base currency to override.", "error")
        elif rate <= 0:
            flash("Enter a rate greater than zero.", "error")
        else:
            fx.set_manual_rate(cur, rate)
            flash(f"{cur} rate set manually to {rate} per 1 {fx.BASE}.", "success")
        return redirect(url_for("currencies"))

    # ------------------------------ Updates ------------------------------
    @app.route("/api/update/check")
    @admin_required
    def api_update_check():
        """Report whether a newer GitHub release is available (in-app notice)."""
        return jsonify(updates.check())

    @app.route("/api/update/launch", methods=["POST"])
    @admin_required
    def api_update_launch():
        """Apply the update by re-launching the installer in --update mode.

        The installer (ZTPOS-Setup.exe, dropped next to POS.exe at install time)
        elevates, downloads the latest release from GitHub, closes this app,
        swaps the files in place, and relaunches it. POS.exe runs unelevated and
        cannot replace its own files in Program Files, so the elevated installer
        does it — launched via ShellExecute "runas" so Windows shows the UAC
        prompt and passes the --update argument.
        """
        import ctypes
        exe = os.path.join(app_dir(), "ZTPOS-Setup.exe")
        if not os.path.isfile(exe):
            return jsonify({
                "ok": False,
                "error": "Updater not found. (Only available in the installed app.)",
            }), 404
        try:
            # ShellExecuteW(hwnd, verb, file, params, dir, show). "runas" forces
            # the elevation prompt; >32 means it started. (os.startfile can't
            # pass the --update argument, so we call ShellExecute directly.)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe, "--update", None, 1)
            if rc <= 32:
                return jsonify({
                    "ok": False,
                    "error": f"Could not start the updater (code {rc}).",
                }), 500
            return jsonify({"ok": True})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    # ----------------------------- Products ------------------------------
    @app.route("/products")
    @admin_required
    def products():
        q = request.args.get("q", "").strip()
        query = Product.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Product.name.like(like), Product.barcode.like(like)))
        items = query.order_by(Product.name).all()
        return render_template("products.html", products=items, q=q)

    @app.route("/products/save", methods=["POST"])
    @admin_required
    def product_save():
        pid = request.form.get("id")
        barcode = request.form.get("barcode", "").strip()
        name = request.form.get("name", "").strip()
        if not barcode or not name:
            flash("Barcode and name are required.", "error")
            return redirect(url_for("products"))

        # Validate any uploaded image up-front so we don't half-save the product.
        image_file = request.files.get("image")
        if image_file and image_file.filename and not _allowed_image(image_file.filename):
            flash("Image must be a PNG, JPG, GIF or WEBP file.", "error")
            return redirect(url_for("products"))

        if pid:
            product = Product.query.get_or_404(int(pid))
        else:
            if Product.query.filter_by(barcode=barcode).first():
                flash(f"A product with barcode {barcode} already exists.", "error")
                return redirect(url_for("products"))
            product = Product(barcode=barcode)
            db.session.add(product)

        product.barcode = barcode
        product.name = name
        product.description = request.form.get("description", "").strip()
        product.price = _dec(request.form.get("price", 0))
        product.cost_price = _dec(request.form.get("cost_price", 0))
        product.reorder_level = int(request.form.get("reorder_level") or 5)

        # On create only, allow setting an opening stock quantity.
        if not pid:
            opening = int(request.form.get("quantity") or 0)
            product.quantity = opening
            db.session.flush()
            if opening:
                db.session.add(StockMovement(
                    product_id=product.id, change_qty=opening,
                    movement_type="receive", note="Opening stock",
                ))

        # --- Image: replace, or remove on request ---
        # product.id is set by now (existing row, or flushed above for new ones).
        if request.form.get("remove_image"):
            _delete_product_image(product.image)
            product.image = None
        if image_file and image_file.filename:
            old = product.image
            product.image = _save_product_image(image_file, product)
            if old and old != product.image:
                _delete_product_image(old)

        db.session.commit()
        flash("Product saved.", "success")
        return redirect(url_for("products"))

    @app.route("/products/delete/<int:pid>", methods=["POST"])
    @admin_required
    def product_delete(pid):
        product = Product.query.get_or_404(pid)
        product.is_active = False  # soft delete keeps sales history intact
        db.session.commit()
        flash(f"{product.name} deactivated.", "success")
        return redirect(url_for("products"))

    @app.route("/uploads/products/<path:filename>")
    @login_required
    def product_image(filename):
        """Serve an uploaded product photo from the writable upload dir."""
        return send_from_directory(Config.UPLOAD_DIR, filename)

    # ------------------------------- Stock -------------------------------
    @app.route("/stock")
    @admin_required
    def stock():
        movements = (
            StockMovement.query.order_by(StockMovement.created_at.desc()).limit(100).all()
        )
        products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
        return render_template("stock.html", movements=movements, products=products)

    @app.route("/api/stock/receive", methods=["POST"])
    @admin_required
    def api_stock_receive():
        """Receive stock by scanning a barcode and entering a quantity."""
        data = request.get_json(silent=True) or {}
        barcode = (data.get("barcode") or "").strip()
        qty = int(data.get("quantity") or 0)
        note = (data.get("note") or "").strip()

        if qty == 0:
            return jsonify({"ok": False, "error": "Quantity must not be zero"}), 400

        product = Product.query.filter_by(barcode=barcode).first()
        if not product:
            return jsonify({"ok": False, "error": "unknown_barcode", "barcode": barcode}), 404

        # Reject write-offs larger than what's on hand. Silently clamping to 0
        # would log a movement that doesn't match the actual stock change, so
        # the ledger could no longer be reconciled against quantity-on-hand.
        if qty < 0 and -qty > product.quantity:
            return jsonify({
                "ok": False,
                "error": f"Cannot remove {-qty} — only {product.quantity} on hand.",
            }), 400

        product.quantity += qty
        db.session.add(StockMovement(
            product_id=product.id,
            change_qty=qty,
            movement_type="receive" if qty > 0 else "adjust",
            note=note or ("Stock received" if qty > 0 else "Adjustment"),
        ))
        db.session.commit()
        return jsonify({"ok": True, "product": product.to_dict()})

    # ------------------------------- Sales -------------------------------
    @app.route("/sales")
    @admin_required
    def sales():
        # optional filters: ?from=YYYY-MM-DD&to=YYYY-MM-DD&user=<id>&shift=<id>
        from_str = request.args.get("from")
        to_str = request.args.get("to")
        user_id = request.args.get("user", type=int)
        shift_id = request.args.get("shift", type=int)
        query = Sale.query
        try:
            if from_str:
                query = query.filter(Sale.created_at >= datetime.strptime(from_str, "%Y-%m-%d"))
            if to_str:
                end = datetime.strptime(to_str, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(Sale.created_at < end)
        except ValueError:
            flash("Invalid date filter.", "error")
        if user_id:
            query = query.filter(Sale.user_id == user_id)
        if shift_id:
            query = query.filter(Sale.shift_id == shift_id)

        records = query.order_by(Sale.created_at.desc()).limit(500).all()
        total = sum((s.total for s in records), Decimal("0"))
        people = User.query.order_by(User.name).all()
        return render_template(
            "sales.html", sales=records, total=total,
            from_str=from_str or "", to_str=to_str or "",
            people=people, user_id=user_id, shift_id=shift_id,
        )

    # --------------------------- DB troubleshooting ----------------------
    @app.route("/troubleshoot", methods=["GET", "POST"])
    def troubleshoot():
        """Recover from a database outage without exposing connection details.

        Lets an operator enter the database admin (root) account to test the
        connection and repair the setup: it (re)creates the database, makes the
        app's own configured account valid, creates the tables, and seeds the
        default login — so the app can connect again on the next request.
        Reachable without signing in, since sign-in itself needs the database.
        """
        if request.method == "GET":
            return render_template("troubleshoot.html",
                                   admin_user=Config.DB_USER or "root")

        admin_user = request.form.get("admin_user", "").strip() or "root"
        admin_pw = request.form.get("admin_password", "")
        host, port, name = Config.DB_HOST, Config.DB_PORT, Config.DB_NAME

        # 1) Test the connection with the supplied admin credentials.
        server_uri = (
            f"mysql+pymysql://{admin_user}:{admin_pw}@{host}:{port}/?charset=utf8mb4"
        )
        eng = None
        try:
            eng = create_engine(server_uri, connect_args={"connect_timeout": 6})
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            if eng is not None:
                eng.dispose()
            return render_template(
                "troubleshoot.html", admin_user=admin_user,
                error="Couldn't connect with those credentials. Check the "
                      "password and that the database service is running, then "
                      "try again.",
            ), 400

        # 2) Repair: create the database and make the app's own configured
        #    account valid, so the live connection works without a restart.
        app_user = Config.DB_USER or "root"
        app_pw = (Config.DB_PASSWORD or "").replace("'", "''")
        try:
            with eng.connect() as conn:
                conn.execute(text(
                    f"CREATE DATABASE IF NOT EXISTS `{name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
                if app_user != "root":
                    for hp in ("localhost", "127.0.0.1"):
                        conn.execute(text(
                            f"CREATE USER IF NOT EXISTS '{app_user}'@'{hp}' "
                            f"IDENTIFIED VIA mysql_native_password USING PASSWORD('{app_pw}')"))
                        conn.execute(text(
                            f"ALTER USER '{app_user}'@'{hp}' "
                            f"IDENTIFIED VIA mysql_native_password USING PASSWORD('{app_pw}')"))
                        conn.execute(text(
                            f"GRANT ALL PRIVILEGES ON `{name}`.* TO '{app_user}'@'{hp}'"))
                elif admin_user == "root" and Config.DB_PASSWORD:
                    # The app itself connects as root: align root's password with
                    # the saved config so the live connection succeeds.
                    conn.execute(text(
                        "ALTER USER 'root'@'localhost' "
                        f"IDENTIFIED VIA mysql_native_password USING PASSWORD('{app_pw}')"))
                conn.execute(text("FLUSH PRIVILEGES"))
                conn.commit()
        except Exception:
            return render_template(
                "troubleshoot.html", admin_user=admin_user,
                error="Connected, but couldn't set up the database. Use an "
                      "administrator account allowed to create databases.",
            ), 400
        finally:
            eng.dispose()

        # 3) Create tables + seed the default login using the app's (now valid)
        #    account. Best-effort — the launcher also ensures these at startup.
        try:
            db.create_all()
            if User.query.count() == 0:
                seed = User(username="admin", name="Administrator", role="admin",
                            must_change_password=True)
                seed.set_password("admin")
                db.session.add(seed)
                db.session.commit()
        except Exception:
            db.session.rollback()

        flash("Database connection restored. You can sign in now.", "success")
        return redirect(url_for("login"))

    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("403.html"), 403

    @app.errorhandler(OperationalError)
    @app.errorhandler(InterfaceError)
    def db_unavailable(e):
        """Shown when MariaDB isn't reachable, instead of a raw stack trace."""
        try:
            import os as _os, traceback as _tb
            from config import app_dir as _ad
            with open(_os.path.join(_ad(), "pos_error.log"), "a", encoding="utf-8") as _f:
                _f.write(_tb.format_exc() + "\n" + ("-" * 60) + "\n")
        except Exception:
            pass
        return render_template("db_error.html"), 503


app = create_app()

if __name__ == "__main__":
    # Dev convenience only. NOTE: the real app is launched via launcher.py /
    # POS.exe (native window). debug=False so the reloader doesn't spawn a
    # second process that squats on port 5000.
    print("Running the raw dev server. For the real app use: python launcher.py")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
