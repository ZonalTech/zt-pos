"""POS launcher — the entry point compiled into POS.exe.

Three modes:

  POS.exe                      Serve the POS (production WSGI via waitress) and
                               open it in a native desktop window (WebView2),
                               falling back to the default browser if no
                               WebView2 runtime is available.

  POS.exe --init-db --config <file>
                               Used by the installer right after MariaDB is
                               installed. Reads root + app credentials from a
                               JSON file, creates the database, the app user,
                               and all tables. Prints OK / an error and exits.

  POS.exe --check-db           Exit 0 if the database is reachable with the
                               current .env, else 1. Used by the installer to
                               detect an already-working install on a re-run.

  POS.exe --drop-db            Drop the POS database using the current .env
                               credentials, then exit. Used by the uninstaller
                               to remove all data. Runs windowless (GUI exe),
                               so it never flashes a console.

Keeping database creation inside the compiled exe means the installer needs
nothing but this one binary to provision everything.
"""
import argparse
import json
import os
import sys
import threading
import time

import sqlalchemy
from sqlalchemy import text


def init_db_from_config(config_path):
    """Provision the database using credentials collected by the installer.

    The JSON file contains:
      { "root_password": "...", "db_host": "127.0.0.1", "db_port": "3306",
        "db_name": "pos_db", "app_user": "root", "app_password": "..." }

    MariaDB's admin account is always 'root'; we connect as root (whose password
    the MSI just set) to create the database. If the app user is something other
    than root, we create it and grant it rights on the POS database.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    host = cfg.get("db_host", "127.0.0.1")
    port = cfg.get("db_port", "3306")
    name = cfg.get("db_name", "pos_db")
    root_pw = cfg.get("root_password", "")
    app_user = cfg.get("app_user", "root")
    app_pw = cfg.get("app_password", root_pw)

    root_uri = (
        f"mysql+pymysql://root:{root_pw}@{host}:{port}/?charset=utf8mb4"
    )

    engine = sqlalchemy.create_engine(root_uri)
    with engine.connect() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
        if app_user and app_user != "root":
            # Escape single quotes in the password for the SQL literal.
            safe_pw = app_pw.replace("'", "''")
            conn.execute(text(
                f"CREATE USER IF NOT EXISTS '{app_user}'@'localhost' "
                f"IDENTIFIED BY '{safe_pw}'"
            ))
            conn.execute(text(
                f"GRANT ALL PRIVILEGES ON `{name}`.* TO '{app_user}'@'localhost'"
            ))
            conn.execute(text("FLUSH PRIVILEGES"))
        conn.commit()
    engine.dispose()

    # Create tables via the app's own models, pointed at the new database.
    import os
    os.environ["DB_HOST"] = host
    os.environ["DB_PORT"] = str(port)
    os.environ["DB_NAME"] = name
    os.environ["DB_USER"] = app_user
    os.environ["DB_PASSWORD"] = app_pw

    # Import after env is set so Config picks these up.
    from app import app as flask_app
    from models import db, User
    with flask_app.app_context():
        db.create_all()
        # Seed the default POS login if there are no users yet. The app login is
        # separate from the MariaDB account, so without this a fresh install has
        # nobody to sign in as. Forced to change on first sign-in.
        if User.query.count() == 0:
            admin = User(username="admin", name="Administrator", role="admin",
                         must_change_password=True)
            admin.set_password("admin")
            db.session.add(admin)
            db.session.commit()


def _start_server(flask_app, host, port):
    """Run the WSGI server (blocking) — launched on a background thread."""
    from waitress import serve as waitress_serve
    waitress_serve(flask_app, host=host, port=port, threads=8, _quiet=True)


def _wait_until_up(url, timeout=15.0):
    """Poll the local server until it answers, so the window opens to a ready app."""
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # server responded (even a 500 means it's up)
        except Exception:
            time.sleep(0.3)
    return False


def _pick_port(host, preferred):
    """Return `preferred` if free, otherwise an OS-assigned free port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, int(preferred)))
            return int(preferred)
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
        s2.bind((host, 0))
        return s2.getsockname()[1]


def serve():
    """Run the POS inside a native desktop window (no web browser).

    The Flask app is served locally by waitress on a background thread; the UI
    is shown in an embedded WebView2 window via pywebview. Closing the window
    stops the app.
    """
    from app import app as flask_app
    from config import Config

    # First-launch self-healing: make sure the database is reachable. If
    # MariaDB isn't installed this can hand off to the installer and exit.
    try:
        from provision import bootstrap
        if bootstrap() == "handoff":
            return  # the installer is taking over; this process should exit
    except Exception as e:
        print(f"Database bootstrap skipped ({e}).")

    # Pick a port. If the preferred one is taken (e.g. another app — or a
    # stray "python app.py" — is squatting on 5000), use a free one instead,
    # so our window never shows someone else's page.
    port = _pick_port(Config.HOST, Config.PORT)
    url = f"http://{Config.HOST}:{port}"

    # Serve in the background; the main thread owns the GUI window.
    threading.Thread(
        target=_start_server, args=(flask_app, Config.HOST, port), daemon=True
    ).start()
    _wait_until_up(url)

    try:
        import webview
        from config import resource_path
        webview.create_window(
            Config.STORE_NAME,
            url,
            width=1280,
            height=820,
            min_size=(1000, 680),
        )
        # Brand the window + taskbar with the app logo (falls back silently if
        # the icon is missing or this pywebview backend ignores the argument).
        icon = resource_path("assets/icon.ico")
        try:
            webview.start(icon=icon) if os.path.isfile(icon) else webview.start()
        except TypeError:
            webview.start()  # older pywebview without the icon argument
    except Exception as e:
        # No WebView2 runtime / GUI unavailable: fall back to the browser so
        # the POS is still usable, and keep the server alive.
        import webbrowser
        print(f"Native window unavailable ({e}); opening in your browser instead.")
        print(f"POS running at {url} — close this window to stop.")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def main():
    parser = argparse.ArgumentParser(description="ZT POS")
    parser.add_argument("--init-db", action="store_true",
                        help="Create database/user/tables, then exit.")
    parser.add_argument("--check-db", action="store_true",
                        help="Exit 0 if the database is reachable with the current .env, else 1.")
    parser.add_argument("--drop-db", action="store_true",
                        help="Drop the POS database (used by the uninstaller), then exit.")
    parser.add_argument("--config", help="Path to JSON credentials (with --init-db).")
    args = parser.parse_args()

    if args.check_db:
        try:
            from provision import can_connect
            from config import Config
            sys.exit(0 if can_connect(Config.SQLALCHEMY_DATABASE_URI, timeout=4) else 1)
        except Exception:
            sys.exit(1)

    if args.drop_db:
        # Used by the uninstaller to remove all POS data. Connect to the server
        # (not the database itself) so the DROP succeeds even with open handles,
        # using the credentials the installer saved in .env.
        try:
            from config import Config, server_uri
            engine = sqlalchemy.create_engine(server_uri(), connect_args={"connect_timeout": 5})
            with engine.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{Config.DB_NAME}`"))
                conn.commit()
            engine.dispose()
            print("DB_DROP_OK")
            sys.exit(0)
        except Exception as e:
            print(f"DB_DROP_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)

    if args.init_db:
        if not args.config:
            print("ERROR: --init-db requires --config <file>", file=sys.stderr)
            sys.exit(2)
        try:
            init_db_from_config(args.config)
            print("DB_INIT_OK")
            sys.exit(0)
        except Exception as e:
            print(f"DB_INIT_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        serve()


if __name__ == "__main__":
    main()
