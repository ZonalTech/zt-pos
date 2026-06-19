"""ZT POS — self-contained Windows setup wizard.

Compiled (with the POS app bundled inside) into a single ZTPOS-Setup.exe.
When the user runs it, it:

  1. Asks for the database admin (root) username + password, port, shop name.
  2. Downloads and silently installs MariaDB as a Windows service (skipped if
     MariaDB is already present), using the password the user chose.
  3. Installs the WebView2 runtime if it's missing (needed for the app window).
  4. Copies the POS app into Program Files and writes its config.
  5. Creates the pos_db database, the app user, and all tables.
  6. Creates Start-Menu + Desktop shortcuts and launches the app.

No Python, no MariaDB, and no other tools need to be pre-installed on the
target PC. Internet is required during installation to download MariaDB.
"""
import argparse
import concurrent.futures
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

APP_NAME = "ZT POS"
APP_PUBLISHER = "Zonal Tech"
INSTALL_DIRNAME = "ZTPOS"
APP_EXE = "POS.exe"
# This installer is the only shippable exe: dropped next to the app at install
# time as ZTPOS-Setup.exe, the app re-launches it with --update to refresh the
# app files in place, and Add/Remove Programs runs it with --uninstall. So no
# separate Update.exe or Uninstall.exe need to ship in the payload.
SETUP_EXE = "ZTPOS-Setup.exe"
# Default update manifest URL written into .env. Leave blank to configure later
# (edit UPDATE_URL in the install folder's .env).
DEFAULT_UPDATE_URL = ""
# Add/Remove Programs registry key (so the POS appears in "Apps & features").
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ZTPOS"

# MariaDB to download (LTS). Bump both together to update.
MARIADB_VERSION = "11.4.4"
MARIADB_URL = (
    "https://archive.mariadb.org/mariadb-11.4.4/winx64-packages/"
    "mariadb-11.4.4-winx64.msi"
)
# Microsoft Edge WebView2 Evergreen bootstrapper (tiny, pulls the runtime).
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# Where the online ("bootstrapper") installer pulls the app from. Each release
# is tagged with its version (e.g. v1.2.0) and ships an ZTPOS-<version>.zip
# asset holding the flat POS.exe / Uninstall.exe payload. The
# GitHub "latest release" API always points at the newest one, so there is
# nothing to hand-edit per release — the same as the in-app updater uses.
GITHUB_REPO = "ZonalTech/zt-pos"
GITHUB_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _detect_version():
    """Read the bundled VERSION file (single source of truth). The build
    bundles it into the installer; falls back if it's somehow missing."""
    try:
        with open(resource_path("VERSION"), "r", encoding="utf-8") as fh:
            return fh.read().strip() or "1.0.0"
    except OSError:
        return "1.0.0"


APP_VERSION = _detect_version()


def payload_dir():
    """Folder (bundled inside this exe) holding the compiled POS app."""
    return resource_path("app_payload")


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _service_exists(name):
    try:
        out = subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.returncode == 0
    except Exception:
        return False


def is_mariadb_installed():
    return _service_exists("MariaDB") or _service_exists("MySQL")


def test_db_connection(password, port, host="127.0.0.1", timeout=5):
    """Try to reach MariaDB as root with the given password/port.

    Returns (status, message):
      True  — connected (the password matches the existing MariaDB root),
      False — reachable but the connection/login failed,
      None  — nothing to test yet (MariaDB will be installed by setup).
    """
    if not is_mariadb_installed():
        return (None, "MariaDB isn't installed yet — setup will download and "
                      "install it. Nothing to test on this PC.")
    try:
        import pymysql
    except Exception as e:  # noqa: BLE001
        return (None, f"Database driver unavailable ({e}).")
    try:
        conn = pymysql.connect(host=host, port=int(port or 3306), user="root",
                               password=password, connect_timeout=timeout)
        conn.close()
        return (True, f"Connected to MariaDB on port {port}. The password matches.")
    except Exception as e:  # noqa: BLE001
        return (False, f"Could not connect: {e}")


def is_webview2_installed():
    """WebView2 Evergreen runtime registers a client GUID in the registry."""
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    paths = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + guid),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + guid),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + guid),
    ]
    for root, sub in paths:
        try:
            with winreg.OpenKey(root, sub):
                return True
        except OSError:
            continue
    return False


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _hidden_startupinfo():
    """A STARTUPINFO that force-hides any window a child process would create.

    CREATE_NO_WINDOW alone can still briefly flash a console window on some
    Windows builds; STARTF_USESHOWWINDOW + SW_HIDE makes the "no window"
    explicit and reliable (e.g. the powershell shortcut helper). Harmless on
    non-Windows.
    """
    si = None
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return si


# How many parallel connections to split a download across, and the smallest
# file worth splitting. Servers (GitHub/Fastly, MariaDB's mirrors) commonly cap
# the speed of a SINGLE connection well below the link's capacity, so splitting
# the file across many connections is what actually makes the download fast —
# on a quick link 16 streams can be ~10x a single one. Each segment retries and
# the whole thing falls back to one resumable stream if ranges aren't supported.
DOWNLOAD_CONNECTIONS = 16
PARALLEL_MIN_BYTES = 2 * 1024 * 1024


def _human_size(n):
    """Bytes -> a short human string, e.g. 31965217 -> '30.5 MB'."""
    if not n or n < 0:
        return "?"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024


def _human_time(secs):
    """Seconds -> '8s' / '1m 45s' / '1h 04m'."""
    secs = int(round(secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _probe(url):
    """Resolve final URL + total size + whether the server supports byte ranges.

    Asks for a 1-byte range: a 206 reply means ranges work (and Content-Range
    carries the full size); a 200 means they don't. Following the request also
    resolves any redirect to the real CDN URL, which we reuse for the segment
    requests. Returns (final_url, total_bytes, ranges_ok)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ZTPOS-Setup", "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        final_url = resp.geturl()
        status = getattr(resp, "status", 200)
        crange = resp.headers.get("Content-Range")
        clen = resp.headers.get("Content-Length")
        resp.read(1)
        if status == 206 and crange and "/" in crange:
            return final_url, int(crange.rsplit("/", 1)[1]), True
        # Server ignored the range; we only know the size if it gave one.
        total = int(clen) if (clen and status == 200) else 0
        return final_url, total, False


class _Progress:
    """Thread-safe download progress aggregator that throttles UI updates to
    ~5/sec and reports percentage, MB done/total, and live speed."""
    def __init__(self, total, progress, status):
        self.total = total
        self.progress = progress
        self.status = status
        self.done = 0
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._last = 0.0

    def add(self, n):
        with self._lock:
            self.done += n
            now = time.monotonic()
            if now - self._last < 0.2 and self.done < self.total:
                return
            self._last = now
            done, total, start = self.done, self.total, self._start
        self._emit(done, total, start)

    def finish(self):
        with self._lock:
            done, total, start = self.done, self.total, self._start
        self._emit(done, total, start)

    def _emit(self, done, total, start):
        if total and self.progress:
            self.progress(min(100, int(done * 100 / total)))
        if self.status:
            spd = done / max(1e-3, time.monotonic() - start)
            pct = f"{min(100, int(done * 100 / total))}% — " if total else ""
            self.status(f"{pct}{_human_size(done)} / {_human_size(total)} "
                        f"· {_human_size(int(spd))}/s")


def _download_segments(url, dest, total, conns, prog, log):
    """Download `url` to `dest` using `conns` parallel ranged connections, each
    writing its own slice of a preallocated file. Raises on failure."""
    with open(dest, "wb") as fh:
        fh.truncate(total)
    seg = total // conns
    ranges = [(i * seg, (total - 1 if i == conns - 1 else (i + 1) * seg - 1))
              for i in range(conns)]

    def fetch(start, end):
        pos = start
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "ZTPOS-Setup", "Range": f"bytes={pos}-{end}"})
                with urllib.request.urlopen(req, timeout=60) as resp, \
                        open(dest, "r+b") as fh:
                    fh.seek(pos)
                    while pos <= end:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        pos += len(chunk)
                        prog.add(len(chunk))
                if pos > end:
                    return True
            except Exception:  # noqa: BLE001 — retry this segment from `pos`
                time.sleep(1 + attempt)
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=conns) as ex:
        futures = [ex.submit(fetch, s, e) for s, e in ranges]
        ok = all(f.result() for f in futures)
    if not ok or os.path.getsize(dest) != total:
        raise IOError("a download segment did not complete")
    prog.finish()


def _download_stream(url, dest, total, prog, log, attempts=6):
    """Single-connection download with retry + Range resume (the fallback when
    the server won't do parallel ranges or the parallel path fails)."""
    last_err = None
    for attempt in range(1, attempts + 1):
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        headers = {"User-Agent": "ZTPOS-Setup"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resumed = getattr(resp, "status", 200) == 206 and have > 0
                if not resumed:
                    have = 0
                    prog.done = 0
                with open(dest, "ab" if resumed else "wb") as fh:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        prog.add(len(chunk))
            got = os.path.getsize(dest)
            if not total or got >= total:
                prog.finish()
                return
            last_err = f"incomplete: got {got} of {total} bytes"
        except Exception as e:  # noqa: BLE001 — network drop / timeout / DNS
            last_err = str(e) or e.__class__.__name__
        if attempt < attempts:
            if log:
                log(f"  connection dropped ({last_err}); resuming "
                    f"(attempt {attempt + 1}/{attempts})…")
            time.sleep(min(2 * attempt, 10))
    raise InstallError(
        f"Download failed after {attempts} attempts.\n{last_err}\n\n"
        "The connection keeps dropping. Check the internet connection and "
        "try again.")


def download(url, dest, progress=None, log=None, status=None,
             connections=DOWNLOAD_CONNECTIONS):
    """Download `url` to `dest`, fast and reliably.

    Uses up to `connections` parallel ranged connections when the server
    supports them (much faster — it beats per-connection CDN throttling), and
    falls back to a single resumable connection otherwise. Reports percentage,
    size, and live speed via `progress`/`status`. Raises InstallError on
    failure."""
    try:
        final_url, total, ranges_ok = _probe(url)
    except Exception:  # noqa: BLE001 — probe failed; try a plain stream
        final_url, total, ranges_ok = url, 0, False

    prog = _Progress(total, progress, status)
    if ranges_ok and total >= PARALLEL_MIN_BYTES and connections > 1:
        conns = min(connections, max(1, total // (1024 * 1024)))
        try:
            _download_segments(final_url, dest, total, conns, prog, log)
            return
        except Exception as e:  # noqa: BLE001 — fall back to a single stream
            if log:
                log(f"  parallel download hit a snag ({e}); using a single "
                    "connection…")
            prog.done = 0
    _download_stream(final_url, dest, total, prog, log)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Payload source — bundled (offline installer) or downloaded (online installer)
# --------------------------------------------------------------------------
def _bundled_payload():
    """Return the flat payload baked into this exe (the self-contained
    installer), or None when this is the online bootstrapper (nothing bundled,
    so the app is fetched from GitHub at run time)."""
    p = payload_dir()
    if os.path.isdir(p) and os.path.isfile(os.path.join(p, APP_EXE)):
        return p
    return None


def _fetch_latest_release():
    """Read GitHub's 'latest release' for GITHUB_REPO and pick the app zip.

    Returns (version, zip_url, sha256_hex). Raises InstallError if the release
    has no ZTPOS .zip asset. GitHub release JSON gives `tag_name` (e.g.
    'v1.2.0') and `assets[]`, each with `name`, `browser_download_url`, and a
    `digest` like 'sha256:...'.
    """
    req = urllib.request.Request(GITHUB_LATEST_URL, headers={
        "User-Agent": "ZTPOS-Setup",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    version = str(data.get("tag_name", "")).strip().lstrip("vV")
    zip_url, digest, size = None, "", 0
    for asset in data.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if name.startswith("ztpos") and name.endswith(".zip"):
            zip_url = asset.get("browser_download_url")
            size = int(asset.get("size") or 0)
            raw = asset.get("digest") or ""
            digest = raw.split(":", 1)[1] if raw.startswith("sha256:") else ""
            break
    if not zip_url:
        raise InstallError(
            "The latest GitHub release has no ZTPOS .zip asset to download.")
    return version, zip_url, digest, size


def _download_payload(log, progress=None, status=None):
    """Online installer: pull the latest release zip from GitHub, verify it,
    and extract it. Returns (payload_dir, version)."""
    log("Checking GitHub for the latest version…")
    try:
        version, url, digest, size = _fetch_latest_release()
    except InstallError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Reached GitHub fine — the repo just has no published release yet.
            raise InstallError(
                f"No published release was found for {GITHUB_REPO}.\n\n"
                "The app is downloaded from this repository's GitHub Releases, "
                "and none has been published yet. Publish a release (with an "
                "ZTPOS-<version>.zip asset) first, then run setup again.")
        if e.code in (403, 429):
            raise InstallError(
                "GitHub is rate-limiting this connection. Wait a few minutes "
                f"and try again.\n{e}")
        raise InstallError(
            f"GitHub returned an error (HTTP {e.code}). Try again later.\n{e}")
    except Exception as e:  # noqa: BLE001 — offline / DNS / TLS
        raise InstallError(
            "Could not reach GitHub to download the app.\n"
            f"{e}\n\nCheck the internet connection and try again.")

    sz = f" ({_human_size(size)})" if size else ""
    log(f"Downloading ZT POS {version} from GitHub{sz}…")
    tmp_zip = os.path.join(tempfile.gettempdir(), "ztpos-payload.zip")
    # Start fresh: never resume onto a partial file left by an earlier run (it
    # could be a different build and corrupt the result). Resume happens only
    # within this download() call's own retries.
    try:
        os.remove(tmp_zip)
    except OSError:
        pass
    t0 = time.monotonic()
    download(url, tmp_zip, progress, log, status)
    dt = time.monotonic() - t0
    got = os.path.getsize(tmp_zip)
    spd = got / max(1e-3, dt)
    log(f"  ✓ Downloaded {_human_size(got)} in {_human_time(dt)} "
        f"({_human_size(int(spd))}/s).")

    if digest:
        log("Verifying download…")
        if _sha256(tmp_zip).lower() != digest.lower():
            raise InstallError(
                "The downloaded app package failed its integrity check. "
                "Try again.")

    extract_dir = os.path.join(tempfile.gettempdir(), "ztpos-payload")
    shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        raise InstallError("The downloaded package is not a valid .zip.")
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass

    # If the zip wraps everything in a single top folder, descend into it.
    entries = [e for e in os.listdir(extract_dir) if not e.startswith("__MACOSX")]
    src = extract_dir
    if len(entries) == 1:
        only = os.path.join(extract_dir, entries[0])
        if os.path.isdir(only) and not os.path.isfile(
                os.path.join(extract_dir, APP_EXE)):
            src = only
    if not os.path.isfile(os.path.join(src, APP_EXE)):
        raise InstallError(
            f"The downloaded package did not contain {APP_EXE}.")
    return src, version


def resolve_payload(log, progress=None, status=None):
    """Where the app files come from: the bundled payload (self-contained
    installer) or a fresh download from GitHub (online installer). Returns
    (payload_dir, version)."""
    bundled = _bundled_payload()
    if bundled:
        log("Using the app bundled in this installer.")
        return bundled, APP_VERSION
    return _download_payload(log, progress, status)


# --------------------------------------------------------------------------
# Install steps
# --------------------------------------------------------------------------
class InstallError(Exception):
    pass


def install_webview2(log, progress=None, status=None):
    if is_webview2_installed():
        log("WebView2 runtime already present — skipping.")
        return
    log("Downloading WebView2 runtime…")
    boot = os.path.join(tempfile.gettempdir(), "MicrosoftEdgeWebview2Setup.exe")
    try:
        t0 = time.monotonic()
        download(WEBVIEW2_URL, boot, progress, log, status)
        log(f"  ✓ Downloaded in {_human_time(time.monotonic() - t0)}.")
    except Exception as e:
        log(f"WARNING: could not download WebView2 ({e}). The app may need it.")
        return
    log("Installing WebView2 runtime…")
    t0 = time.monotonic()
    subprocess.run([boot, "/silent", "/install"],
                   creationflags=_no_window(), startupinfo=_hidden_startupinfo())
    log(f"  ✓ WebView2 installed in {_human_time(time.monotonic() - t0)}.")


def install_mariadb(password, port, log, progress=None, status=None):
    """Returns True if it actually installed MariaDB, False if it was already
    present (in which case the entered password must match the existing root)."""
    if is_mariadb_installed():
        log("MariaDB is already installed — skipping download.")
        return False
    msi = os.path.join(tempfile.gettempdir(), "mariadb.msi")
    log(f"Downloading MariaDB {MARIADB_VERSION}…")
    try:
        t0 = time.monotonic()
        download(MARIADB_URL, msi, progress, log, status)
        dt = time.monotonic() - t0
        got = os.path.getsize(msi)
        log(f"  ✓ Downloaded {_human_size(got)} in {_human_time(dt)} "
            f"({_human_size(int(got / max(1e-3, dt)))}/s).")
    except Exception as e:
        raise InstallError(
            f"Could not download MariaDB.\n{e}\n\n"
            "Check your internet connection and run setup again."
        )
    log("Installing MariaDB service (this can take a few minutes)…")
    if status:
        status("Installing MariaDB…")
    t0 = time.monotonic()
    params = [
        "msiexec.exe", "/i", msi, "/qn", "/norestart",
        "SERVICENAME=MariaDB", f"PORT={port}",
        f"PASSWORD={password}", "UTF8=1",
    ]
    res = subprocess.run(params, creationflags=_no_window(),
                         startupinfo=_hidden_startupinfo())
    if res.returncode != 0:
        raise InstallError(
            f"MariaDB installation failed (code {res.returncode})."
        )
    # Make sure the service is running.
    subprocess.run(["net", "start", "MariaDB"],
                   creationflags=_no_window(), startupinfo=_hidden_startupinfo())
    log(f"  ✓ MariaDB installed in {_human_time(time.monotonic() - t0)}.")
    return True


def app_already_working(install_dir, log):
    """True if an existing install can already reach its database (so a re-run
    needn't touch MariaDB, the database, or the saved credentials)."""
    exe = os.path.join(install_dir, APP_EXE)
    env = os.path.join(install_dir, ".env")
    if not (os.path.isfile(exe) and os.path.isfile(env)):
        return False
    try:
        r = subprocess.run([exe, "--check-db"], creationflags=_no_window(),
                           startupinfo=_hidden_startupinfo(), timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def copy_app(install_dir, src, log):
    log(f"Installing the app to {install_dir}…")
    os.makedirs(install_dir, exist_ok=True)
    if not os.path.isdir(src):
        raise InstallError("The app payload to install is missing.")
    t0 = time.monotonic()
    # Walk-copy with a brief retry per file. On an update re-run the previous
    # POS.exe was just closed, and Windows can hold its handle open for a moment
    # after the process exits — a short retry lets the overwrite succeed instead
    # of failing the whole update.
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        dest_dir = install_dir if rel == "." else os.path.join(install_dir, rel)
        os.makedirs(dest_dir, exist_ok=True)
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dest_dir, f)
            for attempt in range(10):
                try:
                    shutil.copy2(s, d)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise InstallError(
                            f"Could not replace {f} — it is still in use. "
                            "Close ZT POS and try again.")
                    time.sleep(0.5)
    log(f"  ✓ App files copied in {_human_time(time.monotonic() - t0)}.")


def close_running_app(log):
    """Close a running POS.exe so its file can be replaced (update re-run)."""
    log("Closing ZT POS if it is open…")
    subprocess.run(["taskkill", "/f", "/im", APP_EXE],
                   creationflags=_no_window(), startupinfo=_hidden_startupinfo())
    # Give Windows a moment to release the executable's file handle.
    time.sleep(1.0)


def _drop_self_copy(install_dir, log):
    """Copy this installer next to the app. The running POS re-launches it with
    --update to apply updates, and it can also repair a broken setup / install
    MariaDB on first launch."""
    try:
        if getattr(sys, "frozen", False):
            dest = os.path.join(install_dir, SETUP_EXE)
            if os.path.abspath(sys.executable) != os.path.abspath(dest):
                shutil.copy2(sys.executable, dest)
    except Exception as e:
        log(f"  (could not stage the updater/repair installer: {e})")


def _env_quote(value):
    """Double-quote a .env value and escape it so special characters (#, =,
    spaces, backslashes, quotes) survive round-tripping through python-dotenv."""
    s = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
    return '"' + s + '"'


def write_env(install_dir, user, password, port, shop):
    lines = [
        "DB_HOST=127.0.0.1",
        f"DB_PORT={port}",
        "DB_NAME=pos_db",
        f"DB_USER={_env_quote(user)}",
        f"DB_PASSWORD={_env_quote(password)}",
        f"STORE_NAME={_env_quote(shop)}",
        "CURRENCY=KES",
        "TAX_RATE=0",
        f"UPDATE_URL={_env_quote(DEFAULT_UPDATE_URL)}",
    ]
    with open(os.path.join(install_dir, ".env"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_version(install_dir, version):
    """Record the installed version so the updater can compare against the
    online manifest."""
    try:
        with open(os.path.join(install_dir, "version.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(str(version).strip() + "\n")
    except OSError:
        pass


def init_database(install_dir, root_password, user, port, log, mariadb_was_present):
    """Create the database/user/tables. Raises InstallError on failure.

    Must run BEFORE write_env so a wrong password never clobbers a working
    config on a re-run.
    """
    log("Creating the POS database and tables…")
    cfg = {
        "root_password": root_password,
        "db_host": "127.0.0.1",
        "db_port": str(port),
        "db_name": "pos_db",
        "app_user": user,
        "app_password": root_password,
    }
    cfg_path = os.path.join(tempfile.gettempdir(), "pos-init.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    try:
        exe = os.path.join(install_dir, APP_EXE)
        res = subprocess.run(
            [exe, "--init-db", "--config", cfg_path],
            creationflags=_no_window(),
            startupinfo=_hidden_startupinfo(),
        )
        if res.returncode != 0:
            if mariadb_was_present:
                raise InstallError(
                    "MariaDB is already installed on this PC, and the password "
                    "you entered does not match its existing root password.\n\n"
                    "Enter the ORIGINAL root password you set the first time, "
                    "or uninstall MariaDB (Add/Remove Programs) to start fresh."
                )
            raise InstallError(
                "The database could not be initialized. Make sure the MariaDB "
                "service started, then run setup again."
            )
    finally:
        try:
            os.remove(cfg_path)  # contains the root password
        except OSError:
            pass


def _make_shortcut(lnk, target, workdir, log):
    try:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
            "$s.TargetPath='{exe}';$s.WorkingDirectory='{wd}';$s.Save()"
        ).format(lnk=lnk.replace("'", "''"),
                 exe=target.replace("'", "''"),
                 wd=workdir.replace("'", "''"))
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       creationflags=_no_window(), startupinfo=_hidden_startupinfo())
    except Exception as e:
        log(f"  (could not create {os.path.basename(lnk)}: {e})")


def create_shortcuts(install_dir, log):
    log("Creating shortcuts…")
    exe = os.path.join(install_dir, APP_EXE)
    start_menu = os.path.join(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "Microsoft", "Windows", "Start Menu", "Programs")
    desktop = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")

    _make_shortcut(os.path.join(desktop, f"{APP_NAME}.lnk"), exe, install_dir, log)
    _make_shortcut(os.path.join(start_menu, f"{APP_NAME}.lnk"), exe, install_dir, log)
    # Updates are handled inside the app now (Menu → Check for updates), so no
    # separate updater shortcut is created.


def _dir_size_kb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // 1024


def register_uninstall(install_dir, version, log):
    """Register the app in Add/Remove Programs so it uninstalls like any
    normal Windows application (Settings → Apps, or Control Panel).

    Uninstall is handled by the installer itself (ZTPOS-Setup.exe --uninstall),
    dropped next to the app — so no separate Uninstall.exe needs to ship."""
    import winreg
    setup_exe = os.path.join(install_dir, SETUP_EXE)
    if not os.path.isfile(setup_exe):
        log("  (installer not staged; skipping Add/Remove Programs entry)")
        return
    exe = os.path.join(install_dir, APP_EXE)
    values = {
        "DisplayName": APP_NAME,
        "DisplayVersion": version,
        "Publisher": APP_PUBLISHER,
        "InstallLocation": install_dir,
        "DisplayIcon": exe,
        "UninstallString": f'"{setup_exe}" --uninstall',
        "QuietUninstallString": f'"{setup_exe}" --uninstall --silent --purge-data',
    }
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0,
                                winreg.KEY_WRITE) as key:
            for name, val in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD,
                              _dir_size_kb(install_dir))
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        log("Registered in Add/Remove Programs.")
    except OSError as e:
        log(f"  (could not register the uninstaller: {e})")


def launch_app(install_dir):
    exe = os.path.join(install_dir, APP_EXE)
    os.startfile(exe)  # noqa: S606 — launching our own installed app


def run_install(user, password, port, shop, log, progress=None, status=None):
    """Full installation pipeline. Raises InstallError on failure."""
    install_dir = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), INSTALL_DIRNAME)
    started = time.monotonic()

    # Resolve the app payload FIRST. For the online installer this downloads the
    # latest release from GitHub, so a connectivity/availability problem fails
    # fast — before we touch MariaDB or the database.
    payload, version = resolve_payload(log, progress, status)
    if progress:
        progress(0)

    mariadb_was_present = not install_mariadb(password, port, log, progress, status)
    install_webview2(log, progress, status)
    copy_app(install_dir, payload, log)
    _drop_self_copy(install_dir, log)

    # If this PC is already set up and the database is reachable with the
    # existing config, a re-run should just refresh the app files and leave the
    # database + credentials untouched (no password needed).
    if app_already_working(install_dir, log):
        log("Already configured — refreshed the app; kept existing database settings.")
    else:
        # Validate the password / create the DB BEFORE writing .env, so a failed
        # re-run never overwrites a previously-working configuration.
        init_database(install_dir, password, user, port, log, mariadb_was_present)
        write_env(install_dir, user, password, port, shop)

    write_version(install_dir, version)
    create_shortcuts(install_dir, log)
    register_uninstall(install_dir, version, log)
    if status:
        status("Done.")
    log("")
    log(f"✓ Installation complete in {_human_time(time.monotonic() - started)}.")
    return install_dir


def run_update(log, progress=None, status=None):
    """Update an existing install in place: download the latest release from
    GitHub, close the running POS, swap the app files, and bump version.txt.
    Leaves the database, .env credentials, and MariaDB untouched. Raises
    InstallError on failure. Returns (install_dir, version)."""
    install_dir = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), INSTALL_DIRNAME)
    if not os.path.isfile(os.path.join(install_dir, APP_EXE)):
        raise InstallError(
            f"ZT POS is not installed in {install_dir}. Run the installer "
            "first.")
    started = time.monotonic()

    payload, version = _download_payload(log, progress, status)
    if progress:
        progress(0)
    current = ""
    try:
        with open(os.path.join(install_dir, "version.txt"), encoding="utf-8") as fh:
            current = fh.read().strip()
    except OSError:
        pass

    close_running_app(log)
    copy_app(install_dir, payload, log)
    # Re-stage the updater copy in case this release ships a newer installer.
    _drop_self_copy(install_dir, log)
    write_version(install_dir, version)
    # Keep the Add/Remove Programs version in sync.
    register_uninstall(install_dir, version, log)
    if status:
        status("Done.")
    log("")
    elapsed = _human_time(time.monotonic() - started)
    if current and current == version:
        log(f"✓ Reinstalled version {version} in {elapsed}.")
    else:
        log(f"✓ Updated to version {version} in {elapsed}.")
    return install_dir, version


# --------------------------------------------------------------------------
# GUI — a multi-step wizard (Welcome → Settings → Install → Finish)
# --------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    from wizard_ui import Wizard, Page, WHITE, MUTED, make_log_box, append_log

    class WelcomePage(Page):
        title = f"Welcome to the {APP_NAME} setup"
        subtitle = ("This will install everything needed to run the POS on "
                    "this computer.")

        def build(self):
            online = _bundled_payload() is None
            get_app = (
                "    •  Download the latest POS app from GitHub and install it "
                "into Program Files, then create the database.\n\n"
                if online else
                "    •  Copy the POS application into Program Files and create "
                "the database.\n\n"
            )
            net_note = (" An internet connection is required to download the "
                        "app and MariaDB." if online else "")
            steps = (
                "The wizard will:\n\n"
                "    •  Install the MariaDB database engine (downloaded "
                "automatically if it isn't already present).\n\n"
                "    •  Install the WebView2 runtime needed for the app window.\n\n"
                + get_app +
                "    •  Add Start-Menu and Desktop shortcuts.\n\n"
                "Administrator rights are required." + net_note +
                " Click Next to continue."
            )
            tk.Label(self.frame, text=steps, bg=WHITE, justify="left",
                     anchor="nw", wraplength=540).pack(fill="both", expand=True)

    class SettingsPage(Page):
        title = "Database settings"
        subtitle = ("Choose the database administrator account the POS will "
                    "use. Keep these safe — you'll need them for maintenance.")

        def build(self):
            self.vars = {}

            def field(label, default="", show=None, readonly=False):
                row = tk.Frame(self.frame, bg=WHITE)
                row.pack(fill="x", pady=5)
                tk.Label(row, text=label, bg=WHITE, width=18, anchor="w").pack(
                    side="left")
                var = tk.StringVar(value=default)
                ttk.Entry(row, textvariable=var, show=show, width=34,
                          state="readonly" if readonly else "normal").pack(
                    side="left", fill="x", expand=True)
                return var

            self.vars["user"] = field("Admin username", "root")
            self.vars["pass"] = field("Admin password", "", show="•")
            self.vars["port"] = field("Database port", "3306", readonly=True)
            self.vars["shop"] = field("Company / business name", "My Company")

            # Test-connection row: verify MariaDB is reachable before installing.
            test_row = tk.Frame(self.frame, bg=WHITE)
            test_row.pack(fill="x", pady=(12, 2))
            self.test_btn = ttk.Button(test_row, text="Test connection",
                                       command=self._test)
            self.test_btn.pack(side="left")
            self.test_status = tk.Label(test_row, bg=WHITE, anchor="w",
                                        justify="left", wraplength=360, text="")
            self.test_status.pack(side="left", padx=10, fill="x", expand=True)

            tk.Label(self.frame, bg=WHITE, fg=MUTED, justify="left",
                     wraplength=540,
                     text='Enter your company/business name — "-POS" is added '
                          'automatically (e.g. Acme → Acme-POS).').pack(
                anchor="w", pady=(8, 0))

        def _test(self):
            import threading
            self.test_status.config(text="Testing…", fg=MUTED)
            self.test_btn.config(state="disabled")
            pwd = self.vars["pass"].get()
            port = self.vars["port"].get().strip() or "3306"

            def work():
                status, msg = test_db_connection(pwd, port)

                def show():
                    color = {True: "#15803d", False: "#b91c1c"}.get(status, MUTED)
                    self.test_status.config(text=msg, fg=color)
                    self.test_btn.config(state="normal")
                self.wizard.after(show)
            threading.Thread(target=work, daemon=True).start()

        def validate(self):
            v = {k: var.get() for k, var in self.vars.items()}
            if not v["pass"]:
                messagebox.showerror("Setup", "Please enter an admin password.")
                return False
            if not is_admin():
                messagebox.showwarning(
                    "Administrator required",
                    "Please run this installer as Administrator so it can "
                    "install MariaDB and the app.")
                return False
            # The stored/displayed name is the business name with "-POS" appended.
            biz = v["shop"].strip() or "My Company"
            if not biz.upper().endswith("-POS"):
                biz = f"{biz}-POS"
            self.wizard.shared.update(
                user=v["user"].strip() or "root",
                password=v["pass"],
                port=v["port"].strip() or "3306",
                shop=biz,
            )
            return True

    class InstallPage(Page):
        title = "Installing"
        subtitle = "Please wait while the POS is installed…"

        def build(self):
            self.running = False
            self.done = False
            self.progress = ttk.Progressbar(self.frame, mode="determinate")
            self.progress.pack(fill="x", pady=(2, 2))
            self.status_label = tk.Label(self.frame, bg=WHITE, fg=MUTED,
                                         anchor="w", font=("Segoe UI", 9))
            self.status_label.pack(fill="x", pady=(0, 8))
            self.log_box = make_log_box(self.frame)
            self.log_box.pack(fill="both", expand=True)

        def _log(self, msg):
            self.wizard.after(lambda: append_log(self.log_box, msg))

        def _progress(self, pct):
            self.wizard.after(lambda: self.progress.configure(value=pct))

        def _status(self, text):
            self.wizard.after(lambda: self.status_label.config(text=text))

        def on_enter(self):
            if self.done:
                self.wizard.set_next_enabled(True)
                return
            if self.running:
                return
            self.running = True
            self.wizard.set_next_enabled(False)
            self.wizard.set_back_enabled(False)
            self.wizard.set_cancel_enabled(False)
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            s = self.wizard.shared
            try:
                d = run_install(s["user"], s["password"], s["port"],
                                s["shop"], self._log, self._progress,
                                self._status)
                s["install_dir"] = d
                self.done = True
                self.running = False

                def ok():
                    self.progress.configure(value=100)
                    self.wizard.set_next_enabled(True)
                self.wizard.after(ok)
            except Exception as e:  # noqa: BLE001
                self.running = False
                msg = str(e)
                self._log(f"\nERROR: {msg}")

                def fail():
                    messagebox.showerror("Setup failed", msg)
                    self.wizard.set_back_enabled(True)
                    self.wizard.set_cancel_enabled(True)
                self.wizard.after(fail)

        def show_back(self):
            return not self.done

        def show_cancel(self):
            return not self.done

    class FinishPage(Page):
        title = f"{APP_NAME} is installed"
        subtitle = "Setup finished successfully."

        def build(self):
            tk.Label(self.frame, bg=WHITE, justify="left", anchor="nw",
                     wraplength=540,
                     text="The POS is ready to use. You can open it any time "
                          "from the Start Menu or the desktop shortcut.").pack(
                anchor="w", pady=(0, 16))
            self.launch = tk.BooleanVar(value=True)
            tk.Checkbutton(self.frame, bg=WHITE, variable=self.launch,
                           text=f"Launch {APP_NAME} now").pack(anchor="w")

        def next_text(self):
            return "Finish"

        def show_back(self):
            return False

        def show_cancel(self):
            return False

    icon = resource_path(os.path.join("assets", "icon.ico"))
    wiz = Wizard(f"{APP_NAME} — Setup", icon_path=icon, width=620, height=580)
    wiz.add_page(WelcomePage)
    wiz.add_page(SettingsPage)
    wiz.add_page(InstallPage)
    finish = wiz.add_page(FinishPage)

    def on_finish(shared):
        if finish.launch.get() and shared.get("install_dir"):
            launch_app(shared["install_dir"])

    wiz.on_finish = on_finish
    wiz.start()


# --------------------------------------------------------------------------
# GUI — the in-app updater (Updating → Done), launched as ZTPOS-Setup.exe --update
# --------------------------------------------------------------------------
def run_update_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    from wizard_ui import Wizard, Page, WHITE, MUTED, make_log_box, append_log

    class UpdatePage(Page):
        title = f"Updating {APP_NAME}"
        subtitle = "Downloading the latest version from GitHub and applying it…"

        def build(self):
            self.running = False
            self.done = False
            self.progress = ttk.Progressbar(self.frame, mode="determinate")
            self.progress.pack(fill="x", pady=(2, 2))
            self.status_label = tk.Label(self.frame, bg=WHITE, fg=MUTED,
                                         anchor="w", font=("Segoe UI", 9))
            self.status_label.pack(fill="x", pady=(0, 8))
            self.log_box = make_log_box(self.frame)
            self.log_box.pack(fill="both", expand=True)

        def _log(self, msg):
            self.wizard.after(lambda: append_log(self.log_box, msg))

        def _progress(self, pct):
            self.wizard.after(lambda: self.progress.configure(value=pct))

        def _status(self, text):
            self.wizard.after(lambda: self.status_label.config(text=text))

        def on_enter(self):
            if self.done or self.running:
                return
            self.running = True
            self.wizard.set_next_enabled(False)
            self.wizard.set_cancel_enabled(False)
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            try:
                _d, version = run_update(self._log, self._progress, self._status)
                self.wizard.shared["version"] = version
                self.wizard.shared["applied"] = True
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self._log(f"\nERROR: {msg}")
                self.wizard.after(
                    lambda: messagebox.showerror("Update failed", msg))
            self.done = True
            self.running = False

            def ok():
                self.progress.configure(value=100)
                self.wizard.set_next_enabled(True)
            self.wizard.after(ok)

        def next_text(self):
            return "Next"

        def show_back(self):
            return False

        def show_cancel(self):
            return not self.done

    class DonePage(Page):
        title = "Done"
        subtitle = ""

        def build(self):
            self.msg = tk.Label(self.frame, bg=WHITE, justify="left",
                                anchor="nw", wraplength=540, text="")
            self.msg.pack(anchor="w", pady=(0, 14))
            self.relaunch = tk.BooleanVar(value=True)
            self.relaunch_chk = tk.Checkbutton(
                self.frame, bg=WHITE, variable=self.relaunch,
                text=f"Open {APP_NAME} now")

        def on_enter(self):
            if self.wizard.shared.get("applied"):
                self.msg.config(
                    text=f"ZT POS is now version "
                         f"{self.wizard.shared.get('version', '')}.")
                self.relaunch_chk.pack(anchor="w")
            else:
                self.msg.config(text="No changes were made.")

        def next_text(self):
            return "Finish"

        def show_back(self):
            return False

        def show_cancel(self):
            return False

    icon = resource_path(os.path.join("assets", "icon.ico"))
    wiz = Wizard(f"{APP_NAME} — Update", icon_path=icon, width=620, height=500)
    wiz.add_page(UpdatePage)
    done = wiz.add_page(DonePage)

    def on_finish(shared):
        if shared.get("applied") and done.relaunch.get():
            install_dir = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                INSTALL_DIRNAME)
            launch_app(install_dir)

    wiz.on_finish = on_finish
    wiz.start()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ZT POS setup")
    parser.add_argument("--selftest", action="store_true",
                        help="Print environment detection + payload status and exit.")
    parser.add_argument("--out", help="Write selftest output to this file.")
    parser.add_argument("--update", action="store_true",
                        help="Update an existing install in place (the app "
                             "launches this), then exit.")
    parser.add_argument("--uninstall", action="store_true",
                        help="Uninstall the app (Add/Remove Programs runs this); "
                             "delegates to the bundled uninstaller.")
    parser.add_argument("--silent", action="store_true",
                        help="With --update/--uninstall: run with no GUI.")
    # Accept Windows-style /update and /silent too.
    norm = [("--" + a[1:]) if a.startswith("/") else a for a in sys.argv[1:]]
    args, _ = parser.parse_known_args(norm)

    if args.uninstall:
        # Uninstall lives in the bundled `uninstall` module (no separate
        # Uninstall.exe ships). Hand it the remaining flags (--silent,
        # --purge-data, --remove-mariadb) by dropping our own --uninstall token.
        import uninstall
        sys.argv = [sys.argv[0]] + [
            a for a in sys.argv[1:] if a.lower().lstrip("/-") != "uninstall"]
        uninstall.main()
        return

    if args.update:
        if args.silent:
            try:
                run_update(print)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR: {e}")
                sys.exit(1)
        else:
            run_update_gui()
        return

    if args.selftest:
        bundled = _bundled_payload()
        report = {
            "is_admin": is_admin(),
            "mariadb_installed": is_mariadb_installed(),
            "webview2_installed": is_webview2_installed(),
            "mode": "offline (bundled payload)" if bundled
                    else "online (downloads from GitHub)",
            "payload_dir": payload_dir(),
            "payload_has_exe": bool(bundled),
            "github_repo": GITHUB_REPO,
        }
        text = json.dumps(report, indent=2)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        return

    run_gui()


if __name__ == "__main__":
    main()
