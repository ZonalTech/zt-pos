"""In-app update detection for the running POS.

This module only lets the *running* POS detect that a newer GitHub release
exists, so the UI can show an "Update available" notice. Applying the update is
done by re-launching the installer in --update mode (it elevates, downloads the
latest release, swaps the files, and relaunches). This module is deliberately
dependency-free (urllib + json) and never raises — callers get a plain dict.

Version sources, in order:
  1. version.txt next to POS.exe (written at install time)
  2. the bundled VERSION file (dev / first run)
  3. DEFAULT_VERSION
"""
import json
import os
import urllib.error
import urllib.request

from config import Config, app_dir, resource_path

DEFAULT_VERSION = "1.0.0"


def installed_version():
    for path in (os.path.join(app_dir(), "version.txt"), resource_path("VERSION")):
        try:
            with open(path, encoding="utf-8") as fh:
                v = fh.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return DEFAULT_VERSION


def parse_version(s):
    """'1.10.2' -> (1, 10, 2) for ordered comparison; tolerant of junk/'v'."""
    out = []
    for part in str(s).strip().lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def _normalize(data):
    """GitHub 'latest release' object OR a plain manifest -> {version, notes}."""
    if isinstance(data, dict) and "tag_name" in data:
        return {
            "version": str(data.get("tag_name", "")).strip().lstrip("vV"),
            "notes": data.get("body") or "",
        }
    return {"version": data.get("version"), "notes": data.get("notes", "")}


def check():
    """Return {current, latest, available, notes, configured, error}."""
    url = (Config.UPDATE_URL or "").strip()
    cur = installed_version()
    out = {
        "current": cur, "latest": None, "available": False,
        "notes": "", "configured": bool(url) and "OWNER/REPO" not in url,
        "error": None,
    }
    if not out["configured"]:
        return out
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ZTPOS-Updater",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        m = _normalize(data)
        out["latest"] = m["version"]
        out["notes"] = m["notes"]
        out["available"] = bool(m["version"]) and \
            parse_version(m["version"]) > parse_version(cur)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return out
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — offline / rate-limited: just report it
        out["error"] = str(e)
    return out
