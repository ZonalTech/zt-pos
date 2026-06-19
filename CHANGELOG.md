# Changelog

All notable changes to the ZT POS app are recorded here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project uses [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

The current version lives in the [`VERSION`](VERSION) file (single source of truth).
Bump it with `python bump_version.py [major|minor|patch]`, which also adds a new
section here. The build bundles `VERSION` into the installer, which writes it to
the installed app's `version.txt` — what the in-app updater compares against the
release manifest.

## [Unreleased]

## [1.2.12] - 2026-06-19
### Changed
- minor updates

## [1.2.11] - 2026-06-19
### Changed
- minor updates

## [1.2.10] - 2026-06-19
### Changed
- minor updates

## [1.2.9] - 2026-06-19
### Changed
- minor updates

## [1.2.8] - 2026-06-19
### Changed
- minor updates

## [1.2.7] - 2026-06-19
### Changed
- minor updates

## [1.2.6] - 2026-06-19
### Changed
- minor updates

## [1.2.5] - 2026-06-19
### Changed
- minor updates

## [1.2.4] - 2026-06-19
### Changed
- minor updates

## [1.2.3] - 2026-06-12
### Changed
- minor updates

## [1.2.2] - 2026-06-12
### Changed
- minor updates

## [1.2.1] - 2026-06-12
### Changed
- minor updates

## [1.2.0] - 2026-06-12
### Added
- **Admin-assigned shifts.** An administrator now assigns each user's work
  shift (Morning/Afternoon/Night) on the Users page instead of the cashier
  picking it every time. The assigned shift shows automatically — on the nav
  pill and the Shift page — and is applied to the till session when the cashier
  opens it, so they only enter the opening cash float. Run `python migrate.py`
  once to add the new `users.assigned_shift` column.
- **Online installer (`ZTPOS-Online-Setup.exe`).** A small bootstrapper that
  bundles no app files — at install time it downloads the latest
  `ZTPOS-<version>.zip` from this repo's GitHub release, verifies its sha256,
  and installs it. The same exe always installs the newest version, and it can
  be run on any machine straight from the release page. Build it with
  `build-online-setup.bat`.

### Changed
- **Updates are now applied by the installer, not a separate `Update.exe`.**
  The in-app "Update now" prompt re-launches the installer in `--update` mode
  (it elevates, downloads the latest release, closes the app, swaps the files,
  and reopens). `Update.exe` / `update_wizard.py` have been removed.
- **The self-contained/offline installer and the legacy Inno Setup path are
  gone.** The online installer is the only installer. `build-setup.bat` now just
  compiles `POS.exe` + `Uninstall.exe` and packages the release zip + manifest;
  `setup.spec`, `update.spec`, `build.bat`, `build-all.bat`, `release.bat`,
  `build_installers.ps1`, and `installer/` were removed.
- **Automatic updates via GitHub Releases.** The app reads the repo's "latest
  release" (tag = version, with `ZTPOS-<version>.zip` attached), so publishing a
  release is all it takes — no manifest to hand-edit. Admins see an in-app
  "Update available — Update now" toast. Publish with `release-github.bat`
  (bumps, builds, and `gh release create`). Set `GITHUB_REPO` ("owner/name") in
  `config.py` and `installer_app/setup_wizard.py`. The repo must be public so
  the app can read releases and download assets.

### Changed
- The "database not reachable" page no longer shows the host/port/database name.
  It now offers a **Troubleshoot** link to a `/troubleshoot` page where an
  operator enters the database admin (root) account to test the connection and
  repair the setup (creates the database, makes the app's own account valid,
  creates tables, seeds the default login) — so the app reconnects without a
  restart. Request handling also no longer crashes on a DB outage, so these
  pages always render.

### Fixed
- In-app updater: the running `Update.exe` now reliably replaces itself after an
  update. The post-exit file swap used to wait by matching the old process's PID
  in `tasklist`, which could spin forever (PID reuse / loose substring matches),
  leaving an `Update.exe.new` behind. It now simply retries the move until it
  succeeds. The uninstaller's self-delete used the same pattern and got the same
  fix.

### Added
- `make_update.py` packages an update — a versioned `ZTPOS-<version>.zip` of the
  app exes plus a `manifest.json` (with sha256). `build-setup.bat` now runs it
  automatically, emitting `release/` artifacts to host for the in-app updater.
  Set `UPDATE_BASE_URL` before building (and `UPDATE_URL` in each install's
  `.env`) to wire updates to your download host.

### Changed
- Rebranded from "Local POS" to **ZT POS** (publisher **Zonal Tech**). The
  install folder is now `C:\Program Files\ZTPOS`, the installer is
  `ZTPOS-Setup.exe`, and the Add/Remove Programs entry, shortcuts, and exe file
  properties all read "ZT POS". (The running app window title still comes from
  the per-store `STORE_NAME` setting, not the product name.)
- The installer now seeds the default `admin`/`admin` POS login when the
  database has no users yet, so a fresh install has an account to sign in with.
- Packaging: `POS.exe`, `Update.exe` and `Uninstall.exe` are now single-file
  (onefile) builds, so the installed app folder is flat — no nested `_internal/`
  directory. The build runs entirely under `setup/`, stages a flat payload in
  `setup/build/`, and leaves only the shippable `setup/ZTPOS-Setup.exe`.

## [1.1.0] - 2026-06-11
### Added
- Forced password change on first login: the default admin is flagged
  `must_change_password` and is sent to a new **Set a new password** screen
  (`/change-password`) until a new password is chosen.
- `fix-database.bat` + `db_setup.sql`: one-click creation of the `admin` MariaDB
  account the app uses, plus the `pos_db` database, tables and default login.
- Versioning: `VERSION` single source of truth, this changelog, and
  `bump_version.py`. The build now writes `version.txt` into `dist\POS\`.

### Changed
- Default credentials are now `admin` / `admin` for both the MariaDB account
  (in `.env`) and the POS app login (`init_db.py`).
- The installer (`pos.iss`) and `build-all.bat` now read the version from the
  `VERSION` file instead of a hardcoded string.

## [1.0.0] - 2026-06-11
### Added
- Initial release: POS with barcode scanning, stock tracking, local payments
  (cash / card / mobile money), shifts, users and sales reporting, backed by a
  local MariaDB database. Packaged as a native Windows app with an installer
  and an in-app updater.
