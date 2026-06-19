"""Package an ZT POS update: a versioned .zip of the app binaries + a manifest.

The app detects a newer release (updates.py reads GitHub's "latest release"),
and applying it re-launches the installer in --update mode, which downloads
this .zip and copies the files over the install folder. This script produces
the .zip + manifest that ship as release assets; the installer (and the in-app
check) read GitHub's release API directly.

Usage:
    python make_update.py --source setup/build/app \
        --base-url https://downloads.example.com/ztpos
    python make_update.py --source "C:/Program Files/ZTPOS" --version 1.1.1 \
        --base-url http://127.0.0.1:8000 --notes "Test build"

Outputs (default ./release):
    release/ZTPOS-<version>.zip     POS.exe
    release/manifest.json           {version, url, notes, sha256}

The zip deliberately contains ONLY the app executables — never .env (which holds
the user's DB credentials) or version.txt (the updater writes that itself), so
applying an update can't clobber a machine's configuration.
"""
import argparse
import hashlib
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
# Only POS.exe ships in the payload. The installer (dropped as ZTPOS-Setup.exe)
# applies updates (--update) and uninstalls (--uninstall) itself, so neither an
# Update.exe nor an Uninstall.exe is shipped — keeping the download small.
APP_FILES = ["POS.exe"]


def read_version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
        return fh.read().strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Package an ZT POS update.")
    ap.add_argument("--source", required=True,
                    help="Folder holding the freshly-built POS.exe / "
                         "Uninstall.exe.")
    ap.add_argument("--out", default=os.path.join(ROOT, "release"),
                    help="Output folder for the zip + manifest (default ./release).")
    ap.add_argument("--base-url", default=os.getenv("UPDATE_BASE_URL", ""),
                    help="Public base URL where the zip will be hosted. The "
                         "manifest 'url' becomes <base-url>/ZTPOS-<version>.zip.")
    ap.add_argument("--version", default=None,
                    help="Version string (default: read from the VERSION file).")
    ap.add_argument("--notes", default="", help="Release notes for the manifest.")
    args = ap.parse_args()

    version = args.version or read_version()
    os.makedirs(args.out, exist_ok=True)

    present = [f for f in APP_FILES if os.path.isfile(os.path.join(args.source, f))]
    if "POS.exe" not in present:
        raise SystemExit(f"POS.exe not found in {args.source!r}. Build first.")

    zip_name = f"ZTPOS-{version}.zip"
    zip_path = os.path.join(args.out, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in present:
            zf.write(os.path.join(args.source, f), arcname=f)

    base = args.base_url.rstrip("/")
    url = f"{base}/{zip_name}" if base else f"REPLACE_WITH_HOST_URL/{zip_name}"
    manifest = {
        "version": version,
        "url": url,
        "notes": args.notes,
        "sha256": sha256(zip_path),
    }
    manifest_path = os.path.join(args.out, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"Packaged update {version}: {', '.join(present)}")
    print(f"  zip:      {zip_path}")
    print(f"  manifest: {manifest_path}")
    print(f"  url:      {url}")
    if not base:
        print("  WARNING: no --base-url/UPDATE_BASE_URL set; edit manifest 'url' "
              "before publishing.")


if __name__ == "__main__":
    main()
