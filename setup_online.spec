# -*- mode: python ; coding: utf-8 -*-
# Builds the ONLINE bootstrapper installer: setup\ZTPOS-Online-Setup.exe (onefile).
#
# It does NOT bundle the app payload. At run time the wizard reads GitHub's
# "latest release" for the configured repo, downloads the ZTPOS-<version>.zip
# asset, verifies its sha256, and installs that. The result is a tiny,
# version-independent installer you can run on any machine to pull the newest
# POS straight from GitHub. The same exe, run with --update, applies updates.
#
#   Build it via build-online-setup.bat (no need to compile the app exes first —
#   they are downloaded at install time).
#
import sys; sys.path.insert(0, SPECPATH)  # make winversion.py (beside the spec) importable

from winversion import version_info

# No app_payload here — only the icon (for the window) and VERSION (a harmless
# fallback; the real installed version is whatever GitHub serves).
datas = [
    ("assets/icon.ico", "assets"),
    ("VERSION", "."),
]

a = Analysis(
    ["installer_app/setup_wizard.py"],
    pathex=["installer_app"],
    binaries=[],
    datas=datas,
    hiddenimports=["wizard_ui", "uninstall", "pymysql"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ZTPOS-Online-Setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,            # GUI wizard
    uac_admin=True,           # request elevation (needed to install MariaDB)
    disable_windowed_traceback=False,
    icon="assets/icon.ico",   # brands ZTPOS-Online-Setup.exe
    version=version_info("ZTPOS-Online-Setup", "ZT POS Online Installer"),
)
