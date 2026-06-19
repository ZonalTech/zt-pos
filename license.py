"""Offline license enforcement for the POS.

A license is a short signed token the vendor (Zonal Tech) issues with the React
"License Generator" tool. It is bound to one computer and carries an expiry
date, so it cannot be copied to another machine or used forever for free.

Security model (asymmetric — Ed25519, RFC 8032):
  * The vendor holds the PRIVATE key (only inside the generator) and signs each
    license payload with it.
  * This POS holds only the PUBLIC key (``Config.LICENSE_PUBLIC_KEY``) and can
    *verify* a signature but never *forge* one. A customer therefore cannot mint
    their own license, even with the full source code in hand.

To avoid adding a binary crypto dependency to the PyInstaller build, Ed25519
verification is implemented here in pure Python (the well-known RFC 8032
reference algorithm). It only needs ``hashlib`` and runs once at startup /
activation, so its speed is irrelevant.

Token format (what the customer pastes in):
    <payload_b64url>.<signature_b64url>
optionally wrapped in BEGIN/END lines — both are accepted. ``payload`` is the
UTF-8 JSON object below; the signature is over the ASCII bytes of the
``payload_b64url`` string (JWT-style, so no JSON canonicalisation is needed):

    {"v":1,"machine_id":"AB12-...","customer":"Acme","edition":"pro",
     "issued":"2026-06-19","expires":"2027-06-19"}
"""
import base64
import hashlib
import json
import os
import platform
import uuid
from datetime import date, datetime

from config import Config, app_dir

LICENSE_FILENAME = "license.key"


# ===========================================================================
# Ed25519 signature verification (pure Python, RFC 8032 reference math).
# ===========================================================================
_q = 2 ** 255 - 19
_d = -121665 * pow(121666, _q - 2, _q) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _inv(x):
    return pow(x, _q - 2, _q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = 4 * _inv(5) % _q
_Bx = _xrecover(_By)
_B = [_Bx % _q, _By % _q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return [x3 % _q, y3 % _q]


def _scalarmult(P, e):
    """Iterative double-and-add (iterative to avoid deep recursion on 512-bit e)."""
    result = [0, 1]            # identity
    addend = P
    while e > 0:
        if e & 1:
            result = _edwards(result, addend)
        addend = _edwards(addend, addend)
        e >>= 1
    return result


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _decodeint(s):
    """Little-endian integer over the full byte string (8*len(s) bits)."""
    return sum(2 ** i * _bit(s, i) for i in range(0, 8 * len(s)))


def _isoncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodepoint(s):
    y = sum(2 ** i * _bit(s, i) for i in range(0, 255))
    x = _xrecover(y)
    if x & 1 != _bit(s, 255):
        x = _q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("point not on curve")
    return P


def ed25519_verify(signature, message, public_key):
    """True iff ``signature`` is a valid Ed25519 signature of ``message``.

    ``signature`` is 64 bytes, ``public_key`` 32 bytes, ``message`` arbitrary.
    Returns False (never raises) on any malformed input.
    """
    try:
        if len(signature) != 64 or len(public_key) != 32:
            return False
        R = _decodepoint(signature[:32])
        A = _decodepoint(public_key)
        S = _decodeint(signature[32:])
        h = _decodeint(hashlib.sha512(signature[:32] + public_key + message).digest())
        return _scalarmult(_B, S) == _edwards(R, _scalarmult(A, h))
    except Exception:
        return False


# ===========================================================================
# Encoding + machine identity helpers
# ===========================================================================
def _b64u_decode(s):
    """Decode URL-safe base64 that may be missing its '=' padding."""
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _public_key_bytes():
    """The vendor's Ed25519 public key (32 raw bytes), or None if unset.

    Accepts standard or URL-safe base64 in ``Config.LICENSE_PUBLIC_KEY``.
    """
    raw = (Config.LICENSE_PUBLIC_KEY or "").strip()
    if not raw:
        return None
    for decoder in (base64.b64decode, _b64u_decode):
        try:
            key = decoder(raw)
            if len(key) == 32:
                return key
        except Exception:
            continue
    return None


def _raw_machine_identity():
    """A string that is stable for one physical computer.

    Prefers Windows' MachineGuid (survives disk moves, unique per install),
    falling back to the hostname and the primary NIC's MAC address.
    """
    parts = []
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as k:
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            parts.append(str(guid))
    except Exception:
        pass
    node = platform.node()
    if node:
        parts.append(node)
    parts.append(str(uuid.getnode()))   # MAC-derived; last-resort uniqueness
    return "|".join(p for p in parts if p)


def machine_id():
    """The human-readable Machine ID shown on the lock screen.

    Example: ``A1B2-C3D4-E5F6-7890``. The customer reads this to the vendor,
    who bakes it into the license so it only works on this computer.
    """
    digest = hashlib.sha256(_raw_machine_identity().encode("utf-8")).hexdigest().upper()
    short = digest[:16]
    return "-".join(short[i:i + 4] for i in range(0, 16, 4))


# ===========================================================================
# License file storage (lives beside POS.exe / the source, like .env)
# ===========================================================================
def license_path():
    return os.path.join(app_dir(), LICENSE_FILENAME)


def read_license_file():
    try:
        with open(license_path(), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def save_license_file(text):
    with open(license_path(), "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


def _extract_body(token):
    """Pull the ``payload.signature`` body out of pasted text.

    Tolerates BEGIN/END wrapper lines, surrounding whitespace and line breaks
    so the customer can paste a whole .lic file or a single line.
    """
    lines = [
        ln.strip() for ln in (token or "").splitlines()
        if ln.strip() and "-----" not in ln
    ]
    return "".join(lines)


# ===========================================================================
# Verification + status
# ===========================================================================
def verify_token(token):
    """Validate a pasted token. Returns (ok: bool, reason: str, payload: dict|None)."""
    body = _extract_body(token)
    if not body or "." not in body:
        return False, "License is empty or malformed.", None
    try:
        p_b64, s_b64 = body.split(".", 1)
        payload = json.loads(_b64u_decode(p_b64))
        signature = _b64u_decode(s_b64)
    except Exception:
        return False, "License is malformed and could not be read.", None

    pub = _public_key_bytes()
    if pub is None:
        return False, "This POS has no license public key configured.", payload

    if not ed25519_verify(signature, p_b64.encode("ascii"), pub):
        return False, "License signature is invalid (wrong key or tampered).", payload

    wanted = str(payload.get("machine_id", "")).upper()
    if wanted != machine_id().upper():
        return False, "This license was issued for a different computer.", payload

    expires = payload.get("expires")
    if expires:
        try:
            exp_date = datetime.strptime(expires, "%Y-%m-%d").date()
        except ValueError:
            return False, "License expiry date is invalid.", payload
        if date.today() > exp_date:
            return False, "License expired on %s." % expires, payload

    return True, "Active.", payload


_cache = None


def status(force=False):
    """Cached license status for the running process.

    Returns a dict: ``licensed`` (bool), ``reason`` (str), ``payload`` (dict|None),
    ``machine_id`` (str) and ``days_left`` (int|None — None means perpetual).
    Call with ``force=True`` after installing a new license to recompute.
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    mid = machine_id()
    token = read_license_file()
    if not token:
        _cache = {
            "licensed": False, "reason": "No license installed yet.",
            "payload": None, "machine_id": mid, "days_left": None,
        }
        return _cache

    ok, reason, payload = verify_token(token)
    days_left = None
    if payload and payload.get("expires"):
        try:
            exp = datetime.strptime(payload["expires"], "%Y-%m-%d").date()
            days_left = (exp - date.today()).days
        except ValueError:
            days_left = None
    _cache = {
        "licensed": ok, "reason": reason, "payload": payload,
        "machine_id": mid, "days_left": days_left,
    }
    return _cache


def is_licensed():
    return status()["licensed"]
