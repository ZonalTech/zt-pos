"""First-launch database bootstrap.

Called by the launcher *before* the app window opens. If the database isn't
reachable it tries, in order, to bring it up automatically:

  1. MariaDB service exists but is stopped  -> start it (elevated).
  2. Server reachable but pos_db missing    -> create the database + tables.
  3. MariaDB not installed at all           -> hand off to the bundled
                                               installer (ZTPOS-Setup.exe),
                                               which installs MariaDB and
                                               relaunches the app.

Everything is best-effort: if it can't fix things it returns "no_db" and the
app shows its friendly "Can't reach the database" page.
"""
import ctypes
import os
import subprocess
import time

from sqlalchemy import create_engine, text

from config import Config, app_dir, server_uri


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _sc_query(name):
    try:
        return subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True, text=True, creationflags=_no_window(),
        )
    except Exception:
        return None


def mariadb_service():
    """Return the MariaDB/MySQL service name if one is installed, else None."""
    for name in ("MariaDB", "MySQL"):
        r = _sc_query(name)
        if r is not None and r.returncode == 0:
            return name
    return None


def service_running(name):
    r = _sc_query(name)
    return r is not None and "RUNNING" in (r.stdout or "")


def run_elevated(exe, params=""):
    """Launch a program elevated (UAC). Returns True if it started."""
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return rc > 32
    except Exception:
        return False


def start_service(name):
    """Start a Windows service, elevating if we're not already admin."""
    if is_admin():
        subprocess.run(["net", "start", name], creationflags=_no_window())
        return True
    return run_elevated("net.exe", f"start {name}")


def find_installer():
    """Look for the bundled installer next to the app so we can self-repair."""
    for name in ("ZTPOS-Setup.exe", "Repair-Setup.exe"):
        p = os.path.join(app_dir(), name)
        if os.path.isfile(p):
            return p
    return None


def can_connect(uri, timeout=3):
    try:
        engine = create_engine(uri, connect_args={"connect_timeout": timeout})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def _create_db_and_schema():
    """Create the pos_db database and all tables (needs privileged DB_USER)."""
    engine = create_engine(server_uri())
    with engine.connect() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{Config.DB_NAME}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
        conn.commit()
    engine.dispose()
    _ensure_schema()


def _ensure_schema():
    """Create any missing tables (safe to call when they already exist)."""
    from app import app as flask_app
    from models import db
    with flask_app.app_context():
        db.create_all()


def bootstrap(log=print):
    """Try to make the database reachable. Returns one of:
       'ok'       - database is reachable (tables ensured)
       'handoff'  - launched the installer; the caller should exit
       'no_db'    - could not fix it; show the error page
    """
    uri = Config.SQLALCHEMY_DATABASE_URI

    if can_connect(uri):
        try:
            _ensure_schema()
        except Exception:
            pass
        return "ok"

    svc = mariadb_service()

    # Case 3: MariaDB isn't installed — auto-install via the bundled installer.
    if not svc:
        installer = find_installer()
        if installer:
            log("MariaDB not found — launching the installer…")
            if run_elevated(installer):
                return "handoff"
        log("MariaDB is not installed.")
        return "no_db"

    # Case 1: service is installed but stopped — start it.
    if not service_running(svc):
        log(f"Starting the {svc} service…")
        start_service(svc)
        for _ in range(20):
            if can_connect(uri):
                try:
                    _ensure_schema()
                except Exception:
                    pass
                return "ok"
            if can_connect(server_uri()):
                break
            time.sleep(1)

    # Case 2: server is up but the database/tables are missing — create them.
    if can_connect(server_uri()):
        try:
            log("Creating the POS database…")
            _create_db_and_schema()
            if can_connect(uri):
                return "ok"
        except Exception as e:
            log(f"Could not create the database automatically: {e}")

    return "no_db"
