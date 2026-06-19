# -*- coding: utf-8 -*-
"""Shared Windows VERSIONINFO resource for the PyInstaller .spec files.

Every exe we ship (POS, Update, Uninstall, ZTPOS-Setup) embeds the same
company branding, so File -> Properties -> Details reads "Zonal Tech" and the
version is pulled from the repo's single source of truth (VERSION).

IMPORTANT: this only populates the file's *metadata*. The "Publisher" line in
the Windows UAC / SmartScreen popup that appears when the installer asks for
admin rights comes solely from an Authenticode code-signing signature -- it
stays "Unknown" until the exe is signed with a certificate issued to
"Zonal Tech" (see the optional signing step in build-setup.bat).
"""
import os

from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)

COMPANY = "Zonal Tech"
PRODUCT = "ZT POS"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _version_tuple():
    """Read VERSION ('1.1.0') -> (1, 1, 0, 0); fall back to zeros on error."""
    try:
        with open(os.path.join(_HERE, "VERSION"), encoding="utf-8") as fh:
            nums = [int(p) for p in fh.read().strip().split(".")][:4]
    except (OSError, ValueError):
        nums = []
    nums += [0] * (4 - len(nums))
    return tuple(nums[:4])


def version_info(exe_name, description):
    """Build the VSVersionInfo object branding one exe as Zonal Tech."""
    v = _version_tuple()
    vstr = ".".join(str(n) for n in v)
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=v, prodvers=v,
            mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [  # US English, Unicode (CP 1200)
                    StringStruct("CompanyName", COMPANY),
                    StringStruct("FileDescription", description),
                    StringStruct("FileVersion", vstr),
                    StringStruct("InternalName", exe_name),
                    StringStruct("OriginalFilename", exe_name + ".exe"),
                    StringStruct("ProductName", PRODUCT),
                    StringStruct("ProductVersion", vstr),
                    StringStruct("LegalCopyright", "Copyright (c) " + COMPANY),
                ]),
            ]),
            VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
        ],
    )
