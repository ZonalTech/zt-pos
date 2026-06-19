# Licensing (POS side)

ZT POS is gated by an offline license. Until a valid license is installed, every
page and API redirects to the **activation screen** and all features are
disabled. Licenses are issued with the React
[License Generator](../license-generator/README.md).

## Pieces

| File | Role |
|------|------|
| [license.py](../license.py) | Machine ID, Ed25519 verification (pure Python), license file I/O, status cache |
| [config.py](../config.py) | `LICENSE_PUBLIC_KEY` — the vendor's public key used to verify |
| [app.py](../app.py) | `enforce_license` before-request gate + `/activate` route + status in template context |
| [templates/activate.html](../templates/activate.html) | The lock / activation screen |
| `license.key` | The installed license, written beside `POS.exe` (like `.env`); per-machine, never committed |

## How enforcement works

- `enforce_license` runs **before** auth on every request. If unlicensed:
  - browser requests → redirect to `/activate`
  - `/api/*` requests → `403 {"ok": false, "error": "POS is not licensed."}`
  - only `/activate` and `/static/*` stay reachable.
- The check is **DB-independent** — it's just a file read + signature math, so the
  lock screen works even if MariaDB is down.
- Result is cached per-process; `license.status(force=True)` recomputes it (called
  right after a successful activation).

## License token format

`<payload_b64url>.<signature_b64url>`, optionally wrapped in
`-----BEGIN ZT-POS LICENSE-----` / `-----END-----` lines. The payload is JSON:

```json
{ "v": 1, "machine_id": "A1B2-C3D4-E5F6-7890", "customer": "Acme",
  "edition": "pro", "issued": "2026-06-19", "expires": "2027-06-19" }
```

The signature is over the **ASCII bytes of `payload_b64url`** (JWT-style), so the
POS verifies without re-serialising the JSON. Validation checks, in order:
signature → Machine ID matches this PC → not past `expires`.

## Configuring the public key

`Config.LICENSE_PUBLIC_KEY` holds the vendor's Ed25519 public key (base64 of 32
raw bytes). Override per install with `LICENSE_PUBLIC_KEY=...` in `.env`. It must
match the private key in the License Generator, or every license is rejected.

A demo keypair is bundled so the system works out of the box — replace it with
your own before selling (see the generator README).

## Machine ID

`license.machine_id()` hashes the Windows `MachineGuid` (+ hostname + MAC) into a
short `XXXX-XXXX-XXXX-XXXX` code shown on the activation screen. The customer
sends it to the vendor; the license is bound to it.

## Expiry banner

When a valid license is within 14 days of expiry, [base.html](../templates/base.html)
shows a renewal banner to signed-in users (`LICENSE.days_left`).

## Testing locally

1. Start the POS and open it — you'll land on `/activate`. Note the Machine ID.
2. In the License Generator, import the demo seed, enter that Machine ID, generate.
3. Paste the license into the activation box → the POS unlocks.

To re-lock for testing, delete `license.key` from beside the app and restart.
