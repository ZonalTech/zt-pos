"""Foreign-exchange rates for multi-currency sales.

Rates are cached in the `exchange_rates` table as *foreign-per-base*: how many
units of a currency equal one unit of the store's base currency. They are
refreshed from a free online API (open.er-api.com by default) when stale, and
the last good values are kept so the till keeps working offline. Admins can
override any rate by hand from the Currencies page; manual overrides are never
clobbered by a live refresh.

All functions assume an active Flask app context (they touch the DB).
"""
import json
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal

from config import Config
from models import db, ExchangeRate

BASE = (Config.BASE_CURRENCY or "KES").upper()


def supported_currencies():
    """Base currency first, then the configured extras (de-duplicated)."""
    out = [BASE]
    for c in Config.SUPPORTED_CURRENCIES:
        if c and c not in out:
            out.append(c)
    return out


def _seed_base():
    """Ensure the base currency always exists with a rate of exactly 1."""
    if not ExchangeRate.query.filter_by(currency=BASE).first():
        db.session.add(ExchangeRate(currency=BASE, rate=Decimal("1"), source="manual"))
        db.session.commit()


def cached_rates():
    """{currency: Decimal} from the cache, with base guaranteed present as 1."""
    _seed_base()
    rates = {r.currency: r.rate for r in ExchangeRate.query.all()}
    rates[BASE] = Decimal("1")
    return rates


def _is_stale():
    """True if the newest *live* rate is older than the refresh window (or none)."""
    newest = (
        ExchangeRate.query.filter_by(source="live")
        .order_by(ExchangeRate.updated_at.desc())
        .first()
    )
    if not newest or not newest.updated_at:
        return True
    return datetime.now() - newest.updated_at > timedelta(
        hours=Config.RATE_REFRESH_HOURS
    )


def fetch_live(timeout=6):
    """Fetch live foreign-per-base rates for the supported currencies.

    Returns {currency: Decimal}. Raises on network/parse errors so the caller
    can fall back to the cache.
    """
    url = Config.RATE_API_URL.format(base=BASE)
    req = urllib.request.Request(url, headers={"User-Agent": "ZTPOS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("result") == "error":
        raise RuntimeError(data.get("error-type", "rate API error"))
    # open.er-api.com uses "rates"; some compatible APIs use "conversion_rates".
    api = data.get("rates") or data.get("conversion_rates") or {}
    if not api:
        raise RuntimeError("rate API returned no rates")
    out = {}
    for cur in supported_currencies():
        if cur == BASE:
            out[cur] = Decimal("1")
        elif cur in api:
            out[cur] = Decimal(str(api[cur]))
    return out


def refresh(force=False):
    """Refresh cached rates from the live API when stale (or when forced).

    Manual overrides are preserved. Returns (rates, error) where `error` is a
    human-readable string if the live fetch failed (cache is still returned).
    """
    _seed_base()
    if not force and not _is_stale():
        return cached_rates(), None
    try:
        live = fetch_live()
    except Exception as e:  # noqa: BLE001 — offline / API down: keep the cache
        return cached_rates(), str(e)
    for cur, rate in live.items():
        row = ExchangeRate.query.filter_by(currency=cur).first()
        if row and row.source == "manual":
            continue  # respect admin overrides
        if row:
            row.rate = rate
            row.source = "live"
            row.updated_at = datetime.now()
        else:
            db.session.add(ExchangeRate(currency=cur, rate=rate, source="live"))
    db.session.commit()
    return cached_rates(), None


def set_manual_rate(currency, rate):
    """Pin a currency's rate by hand (won't be overwritten by live refreshes)."""
    currency = currency.upper()
    row = ExchangeRate.query.filter_by(currency=currency).first()
    if row:
        row.rate = Decimal(str(rate))
        row.source = "manual"
        row.updated_at = datetime.now()
    else:
        db.session.add(
            ExchangeRate(currency=currency, rate=Decimal(str(rate)), source="manual")
        )
    db.session.commit()


def to_base(amount, currency, rates=None):
    """Convert an amount expressed in `currency` into the base currency."""
    currency = (currency or BASE).upper()
    amount = Decimal(str(amount))
    if currency == BASE:
        return amount
    rates = rates or cached_rates()
    rate = rates.get(currency)
    if not rate or rate == 0:
        raise KeyError(currency)
    return amount / Decimal(str(rate))


def to_foreign(amount, currency, rates=None):
    """Convert a base-currency amount into `currency`."""
    currency = (currency or BASE).upper()
    amount = Decimal(str(amount))
    if currency == BASE:
        return amount
    rates = rates or cached_rates()
    rate = rates.get(currency)
    if not rate:
        raise KeyError(currency)
    return amount * Decimal(str(rate))
