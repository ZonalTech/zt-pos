"""ZT POS — uninstaller.

Compiled to Uninstall.exe and installed next to POS.exe. It is what the
Windows "Apps & features" / Control Panel "Uninstall" button runs (the
installer registers it there), so the POS removes like any normal Windows app.

It will:

  1. Drop the pos_db database (all sales + product data). On by default — an
     uninstall removes both the app and its data — but the wizard lets the
     user keep the data if they untick it.
  2. Optionally uninstall the MariaDB server too (off by default, since other
     programs on the PC may rely on it).
  3. Remove the Start-Menu and Desktop shortcuts.
  4. Remove the Add/Remove Programs registry entry.
  5. Delete the install folder (Program Files\\ZTPOS) — scheduled to run
     just after this process exits, since Uninstall.exe lives inside it.

Modes:
  Uninstall.exe                          Show the GUI wizard.
  Uninstall.exe /silent                  No GUI; remove app, keep the database.
  Uninstall.exe /silent /purge-data      No GUI; also drop the database.
  Uninstall.exe /silent /purge-data /remove-mariadb
                                         No GUI; remove everything incl. MariaDB.
"""
import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
import threading

APP_NAME = "ZT POS"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ZTPOS"
APP_EXE = "POS.exe"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def install_dir():
    """The folder this uninstaller lives in — i.e. where the app is installed."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _hidden_startupinfo():
    """A STARTUPINFO that force-hides any window a child process would create.

    CREATE_NO_WINDOW alone can still briefly flash a console on some Windows
    builds — and combining it with DETACHED_PROCESS (as the self-delete does)
    is technically an invalid flag pairing. STARTF_USESHOWWINDOW + SW_HIDE
    makes the "no window" explicit and reliable. Harmless on non-Windows.
    """
    si = None
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return si


def _unquote_env(value):
    """Reverse installer_app.setup_wizard._env_quote for a .env value."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def read_env(directory):
    """Minimal .env reader for the DB credentials the installer saved."""
    env = {}
    path = os.path.join(directory, ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = _unquote_env(val)
    except OSError:
        pass
    return env


# --------------------------------------------------------------------------
# Uninstall steps
# --------------------------------------------------------------------------
def drop_database(directory, log):
    """Drop the pos_db database using the credentials saved in .env."""
    env = read_env(directory)
    name = env.get("DB_NAME", "pos_db")
    try:
        import pymysql
    except Exception as e:  # noqa: BLE001
        log(f"  (could not load the database driver: {e}; data left in place)")
        return
    try:
        conn = pymysql.connect(
            host=env.get("DB_HOST", "127.0.0.1"),
            port=int(env.get("DB_PORT", "3306") or 3306),
            user=env.get("DB_USER", "root"),
            password=env.get("DB_PASSWORD", ""),
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
            conn.commit()
        finally:
            conn.close()
        log(f"Dropped the '{name}' database.")
    except Exception as e:  # noqa: BLE001
        log(f"  (could not drop the database: {e}; you may remove it manually)")


def remove_shortcuts(log):
    targets = [
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"),
                     "Desktop", f"{APP_NAME}.lnk"),
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                     "Microsoft", "Windows", "Start Menu", "Programs",
                     f"{APP_NAME}.lnk"),
    ]
    for lnk in targets:
        try:
            if os.path.isfile(lnk):
                os.remove(lnk)
                log(f"Removed shortcut: {os.path.basename(lnk)}")
        except OSError as e:
            log(f"  (could not remove {os.path.basename(lnk)}: {e})")


def remove_registry_entry(log):
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
        log("Removed the Add/Remove Programs entry.")
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"  (could not remove the registry entry: {e})")


def schedule_self_delete(directory, log):
    """Delete the install folder after this process exits.

    Uninstall.exe is inside `directory`, so it can't delete itself while
    running. Write a detached batch that waits for THIS process (by PID) to
    exit — however long the user lingers on the Finish page — then removes the
    folder and deletes itself.
    """
    safe = directory.replace('"', '')
    bat = os.path.join(tempfile.gettempdir(), "ztpos_cleanup.bat")
    # Retry rmdir until the folder is gone: it can't remove the still-running
    # Uninstall.exe inside it, so it fails until we've exited, then succeeds.
    # (More reliable than matching our PID in tasklist output, which can spin
    # forever on PID reuse.)
    script = (
        "@echo off\r\n"
        ":retry\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        f'rmdir /s /q "{safe}" 2>nul\r\n'
        f'if exist "{safe}" goto retry\r\n'
        'del "%~f0"\r\n'
    )
    try:
        with open(bat, "w", encoding="ascii", errors="replace") as fh:
            fh.write(script)
        subprocess.Popen(
            ["cmd.exe", "/c", bat],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | _no_window(),
            startupinfo=_hidden_startupinfo(),
            close_fds=True,
        )
        log("Scheduled removal of the application files.")
    except Exception as e:  # noqa: BLE001
        log(f"  (could not schedule folder removal: {e})")


def _find_mariadb_uninstall():
    """Locate MariaDB's own Add/Remove Programs entry. Returns
    (quiet_uninstall_string, msi_product_code) or None."""
    import winreg
    bases = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root, base in bases:
        try:
            bkey = winreg.OpenKey(root, base)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(bkey, i)
                except OSError:
                    break
                i += 1
                try:
                    sk = winreg.OpenKey(bkey, sub)
                    name = winreg.QueryValueEx(sk, "DisplayName")[0]
                except OSError:
                    continue
                if "mariadb" not in str(name).lower():
                    continue
                quiet = None
                for value in ("QuietUninstallString", "UninstallString"):
                    try:
                        quiet = winreg.QueryValueEx(sk, value)[0]
                        break
                    except OSError:
                        continue
                product = sub if sub.startswith("{") else None
                return quiet, product
        finally:
            winreg.CloseKey(bkey)
    return None


def uninstall_mariadb(log):
    """Stop the service and uninstall the MariaDB server via its MSI."""
    entry = _find_mariadb_uninstall()
    if not entry:
        log("  (MariaDB is not installed via MSI; leaving it in place)")
        return
    quiet, product = entry
    log("Stopping the MariaDB service…")
    for svc in ("MariaDB", "MySQL"):
        subprocess.run(["net", "stop", svc],
                       creationflags=_no_window(), startupinfo=_hidden_startupinfo())
    log("Uninstalling the MariaDB server…")
    try:
        if product:
            rc = subprocess.run(
                ["msiexec.exe", "/x", product, "/qn", "/norestart"],
                creationflags=_no_window(), startupinfo=_hidden_startupinfo()).returncode
        else:
            rc = subprocess.run(quiet,
                                creationflags=_no_window(),
                                startupinfo=_hidden_startupinfo()).returncode
        if rc == 0:
            log("Removed the MariaDB server.")
        else:
            log(f"  (MariaDB uninstall returned code {rc}; remove it manually "
                "from Apps & features if needed)")
    except Exception as e:  # noqa: BLE001
        log(f"  (could not uninstall MariaDB: {e})")


def run_uninstall(purge_data, remove_mariadb, log):
    directory = install_dir()
    log(f"Uninstalling {APP_NAME} from {directory}…")
    # Drop the database while the server is still up, before any MariaDB removal.
    if purge_data:
        drop_database(directory, log)
    else:
        log("Keeping the database (sales + product data preserved).")
    if remove_mariadb:
        uninstall_mariadb(log)
    remove_shortcuts(log)
    remove_registry_entry(log)
    schedule_self_delete(directory, log)
    log("")
    log("✓ Uninstall complete.")


# --------------------------------------------------------------------------
# GUI — a multi-step wizard (Welcome → Options → Uninstall → Finish)
# --------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    from wizard_ui import Wizard, Page, WHITE, MUTED, make_log_box, append_log

    class WelcomePage(Page):
        title = f"Uninstall {APP_NAME}"
        subtitle = "This will remove the POS application and its data."

        def build(self):
            tk.Label(
                self.frame, bg=WHITE, justify="left", anchor="nw",
                wraplength=540,
                text=("The wizard will remove:\n\n"
                      "    •  The POS application and its shortcuts.\n\n"
                      "    •  The pos_db database (all sales and product "
                      "data).\n\n"
                      "    •  The Windows 'Apps & features' entry.\n\n"
                      "On the next screen you can choose whether to also "
                      "remove the MariaDB server. Administrator rights are "
                      "required. Click Next to continue."),
            ).pack(fill="both", expand=True)

    class OptionsPage(Page):
        title = "What to remove"
        subtitle = "The application is always removed. Choose what else to delete."

        def build(self):
            self.purge = tk.BooleanVar(value=True)
            self.mariadb = tk.BooleanVar(value=False)
            tk.Checkbutton(
                self.frame, bg=WHITE, variable=self.purge, justify="left",
                text="Delete the database and all data (pos_db).\n"
                     "Sales and products cannot be recovered.").pack(
                anchor="w", pady=(2, 12))
            tk.Checkbutton(
                self.frame, bg=WHITE, variable=self.mariadb, justify="left",
                text="Also uninstall the MariaDB server.\n"
                     "Only do this if no other program on this PC uses it.").pack(
                anchor="w")
            tk.Label(self.frame, bg=WHITE, fg=MUTED, justify="left",
                     wraplength=540,
                     text="\nThe application files in Program Files are removed "
                          "automatically once the wizard closes.").pack(
                anchor="w", pady=(16, 0))

        def next_text(self):
            return "Uninstall"

        def validate(self):
            if not is_admin():
                messagebox.showwarning(
                    "Administrator required",
                    "Please run the uninstaller as Administrator so it can "
                    "remove the app from Program Files.")
                return False
            warn = "Remove " + APP_NAME + "?"
            if self.purge.get():
                warn += "\n\nAll sales and product data will be permanently deleted."
            if self.mariadb.get():
                warn += "\n\nThe MariaDB server will also be uninstalled."
            if not messagebox.askyesno("Confirm uninstall", warn):
                return False
            self.wizard.shared.update(purge=self.purge.get(),
                                      mariadb=self.mariadb.get())
            return True

    class UninstallPage(Page):
        title = "Uninstalling"
        subtitle = "Please wait while the POS is removed…"

        def build(self):
            self.running = False
            self.done = False
            self.progress = ttk.Progressbar(self.frame, mode="indeterminate")
            self.progress.pack(fill="x", pady=(2, 8))
            self.log_box = make_log_box(self.frame)
            self.log_box.pack(fill="both", expand=True)

        def _log(self, msg):
            self.wizard.after(lambda: append_log(self.log_box, msg))

        def on_enter(self):
            if self.done:
                self.wizard.set_next_enabled(True)
                return
            if self.running:
                return
            self.running = True
            self.progress.start(12)
            self.wizard.set_next_enabled(False)
            self.wizard.set_back_enabled(False)
            self.wizard.set_cancel_enabled(False)
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            s = self.wizard.shared
            try:
                run_uninstall(s.get("purge", True), s.get("mariadb", False),
                              self._log)
            except Exception as e:  # noqa: BLE001
                self._log(f"\nERROR: {e}")
            self.done = True
            self.running = False

            def finish_ui():
                self.progress.stop()
                self.progress.configure(mode="determinate", value=100)
                self.wizard.set_next_enabled(True)
            self.wizard.after(finish_ui)

        def show_back(self):
            return False

        def show_cancel(self):
            return not self.done

    class FinishPage(Page):
        title = f"{APP_NAME} has been removed"
        subtitle = "Uninstall finished."

        def build(self):
            tk.Label(self.frame, bg=WHITE, justify="left", anchor="nw",
                     wraplength=540,
                     text="The application has been uninstalled. The program "
                          "files are deleted automatically once this window "
                          "closes.").pack(anchor="w")

        def next_text(self):
            return "Finish"

        def show_back(self):
            return False

        def show_cancel(self):
            return False

    icon = resource_path(os.path.join("assets", "icon.ico"))
    wiz = Wizard(f"{APP_NAME} — Uninstall", icon_path=icon, width=620, height=500)
    wiz.add_page(WelcomePage)
    wiz.add_page(OptionsPage)
    wiz.add_page(UninstallPage)
    wiz.add_page(FinishPage)
    wiz.start()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ZT POS uninstaller")
    parser.add_argument("--silent", dest="silent", action="store_true",
                        help="Uninstall without the GUI.")
    parser.add_argument("--purge-data", dest="purge", action="store_true",
                        help="Also drop the pos_db database.")
    parser.add_argument("--remove-mariadb", dest="mariadb", action="store_true",
                        help="Also uninstall the MariaDB server.")
    # Normalize Windows-style /silent switches to --silent, and tolerate any
    # extra switches the OS may append.
    norm = [("--" + a[1:]) if a.startswith("/") else a for a in sys.argv[1:]]
    args, _ = parser.parse_known_args(norm)

    if args.silent:
        run_uninstall(args.purge, args.mariadb, print)
    else:
        run_gui()


if __name__ == "__main__":
    main()
