"""Application configuration.

Reads settings from environment variables / a .env file. When the app is
compiled with PyInstaller (frozen), the .env lives next to POS.exe so the
installer can write the database credentials it collected during setup.
"""
import os
import sys
from dotenv import load_dotenv


def app_dir():
    """Directory that holds the app + its .env.

    Frozen (PyInstaller onefile): the folder containing POS.exe.
    Source: the folder containing this file.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """Locate a bundled data file (templates/, static/).

    PyInstaller unpacks data files to sys._MEIPASS at runtime.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# Load .env from beside the exe/source (override so installer values win).
load_dotenv(os.path.join(app_dir(), ".env"), override=True)


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

    # --- Local MariaDB connection (the app's runtime user) ---
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = os.getenv("DB_PORT", "3306")
    DB_NAME = os.getenv("DB_NAME", "pos_db")

    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        "?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_recycle": 280, "pool_pre_ping": True}

    # --- Business settings ---
    CURRENCY = os.getenv("CURRENCY", "KES")
    STORE_NAME = os.getenv("STORE_NAME", "My Shop")
    TAX_RATE = float(os.getenv("TAX_RATE", "0"))  # e.g. 0.16 for 16% VAT

    # --- Customer loyalty ---
    # Spend this many base-currency units to earn 1 loyalty point.
    # e.g. 100 => every 100 KES of a sale's total earns the customer 1 point.
    LOYALTY_KES_PER_POINT = float(os.getenv("LOYALTY_KES_PER_POINT", "100"))

    # --- Product images / uploads ---
    # Uploaded product photos live next to the exe/source (NOT inside the
    # read-only PyInstaller bundle), so they survive updates and are writable.
    UPLOAD_DIR = os.path.join(app_dir(), "uploads", "products")
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    # Cap upload (and request) size. Applies app-wide via Flask.
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024

    # --- Multi-currency ---
    # The accounting/base currency: all stored totals and reports use this.
    BASE_CURRENCY = CURRENCY
    # Extra currencies a cashier may accept at the till (comma-separated).
    # The base currency is always available regardless of this list.
    SUPPORTED_CURRENCIES = [
        c.strip().upper()
        for c in os.getenv("SUPPORTED_CURRENCIES", "USD,EUR,GBP,UGX,TZS").split(",")
        if c.strip()
    ]
    # Live FX source. "{base}" is replaced with the base currency code.
    # open.er-api.com is free and needs no API key.
    RATE_API_URL = os.getenv(
        "RATE_API_URL", "https://open.er-api.com/v6/latest/{base}"
    )
    # How long a cached live rate stays fresh before a refresh is attempted (hours).
    # Kept short so the Currencies page (which polls in the background) and the
    # till stay close to real time. Free FX sources update ~daily, so re-fetching
    # more often is harmless and just returns the latest published rate.
    RATE_REFRESH_HOURS = float(os.getenv("RATE_REFRESH_HOURS", "1"))

    # --- Updates ---
    # Updates are published as GitHub Releases: the release tag is the version
    # (v1.2.0) and a ZTPOS-<version>.zip is attached. GitHub's "latest release"
    # API always points at the newest one, so nothing is hand-edited per release.
    GITHUB_REPO = os.getenv("GITHUB_REPO", "ZonalTechnologies/zt-pos")
    # The running app uses this to detect a newer release (in-app notice), then
    # applies it by re-launching the installer (ZTPOS-Setup.exe --update).
    # Override with an explicit UPDATE_URL in the environment / .env; an empty
    # value falls back to the GitHub releases URL above.
    UPDATE_URL = os.getenv("UPDATE_URL") or \
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

    # Server the compiled app listens on.
    HOST = os.getenv("POS_HOST", "127.0.0.1")
    PORT = int(os.getenv("POS_PORT", "5000"))


def server_uri():
    """URI without a database name — used to CREATE DATABASE on first run."""
    return (
        f"mysql+pymysql://{Config.DB_USER}:{Config.DB_PASSWORD}"
        f"@{Config.DB_HOST}:{Config.DB_PORT}/?charset=utf8mb4"
    )
