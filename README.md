# ZT POS

A Windows point-of-sale **desktop app** that runs entirely on one machine. The
UI opens in its **own native app window** (not a web browser) — built with
Flask + Jinja templates + a little JavaScript, rendered inside an embedded
WebView2 window. All data — products, stock, and sales — lives in a **local
MariaDB** database. Payments are recorded locally (cash / card / mobile money),
and stock is added and counted by **scanning barcodes**.

The app compiles to a single **`POS.exe`**, and ships as a small **online
Windows installer** (`ZTPOS-Online-Setup.exe`) that downloads the app from this
repo's latest GitHub release, downloads and installs MariaDB, asks for the admin
(root) username and password during setup, and creates the database
automatically. Updates are applied from inside the app (it re-launches the same
installer in update mode).

> Built per `readme`: *"Windows app with local MariaDB and a POS UI, payments
> done locally, scanning of stock into the system, tracking of stock and sales"*,
> plus *"compile into a Windows app whose installer downloads/installs the
> database and asks for the root user and password,"* and *"the UI should open
> inside the app, not be web-based."*

## Features

- **Native app window** — opens like any desktop program; no browser, no URLs.
- **POS screen** — scan a barcode (any USB scanner works; it types like a
  keyboard) or search by name, build a cart, take payment, and print a receipt.
- **Local payments** — cash (with change calculation), card, or mobile money,
  all recorded on this machine. No internet or payment gateway required.
- **Stock scanning** — receive deliveries or make adjustments by scanning the
  barcode and entering a quantity. Every change is logged.
- **Stock tracking** — live on-hand quantities, reorder levels, low-stock alerts.
- **Sales tracking** — full history with date filtering, per-sale receipts, a
  7-day revenue chart, and daily totals.

## Tech

| Part        | Choice                                          |
|-------------|-------------------------------------------------|
| Language    | Python 3.10+                                    |
| UI window   | pywebview (embedded WebView2)                   |
| Web / views | Flask + Jinja2 templates + vanilla JS           |
| Server      | waitress (local WSGI, background thread)        |
| Database    | MariaDB (local), via SQLAlchemy + PyMySQL       |
| Packaging   | PyInstaller (POS.exe) + online installer (GitHub releases) |

---

## For end users — installing the app

1. Get **`ZTPOS-Online-Setup.exe`** (a small file, ~12 MB) from the latest
   [GitHub release](https://github.com/ZonalTech/zt-pos/releases/latest)
   and double-click it. Approve the Windows admin (UAC) prompt.
2. The setup window asks for:
   - **Admin username** (default `root`) and **password** — the database account
     the POS will use. *Remember these.*
   - **Database port** (default `3306`) and your **shop name**.
3. Click **Install**. It then automatically:
   - **Downloads the latest POS app** from this repo's GitHub release,
   - **Downloads MariaDB** and installs it as a Windows service using the
     password you chose (skipped if MariaDB is already present),
   - Installs the **WebView2** runtime if it's missing,
   - Copies the app into `C:\Program Files\ZTPOS`,
   - **Creates the `pos_db` database**, the app user, and all tables,
   - Adds Start-Menu + Desktop shortcuts.
4. Click **Finish & Launch** — the POS opens in its own window.

> **The installer always installs the newest version** — it pulls the app from
> GitHub at install time, so the same file never goes stale.

> **Updating.** Inside the app, **Menu → Check for updates**. When a newer
> release exists you'll see an "Update now" prompt; approving it re-runs the
> installer in update mode, which downloads the new version, swaps the files,
> and reopens the app. Your database and settings are left untouched.

> **Re-running the installer is safe.** If the app is already set up and its
> database is reachable, a re-run just refreshes the app files and leaves
> MariaDB, the database, and your saved password untouched (no password needed).

> Requirements on the target PC: 64-bit Windows 10/11 and an internet connection
> *during installation and updates* (to download the app + MariaDB). Nothing else
> needs to be pre-installed — Python, MariaDB, and the runtime are all handled.

---

## For developers — building & releasing

You build on a dev machine; end users only get the one small
`ZTPOS-Online-Setup.exe`. Everything is built with **PyInstaller only** — no
other tools required.

### Prerequisite (dev machine only)
- **Python 3.10+** (with *"Add Python to PATH"*).

### The two build steps
```
build-setup.bat          :: compile POS.exe + Uninstall.exe, package the
                         :: release zip + manifest into release\
build-online-setup.bat   :: build the installer  ->  setup\ZTPOS-Online-Setup.exe
```
`build-setup.bat` produces the **app payload** the installer downloads:
`release\ZTPOS-<version>.zip` (POS.exe + Uninstall.exe) and `release\manifest.json`.
`build-online-setup.bat` produces the **only shippable file**,
`setup\ZTPOS-Online-Setup.exe` — a small bootstrapper that bakes in *no* app
files and instead downloads that zip from the latest GitHub release at install
(and update) time. The installed app folder is flat: `POS.exe`, `Uninstall.exe`,
`.env`, `version.txt`, and a dropped copy of the installer (`ZTPOS-Setup.exe`)
that the app re-runs to update.

The installer logic lives in [installer_app/setup_wizard.py](installer_app/setup_wizard.py);
to target a different MariaDB version, edit `MARIADB_VERSION` / `MARIADB_URL`,
or the release source, edit `GITHUB_REPO`, at the top of it.

### Versioning & releases

The version is tracked in **one place — the [`VERSION`](VERSION) file** — and
flows everywhere from there: the build stamps it into the release tag and the
`ZTPOS-<version>.zip`, and the installer writes it to `version.txt` next to
`POS.exe` (what the in-app update check compares against the latest release).
Changes are recorded in [`CHANGELOG.md`](CHANGELOG.md).

**Cut a release with one command** (needs the [GitHub CLI](https://cli.github.com/),
`gh auth login`):

```
release-github.bat            :: bump patch (1.1.0 -> 1.1.1), build, publish
release-github.bat minor      :: new feature      (1.1.0 -> 1.2.0)
release-github.bat major      :: breaking change  (1.1.0 -> 2.0.0)
release-github.bat 1.4.2      :: set an explicit version
```

It runs `bump_version.py` (updates `VERSION` + opens a new `CHANGELOG.md`
section), builds the app payload and the installer, and publishes a GitHub
Release tagged `v<version>` with the zip, manifest, and
`ZTPOS-Online-Setup.exe` attached. Pushing a `v*.*.*` tag does the same on CI
(see [.github/workflows/release.yml](.github/workflows/release.yml)). Installed
apps offer the new version on their next update check.

---

## Running from source (no compiling)

Useful while developing. You need a local MariaDB already installed and running.

```
copy .env.example .env          :: then set DB_PASSWORD (and STORE_NAME, etc.)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python init_db.py               :: creates pos_db + tables
python seed.py                  :: optional: ~10 sample products
python launcher.py              :: opens the POS in its native window
```
(`run.bat` does all of this for you.)

## Daily use

1. **Products** — add what you sell (barcode, name, price, opening stock).
2. **Stock** — when deliveries arrive, scan each barcode and enter the quantity.
   Use a negative quantity to write off damaged/expired stock.
3. **POS** — scan items into the cart, press **Charge**, choose a payment
   method, complete the sale. Stock is reduced automatically.
4. **Dashboard / Sales** — watch revenue, items sold, low-stock alerts, and
   browse/filter past sales (each with a printable receipt).

## Project layout

```
launcher.py        entry point -> POS.exe (native window + --init-db mode)
app.py             Flask routes + checkout/stock APIs
config.py          settings (reads .env beside the exe)
models.py          SQLAlchemy models (products, sales, sale_items, stock_movements)
init_db.py         dev helper: create DB + tables from .env
seed.py            optional sample data
pos.spec           PyInstaller spec for the app (POS.exe)
setup_online.spec  PyInstaller spec for the online installer (no payload bundled)
build-setup.bat    >>> build POS.exe + Uninstall.exe + the release zip/manifest
build-online-setup.bat  >>> build the installer (setup\ZTPOS-Online-Setup.exe)
release-github.bat >>> bump the version, build, and publish a GitHub release
make_update.py     packages the app payload zip + manifest for a release
VERSION            single source of truth for the app version
CHANGELOG.md       what changed in each version
bump_version.py    bumps VERSION + opens a new changelog section
updates.py         in-app check for a newer GitHub release (the "Update" notice)
installer_app/setup_wizard.py   the installer/updater: GUI + GitHub download + MariaDB + DB + launch
templates/         Jinja pages (dashboard, pos, products, stock, sales, receipt, db_error)
static/            CSS + JS (barcode scanning, cart, stock receiving) + img/ (logo, favicon)
assets/icon.ico    app/installer icon (generated)
gen_icon.py        regenerates icon.ico / favicon.ico / logo.png from the logo
```

To change the logo, edit [static/img/logo.svg](static/img/logo.svg) (or the shapes
in `gen_icon.py`) and run `python gen_icon.py`, then rebuild.

## First-launch self-healing

When the app starts it checks the database before opening the window and fixes
common problems automatically (see [provision.py](provision.py)):

- **MariaDB service stopped** (e.g. after a reboot) → it starts the service.
- **Database/tables missing** → it creates `pos_db` and the tables.
- **MariaDB not installed** → it re-runs the bundled installer (a copy is placed
  in the install folder) to download and install MariaDB, then relaunches.

If it still can't connect, the app shows a clear "Can't reach the database" page
instead of an error trace.

## Notes
- A USB barcode scanner needs no setup — it behaves as a keyboard. On the POS
  and Stock pages the input field is focused automatically, so a scan just works.
- Deleting a product is a soft-delete (it's deactivated) so historical sales
  stay intact.
- Everything is local: the database, the server, and all payment records.
  Nothing is sent over the internet (except the one-time MariaDB download at
  install time).
- The app user's password is stored in `.env` next to the exe (plain text) — this
  is a single-machine local POS. The root password is never written to disk by
  the installer.
