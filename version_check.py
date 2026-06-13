"""
Docker Hub version check.

Checks Docker Hub for the highest semantic version tag and compares it with
the running application version. Results are cached in memory.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DOCKER_NAMESPACE = "ronhoebe"
DOCKER_IMAGE = "camping_trip"
DOCKER_HUB_TAGS_URL = (
    "https://registry.hub.docker.com/v2/repositories/"
    f"{DOCKER_NAMESPACE}/{DOCKER_IMAGE}/tags/?page_size=100&ordering=last_updated"
)
DOCKER_HUB_PAGE = f"https://hub.docker.com/r/{DOCKER_NAMESPACE}/{DOCKER_IMAGE}/tags"

CHECK_INTERVAL = 24 * 60 * 60
REQUEST_TIMEOUT = 10

_cache = {
    "latest_version": None,
    "last_check": 0,
    "checking": False,
}
_lock = threading.Lock()

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_semver(version_str):
    if not version_str:
        return None
    match = _SEMVER_RE.match(version_str.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_update_available(current, latest):
    current_version = _parse_semver(current)
    latest_version = _parse_semver(latest)
    if current_version is None or latest_version is None:
        return False
    return latest_version > current_version


def _fetch_latest_version():
    try:
        request = urllib.request.Request(
            DOCKER_HUB_TAGS_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "camping-trip-version-check/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        best = None
        for tag_info in data.get("results", []):
            parsed = _parse_semver(tag_info.get("name", ""))
            if parsed is not None and (best is None or parsed > best):
                best = parsed

        if best:
            return f"v{best[0]}.{best[1]}.{best[2]}"
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        logger.debug("Version check failed: %s", exc)
        return None
    except Exception as exc:
        logger.debug("Unexpected error during version check: %s", exc)
        return None


def _background_check():
    try:
        latest = _fetch_latest_version()
        with _lock:
            if latest:
                _cache["latest_version"] = latest
            _cache["last_check"] = time.time()
            _cache["checking"] = False
    except Exception:
        with _lock:
            _cache["checking"] = False


def get_latest_version():
    with _lock:
        age = time.time() - _cache["last_check"]
        if age < CHECK_INTERVAL:
            return _cache["latest_version"]
        if _cache["checking"]:
            return _cache["latest_version"]
        _cache["checking"] = True

    thread = threading.Thread(target=_background_check, daemon=True)
    thread.start()
    return _cache["latest_version"]
