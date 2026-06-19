"""Bump the app version and start a new changelog section.

Usage:
    python bump_version.py patch     # 1.1.0 -> 1.1.1   (default)
    python bump_version.py minor     # 1.1.0 -> 1.2.0
    python bump_version.py major     # 1.1.0 -> 2.0.0
    python bump_version.py 1.4.2     # set an explicit version

It updates the VERSION file and promotes the "## [Unreleased]" section of
CHANGELOG.md to the new version (with today's date), leaving a fresh empty
Unreleased section on top. The build then stamps VERSION into the bundle.
"""
import datetime
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(ROOT, "VERSION")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")


def read_version():
    with open(VERSION_FILE, encoding="utf-8") as fh:
        return fh.read().strip()


def next_version(current, part):
    if re.fullmatch(r"\d+\.\d+\.\d+", part):
        return part  # explicit version
    major, minor, patch = (int(x) for x in current.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"Unknown bump '{part}'. Use major | minor | patch | X.Y.Z")


def update_changelog(new_version):
    """Rename the Unreleased section to the new version and re-open Unreleased."""
    today = datetime.date.today().isoformat()
    with open(CHANGELOG, encoding="utf-8") as fh:
        text = fh.read()

    released = f"## [{new_version}] - {today}"
    if "## [Unreleased]" in text:
        # Keep an empty Unreleased section above the freshly-released one.
        text = text.replace(
            "## [Unreleased]",
            f"## [Unreleased]\n\n{released}",
            1,
        )
    else:
        # No Unreleased section; insert a new version block after the header.
        marker = "## ["
        idx = text.find(marker)
        block = f"{released}\n### Changed\n- _Describe changes here._\n\n"
        text = text[:idx] + block + text[idx:] if idx != -1 else text + "\n" + block

    with open(CHANGELOG, "w", encoding="utf-8") as fh:
        fh.write(text)


def main():
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"
    current = read_version()
    new = next_version(current, part)
    with open(VERSION_FILE, "w", encoding="utf-8") as fh:
        fh.write(new + "\n")
    if os.path.isfile(CHANGELOG):
        update_changelog(new)
    print(f"Version: {current} -> {new}")
    print("Remember to fill in the changelog, then run release-github.bat to release.")


if __name__ == "__main__":
    main()
