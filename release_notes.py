"""Print the CHANGELOG.md section for a version — used as GitHub release notes.

    python release_notes.py            # section for the current VERSION
    python release_notes.py 1.2.0      # section for an explicit version

Outputs the body under "## [<version>] ..." up to the next "## [" heading.
Falls back to a one-line message if the section isn't found.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def read_version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
        return fh.read().strip()


def section_for(version):
    path = os.path.join(ROOT, "CHANGELOG.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    # Match "## [1.2.0] - 2026-..." then capture until the next "## [" heading.
    pattern = re.compile(
        r"^##\s*\[%s\][^\n]*\n(.*?)(?=^##\s*\[|\Z)" % re.escape(version),
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else read_version()
    body = section_for(version)
    if not body:
        body = f"ZT POS v{version}."
    print(body)


if __name__ == "__main__":
    main()
