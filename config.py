"""Configuration for Camping Trip Planner."""
import os
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_secret_key():
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    key = os.urandom(32).hex()
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _read_optional_key_file(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


SECRET_KEY = _get_secret_key()
DEBUG = os.environ.get("DEBUG", "False").lower() == "true"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.environ.get("HTTPS_ENABLED", "false").lower() == "true"
PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

DATA_FOLDER = os.environ.get("DATA_FOLDER", os.path.join(BASE_DIR, "data"))
DATABASE_PATH = os.path.join(DATA_FOLDER, "camping_trip.db")
UPLOAD_FOLDER = os.path.join(DATA_FOLDER, "uploads")
TRACK_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, "tracks")

ITEMS_PER_PAGE = 24
MAX_CONTENT_LENGTH = 64 * 1024 * 1024

LOGIN_FAIL_THRESHOLD = int(os.environ.get("LOGIN_FAIL_THRESHOLD", "10"))
LOGIN_FAIL_WINDOW = int(os.environ.get("LOGIN_FAIL_WINDOW", "15"))
LOGIN_BAN_DURATION = int(os.environ.get("LOGIN_BAN_DURATION", "30"))

ROUTE_PROVIDER = os.environ.get("ROUTE_PROVIDER", "graphhopper").lower()
GRAPHHOPPER_BASE_URL = os.environ.get("GRAPHHOPPER_BASE_URL", "https://graphhopper.com/api/1")
GRAPHHOPPER_API_KEY_FILE = os.environ.get("GRAPHHOPPER_API_KEY_FILE", os.path.join(BASE_DIR, "graphhopper.key"))
GRAPHHOPPER_API_KEY = os.environ.get("GRAPHHOPPER_API_KEY", "") or _read_optional_key_file(GRAPHHOPPER_API_KEY_FILE)
GRAPHHOPPER_LEG_DELAY_SECONDS = float(os.environ.get("GRAPHHOPPER_LEG_DELAY_SECONDS", "8"))
GRAPHHOPPER_429_RETRY_SECONDS = float(os.environ.get("GRAPHHOPPER_429_RETRY_SECONDS", "65"))
GRAPHHOPPER_429_RETRIES = int(os.environ.get("GRAPHHOPPER_429_RETRIES", "1"))
ROUTE_LEG_CACHE_HOURS = float(os.environ.get("ROUTE_LEG_CACHE_HOURS", "24"))
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
ROUTE_TIMEOUT_SECONDS = int(os.environ.get("ROUTE_TIMEOUT_SECONDS", "20"))

GPXFEED_REPO = os.environ.get("GPXFEED_REPO", "GpxFeed/campgrounds")
GPXFEED_BRANCH = os.environ.get("GPXFEED_BRANCH", "master")
GPXFEED_FOLDER = os.environ.get("GPXFEED_FOLDER", "gpx-stripped")
GPXFEED_AUTO_UPDATE = os.environ.get("GPXFEED_AUTO_UPDATE", "true").lower() == "true"
GPXFEED_UPDATE_INTERVAL_HOURS = int(os.environ.get("GPXFEED_UPDATE_INTERVAL_HOURS", "24"))
GPXFEED_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("GPXFEED_REQUEST_TIMEOUT_SECONDS", "30"))

DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "change-me-please")
DEFAULT_ADMIN_EMAIL = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@example.local")
