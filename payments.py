"""Payment methods and gateway integration.

The till accepts three tender types:
  • cash  — counted in the drawer (handled entirely in app.py checkout)
  • mpesa — Lipa na M-Pesa Online (STK Push) via Safaricom's Daraja API
  • bank  — bank transfer / deposit, recorded against a reference the cashier types

M-Pesa and bank are configured at runtime by an admin from the Payment Settings
page; their values live in the `settings` key/value table (NOT .env) so they can
be changed without redeploying. Helpers here read/write those settings and wrap
the Daraja HTTP calls (OAuth → STK Push → STK Query) using only the stdlib, the
same way rates.py talks to the FX API.

All functions assume an active Flask app context (they touch the DB).
"""
import base64
import json
import urllib.request
import urllib.error
from datetime import datetime
from decimal import Decimal

from models import db, Setting

# Daraja host per environment. The same credentials do NOT work across both —
# sandbox uses test credentials from developer.safaricom.co.ke.
DARAJA_HOSTS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}

# Setting keys and their defaults. Booleans are stored as "1"/"0".
DEFAULTS = {
    # --- M-Pesa (Daraja / Lipa na M-Pesa Online) ---
    "mpesa_enabled": "0",
    "mpesa_env": "sandbox",            # sandbox | production
    "mpesa_consumer_key": "",
    "mpesa_consumer_secret": "",
    "mpesa_shortcode": "",             # Paybill or Till number (BusinessShortCode)
    "mpesa_tx_type": "paybill",        # paybill (CustomerPayBillOnline) | till (CustomerBuyGoodsOnline)
    "mpesa_passkey": "",               # Lipa na M-Pesa Online passkey
    "mpesa_account_ref": "POS",        # shown on the customer's statement
    "mpesa_callback_url": "",          # public https URL Daraja POSTs the result to
    # --- Bank transfer (manual reference) ---
    "bank_enabled": "0",
    "bank_name": "",
    "bank_account_name": "",
    "bank_account_number": "",
    "bank_branch": "",
    "bank_paybill": "",                # optional Paybill/till for bank app transfers
    "bank_api_base_url": "",           # reserved for a future live bank integration
    "bank_api_key": "",                # reserved for a future live bank integration
}

# Keys that hold secrets — masked when shown to the admin and only overwritten
# when a new value is actually submitted.
SECRET_KEYS = {"mpesa_consumer_secret", "mpesa_passkey", "bank_api_key"}


# ------------------------------- Settings store ---------------------------
def get(key, default=None):
    """Read a setting, falling back to DEFAULTS then `default`."""
    row = Setting.query.filter_by(key=key).first()
    if row is not None:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default


def get_bool(key):
    return str(get(key, "0")).strip() in ("1", "true", "True", "on", "yes")


def set_many(values):
    """Upsert a dict of {key: value}. Unknown keys are ignored."""
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        row = Setting.query.filter_by(key=key).first()
        if row:
            row.value = "" if value is None else str(value)
        else:
            db.session.add(Setting(key=key, value="" if value is None else str(value)))
    db.session.commit()


def all_settings():
    """Every known setting merged over its default (raw, unmasked)."""
    stored = {r.key: r.value for r in Setting.query.all()}
    return {k: stored.get(k, v) for k, v in DEFAULTS.items()}


def public_settings():
    """Settings for display: secrets replaced by whether one is set, not its value."""
    out = all_settings()
    for k in SECRET_KEYS:
        out[k + "_set"] = bool(out.get(k))
        out[k] = ""
    return out


# ------------------------------- Method status ----------------------------
def mpesa_configured():
    """True when every credential needed for an STK Push is present."""
    return all(get(k) for k in (
        "mpesa_consumer_key", "mpesa_consumer_secret",
        "mpesa_shortcode", "mpesa_passkey", "mpesa_callback_url",
    ))


def bank_configured():
    """A bank tender just needs an account to show the customer."""
    return bool(get("bank_account_number") or get("bank_paybill"))


def enabled_methods():
    """Tender types offered at the till, in display order. Cash is always on.

    Returns a list of {key, label, ready} dicts. `ready` is False when the
    method is toggled on but missing required configuration, so the till can
    show it greyed-out rather than silently dropping it.

    Runs in the template context processor on every request — including the
    login and DB-unavailable pages — so it must never raise. If the settings
    table can't be read (DB down, or not migrated yet) it falls back to
    cash-only rather than taking the whole page down.
    """
    methods = [{"key": "cash", "label": "Cash", "ready": True}]
    try:
        if get_bool("mpesa_enabled"):
            methods.append({"key": "mpesa", "label": "M-Pesa", "ready": mpesa_configured()})
        if get_bool("bank_enabled"):
            methods.append({"key": "bank", "label": "Bank", "ready": bank_configured()})
    except Exception:  # noqa: BLE001 — degrade to cash-only on any DB problem
        db.session.rollback()
    return methods


# ------------------------------- Daraja helpers ---------------------------
class MpesaError(Exception):
    """A recoverable M-Pesa/Daraja failure with a message safe to show staff."""


def _host():
    env = (get("mpesa_env", "sandbox") or "sandbox").strip().lower()
    return DARAJA_HOSTS.get(env, DARAJA_HOSTS["sandbox"])


def _timestamp():
    # Daraja wants the request timestamp as YYYYMMDDHHMMSS (local server time).
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _password(timestamp):
    """base64(shortcode + passkey + timestamp) — the STK Push 'Password' field."""
    raw = f"{get('mpesa_shortcode')}{get('mpesa_passkey')}{timestamp}"
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


def normalize_phone(phone):
    """Coerce common Kenyan formats to the 2547XXXXXXXX / 2541XXXXXXXX shape.

    Accepts 07XX…, 011X…, +2547…, 2547…, 7XX…. Raises on anything else.
    """
    p = "".join(ch for ch in str(phone) if ch.isdigit() or ch == "+").lstrip("+")
    if p.startswith("0") and len(p) == 10:        # 07.. / 01..
        p = "254" + p[1:]
    elif len(p) == 9 and p[0] in ("7", "1"):       # 7.. / 1.. (no leading 0)
        p = "254" + p
    if not (p.startswith("254") and len(p) == 12 and p.isdigit()):
        raise MpesaError("Enter a valid Safaricom number, e.g. 0712345678.")
    return p


def _post_json(url, payload, token):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token(timeout=20):
    """OAuth client-credentials token from Daraja. Raises MpesaError on failure."""
    key, secret = get("mpesa_consumer_key"), get("mpesa_consumer_secret")
    if not key or not secret:
        raise MpesaError("M-Pesa consumer key/secret are not configured.")
    url = f"{_host()}/oauth/v1/generate?grant_type=client_credentials"
    auth = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("utf-8")
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise MpesaError("M-Pesa authentication failed — check the consumer key/secret "
                         f"and environment ({e.code}).")
    except urllib.error.URLError as e:
        raise MpesaError(f"Couldn't reach M-Pesa ({e.reason}).")
    token = data.get("access_token")
    if not token:
        raise MpesaError("M-Pesa did not return an access token.")
    return token


def stk_push(phone, amount, account_ref=None, description="POS sale"):
    """Trigger an STK Push prompt on the customer's phone.

    `amount` is rounded to a whole shilling (Daraja only accepts integers).
    Returns Daraja's response dict including MerchantRequestID / CheckoutRequestID.
    Raises MpesaError on any failure.
    """
    phone = normalize_phone(phone)
    amount_int = int(Decimal(str(amount)).quantize(Decimal("1")))
    if amount_int < 1:
        raise MpesaError("M-Pesa amount must be at least 1.")

    token = get_access_token()
    ts = _timestamp()
    tx_type = ("CustomerBuyGoodsOnline"
               if get("mpesa_tx_type") == "till" else "CustomerPayBillOnline")
    payload = {
        "BusinessShortCode": get("mpesa_shortcode"),
        "Password": _password(ts),
        "Timestamp": ts,
        "TransactionType": tx_type,
        "Amount": amount_int,
        "PartyA": phone,
        "PartyB": get("mpesa_shortcode"),
        "PhoneNumber": phone,
        "CallBackURL": get("mpesa_callback_url"),
        "AccountReference": (account_ref or get("mpesa_account_ref") or "POS")[:12],
        "TransactionDesc": description[:60],
    }
    url = f"{_host()}/mpesa/stkpush/v1/processrequest"
    try:
        data = _post_json(url, payload, token)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("errorMessage", "")
        except Exception:
            pass
        raise MpesaError(f"STK Push rejected by M-Pesa: {detail or e.code}")
    except urllib.error.URLError as e:
        raise MpesaError(f"Couldn't reach M-Pesa ({e.reason}).")
    # ResponseCode "0" means the prompt was accepted for delivery.
    if str(data.get("ResponseCode")) != "0":
        raise MpesaError(data.get("ResponseDescription") or "M-Pesa rejected the request.")
    return data


def stk_query(checkout_request_id):
    """Ask Daraja for the final status of a push (the STK Push Query API).

    Returns the raw response dict. ResultCode "0" == paid; a missing ResultCode
    with errorCode 500.001.1001 means the user hasn't responded yet (still
    pending). Raises MpesaError only on transport/auth problems.
    """
    token = get_access_token()
    ts = _timestamp()
    payload = {
        "BusinessShortCode": get("mpesa_shortcode"),
        "Password": _password(ts),
        "Timestamp": ts,
        "CheckoutRequestID": checkout_request_id,
    }
    url = f"{_host()}/mpesa/stkpushquery/v1/request"
    try:
        return _post_json(url, payload, token)
    except urllib.error.HTTPError as e:
        # While the customer is still being prompted Daraja answers 500 with
        # "The transaction is being processed" — treat that as 'pending'.
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        if body.get("errorCode") == "500.001.1001":
            return {"pending": True}
        raise MpesaError(body.get("errorMessage") or f"Status check failed ({e.code}).")
    except urllib.error.URLError as e:
        raise MpesaError(f"Couldn't reach M-Pesa ({e.reason}).")
