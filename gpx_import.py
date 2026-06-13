"""GPX import and GpxFeed synchronization helpers for campground data."""
import io
import json
import logging
from datetime import datetime, timedelta
import zipfile
from xml.etree import ElementTree

import requests

import config
from database import get_db


GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}
GITHUB_API = "https://api.github.com"
CAMPGROUND_FILE_SUFFIXES = ("-campsites.gpx", "-caravansites.gpx")


def _find_text(node, names):
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
        child = node.find(f"gpx:{name}", GPX_NS)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _iter_gpx_payloads(filename, payload):
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for item in archive.infolist():
                if item.filename.lower().endswith(".gpx"):
                    yield item.filename, archive.read(item)
    else:
        yield filename, payload


def _source_id(source_name, waypoint, name, lat, lon):
    extensions = waypoint.find("gpx:extensions", GPX_NS) or waypoint.find("extensions")
    if extensions is not None:
        text = "".join(extensions.itertext()).strip()
        if text:
            return f"{source_name}:{text[:180]}"
    return f"{source_name}:{name}:{round(lat, 6)}:{round(lon, 6)}"


def _import_gpx_payloads(payloads, source="gpx"):
    imported = 0
    skipped = 0
    errors = []

    with get_db() as conn:
        for source_name, gpx_payload in payloads:
            try:
                root = ElementTree.fromstring(gpx_payload)
            except ElementTree.ParseError as exc:
                errors.append(f"{source_name}: {exc}")
                continue

            waypoints = root.findall(".//gpx:wpt", GPX_NS) or root.findall(".//wpt")
            for waypoint in waypoints:
                try:
                    lat = float(waypoint.attrib["lat"])
                    lon = float(waypoint.attrib["lon"])
                except (KeyError, TypeError, ValueError):
                    skipped += 1
                    continue

                name = _find_text(waypoint, ["name"]) or "Unnamed campground"
                description = _find_text(waypoint, ["desc", "cmt"])
                link = waypoint.find("gpx:link", GPX_NS) or waypoint.find("link")
                website = link.attrib.get("href", "") if link is not None else ""
                sid = _source_id(source_name, waypoint, name, lat, lon)
                raw_data = json.dumps({"source_file": source_name, "name": name, "description": description})

                try:
                    cursor = conn.execute("""
                        INSERT OR IGNORE INTO imported_campgrounds
                            (source, source_id, name, latitude, longitude, description, website, raw_data, source_file)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (source, sid, name, lat, lon, description, website, raw_data, source_name))
                    if cursor.rowcount:
                        imported += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors.append(f"{name}: {exc}")

    return {"imported": imported, "skipped": skipped, "errors": errors}


def import_gpx_file(filename, payload):
    return _import_gpx_payloads(_iter_gpx_payloads(filename, payload), source="gpx")


def _github_headers():
    return {"User-Agent": "camping-trip-planner"}


def _get_latest_commit_sha():
    url = f"{GITHUB_API}/repos/{config.GPXFEED_REPO}/commits/{config.GPXFEED_BRANCH}"
    response = requests.get(url, headers=_github_headers(), timeout=config.GPXFEED_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()["sha"]


def _list_gpxfeed_files():
    url = f"{GITHUB_API}/repos/{config.GPXFEED_REPO}/contents/{config.GPXFEED_FOLDER}"
    response = requests.get(
        url,
        headers=_github_headers(),
        params={"ref": config.GPXFEED_BRANCH},
        timeout=config.GPXFEED_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    files = response.json()
    return [
        item
        for item in files
        if item.get("type") == "file"
        and item.get("name", "").lower().endswith(CAMPGROUND_FILE_SUFFIXES)
        and item.get("download_url")
    ]


def _download_file(item):
    response = requests.get(
        item["download_url"],
        headers=_github_headers(),
        timeout=config.GPXFEED_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return item["path"], response.content


def get_gpxfeed_status():
    with get_db() as conn:
        settings = {
            row["key"]: row["value"]
            for row in conn.execute("""
                SELECT key, value FROM site_settings
                WHERE key LIKE 'gpxfeed_%'
            """)
        }
        count = conn.execute("""
            SELECT COUNT(*) AS count FROM imported_campgrounds
            WHERE source = 'gpxfeed'
        """).fetchone()["count"]
    return {
        "commit_sha": settings.get("gpxfeed_commit_sha", ""),
        "last_checked_at": settings.get("gpxfeed_last_checked_at", ""),
        "last_imported_at": settings.get("gpxfeed_last_imported_at", ""),
        "last_status": settings.get("gpxfeed_last_status", "Not checked yet"),
        "file_count": int(settings.get("gpxfeed_file_count", "0") or 0),
        "campground_count": count,
    }


def _set_status(conn, **values):
    for key, value in values.items():
        conn.execute(
            "INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
            (f"gpxfeed_{key}", str(value)),
        )


def update_gpxfeed_if_needed(force=False):
    """Download and import the GpxFeed campground dataset when the repo changed."""
    now = datetime.utcnow()
    with get_db() as conn:
        settings = {
            row["key"]: row["value"]
            for row in conn.execute("""
                SELECT key, value FROM site_settings
                WHERE key LIKE 'gpxfeed_%'
            """)
        }
        if not force and settings.get("gpxfeed_last_checked_at"):
            try:
                last_checked = datetime.fromisoformat(settings["gpxfeed_last_checked_at"])
                if now - last_checked < timedelta(hours=config.GPXFEED_UPDATE_INTERVAL_HOURS):
                    return {"updated": False, "reason": "recently_checked", **get_gpxfeed_status()}
            except ValueError:
                pass
        _set_status(conn, last_checked_at=now.isoformat(timespec="seconds"))

    latest_sha = _get_latest_commit_sha()
    current_sha = get_gpxfeed_status()["commit_sha"]
    if not force and current_sha == latest_sha:
        with get_db() as conn:
            _set_status(conn, last_status=f"Already up to date at {latest_sha[:12]}")
        return {"updated": False, "reason": "up_to_date", **get_gpxfeed_status()}

    files = _list_gpxfeed_files()
    result = {"imported": 0, "skipped": 0, "errors": []}
    downloaded_payloads = []
    for item in files:
        try:
            downloaded_payloads.append(_download_file(item))
        except Exception as exc:
            result["errors"].append(f"{item.get('path', item.get('name'))}: {exc}")

    with get_db() as conn:
        conn.execute("DELETE FROM imported_campgrounds WHERE source = 'gpxfeed'")

    imported_result = _import_gpx_payloads(downloaded_payloads, source="gpxfeed")
    result["imported"] += imported_result["imported"]
    result["skipped"] += imported_result["skipped"]
    result["errors"].extend(imported_result["errors"])

    with get_db() as conn:
        status = "Updated GpxFeed campground data"
        if result["errors"]:
            status = f"Updated with {len(result['errors'])} errors"
        _set_status(
            conn,
            commit_sha=latest_sha,
            last_imported_at=now.isoformat(timespec="seconds"),
            last_status=status,
            file_count=len(files),
        )

    logging.info("GpxFeed update complete: %s imported, %s skipped", result["imported"], result["skipped"])
    return {"updated": True, **result, **get_gpxfeed_status()}
