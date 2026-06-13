"""Traffic warning and closure helpers."""
import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

import config
from database import get_db


EVENT_TYPES = {"closure", "roadworks", "event", "bridge_opening", "traffic_measure", "unknown"}
SEVERITIES = {"closed", "major", "minor", "info"}


def _local_name(tag):
    return str(tag).split("}", 1)[-1].lower()


def _text(node):
    return "".join(node.itertext()).strip() if node is not None else ""


def _first_text(root, names):
    wanted = {name.lower() for name in names}
    for node in root.iter():
        if _local_name(node.tag) in wanted and _text(node):
            return _text(node)
    return ""


def _parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _classify_event(record):
    haystack = " ".join([_local_name(record.tag), _text(record)]).lower()
    if any(word in haystack for word in ["closed_lanes", "closed lanes", "laneclosed", "lane closed", "lanes closed", "narrow_lanes", "narrow lanes"]):
        return "traffic_measure", "major"
    if any(word in haystack for word in ["junction_closure", "junction closure", "roadclosed", "road closed", "road closure", "carriageway closed", "trafficprohibition", "no entry"]):
        return "closure", "closed"
    if any(word in haystack for word in ["roadworks", "maintenanceworks", "construction", "werkzaamheden"]):
        return "roadworks", "major"
    if "bridge" in haystack and ("open" in haystack or "opening" in haystack):
        return "bridge_opening", "major"
    if any(word in haystack for word in ["trafficmeasure", "restriction", "laneclosed"]):
        return "traffic_measure", "major"
    if "event" in haystack:
        return "event", "minor"
    return "unknown", "info"


def _extract_times(record):
    start = _first_text(record, ["overallStartTime", "startOfPeriod", "startTime", "validityStartTime"])
    end = _first_text(record, ["overallEndTime", "endOfPeriod", "endTime", "validityEndTime"])
    return start or None, end or None


def _extract_points(record):
    points = []
    for node in record.iter():
        children = list(node)
        lat = None
        lon = None
        for child in children:
            name = _local_name(child.tag)
            value = _text(child)
            if not value:
                continue
            try:
                number = float(value)
            except ValueError:
                continue
            if name in {"latitude", "lat"}:
                lat = number
            if name in {"longitude", "lon", "lng"}:
                lon = number
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append([lon, lat])

        if _local_name(node.tag) in {"poslist", "coordinates"}:
            values = []
            for part in _text(node).replace(",", " ").split():
                try:
                    values.append(float(part))
                except ValueError:
                    pass
            if len(values) >= 2:
                pairs = list(zip(values[0::2], values[1::2]))
                for first, second in pairs:
                    if -90 <= first <= 90 and -180 <= second <= 180:
                        points.append([second, first])
                    elif -180 <= first <= 180 and -90 <= second <= 90:
                        points.append([first, second])
    deduped = []
    seen = set()
    for lon, lat in points:
        key = (round(lon, 6), round(lat, 6))
        if key not in seen:
            seen.add(key)
            deduped.append([lon, lat])
    return deduped


def _geometry(points):
    if not points:
        return None
    if len(points) == 1:
        return {"type": "Point", "coordinates": points[0]}
    return {"type": "LineString", "coordinates": points}


def parse_datex_events(payload, source="ndw", country="NL"):
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    root = ET.fromstring(payload)
    records = [node for node in root.iter() if _local_name(node.tag) == "situationrecord"]
    if not records:
        records = [node for node in root.iter() if _local_name(node.tag) in {"maintenanceworks", "networkmanagement", "abnormalsituation"}]

    events = []
    for index, record in enumerate(records):
        points = _extract_points(record)
        geometry = _geometry(points)
        if not geometry:
            continue
        event_type, severity = _classify_event(record)
        starts_at, ends_at = _extract_times(record)
        record_id = record.attrib.get("id") or record.attrib.get("uuid") or f"{source}-{index}-{hash(json.dumps(geometry, sort_keys=True))}"
        road_name = _first_text(record, ["roadName", "roadNumber", "name"])
        title = _first_text(record, ["comment", "description", "situationRecordCreationReference"]) or road_name or event_type.replace("_", " ").title()
        description = _first_text(record, ["comment", "description", "generalPublicComment"]) or title
        events.append({
            "source": source,
            "country": country,
            "event_type": event_type if event_type in EVENT_TYPES else "unknown",
            "severity": severity if severity in SEVERITIES else "info",
            "title": title[:240],
            "description": description[:1000],
            "starts_at": starts_at,
            "ends_at": ends_at,
            "road_name": road_name[:120] if road_name else "",
            "geometry_geojson": json.dumps(geometry),
            "raw_source_id": str(record_id)[:240],
        })
    return events


def _active(event, now=None, lookahead_days=None):
    now = now or datetime.now(timezone.utc)
    lookahead = now + timedelta(days=lookahead_days or config.TRAFFIC_LOOKAHEAD_DAYS)
    starts_at = _parse_date(event["starts_at"])
    ends_at = _parse_date(event["ends_at"])
    if ends_at and ends_at < now:
        return False
    if starts_at and starts_at > lookahead:
        return False
    return True


def update_traffic_events(force=False):
    source = config.TRAFFIC_FIRST_PROVIDER
    if source != "ndw":
        raise RuntimeError(f"Unsupported traffic provider: {source}")
    response = requests.get(config.TRAFFIC_NDW_URL, timeout=config.TRAFFIC_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    events = parse_datex_events(response.content, source="ndw", country="NL")
    with get_db() as conn:
        conn.execute("DELETE FROM traffic_events WHERE source = ?", ("ndw",))
        for event in events:
            conn.execute("""
                INSERT OR REPLACE INTO traffic_events
                    (source, country, event_type, severity, title, description, starts_at, ends_at,
                     road_name, geometry_geojson, raw_source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event["source"], event["country"], event["event_type"], event["severity"], event["title"],
                event["description"], event["starts_at"], event["ends_at"], event["road_name"],
                event["geometry_geojson"], event["raw_source_id"],
            ))
        conn.execute("""
            INSERT OR REPLACE INTO traffic_update_status (source, status, message, event_count)
            VALUES (?, ?, ?, ?)
        """, ("ndw", "ok", "Traffic data updated.", len(events)))
    return {"source": "ndw", "status": "ok", "event_count": len(events)}


def record_traffic_error(source, message):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO traffic_update_status (source, status, message, event_count)
            VALUES (?, ?, ?, COALESCE((SELECT event_count FROM traffic_update_status WHERE source = ?), 0))
        """, (source, "error", str(message)[:1000], source))


def get_traffic_status():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM traffic_update_status ORDER BY fetched_at DESC").fetchall()
        count = conn.execute("SELECT COUNT(*) AS count FROM traffic_events").fetchone()["count"]
    return {"events": count, "sources": [dict(row) for row in rows]}


def _event_points(geometry):
    if not geometry:
        return []
    if geometry.get("type") == "Point":
        return [geometry["coordinates"]]
    if geometry.get("type") == "LineString":
        return geometry.get("coordinates", [])
    return []


def _bbox(points):
    if not points:
        return None
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return min(lons), min(lats), max(lons), max(lats)


def _expand_bbox(bounds, meters):
    west, south, east, north = bounds
    mid_lat = (south + north) / 2
    lat_delta = meters / 111320
    lon_delta = meters / (111320 * max(math.cos(math.radians(mid_lat)), 0.01))
    return west - lon_delta, south - lat_delta, east + lon_delta, north + lat_delta


def _bbox_intersects(a_bounds, b_bounds):
    if not a_bounds or not b_bounds:
        return False
    a_west, a_south, a_east, a_north = a_bounds
    b_west, b_south, b_east, b_north = b_bounds
    return not (a_east < b_west or b_east < a_west or a_north < b_south or b_north < a_south)


def _downsample_points(points, max_points=250):
    if len(points) <= max_points:
        return points
    step = max(1, math.ceil(len(points) / max_points))
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def _meters_per_degree(lat):
    return 111320, 111320 * max(math.cos(math.radians(lat)), 0.01)


def _point_segment_distance_m(point, start, end):
    lon, lat = point
    lon1, lat1 = start
    lon2, lat2 = end
    m_lat, m_lon = _meters_per_degree((lat + lat1 + lat2) / 3)
    px, py = lon * m_lon, lat * m_lat
    ax, ay = lon1 * m_lon, lat1 * m_lat
    bx, by = lon2 * m_lon, lat2 * m_lat
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _line_distance_m(a_points, b_points):
    if not a_points or not b_points:
        return float("inf")
    if len(a_points) == 1 and len(b_points) == 1:
        return _point_segment_distance_m(a_points[0], b_points[0], b_points[0])
    if len(a_points) == 1:
        return min(_point_segment_distance_m(a_points[0], b_points[i - 1], b_points[i]) for i in range(1, len(b_points)))
    if len(b_points) == 1:
        return min(_point_segment_distance_m(b_points[0], a_points[i - 1], a_points[i]) for i in range(1, len(a_points)))
    best = float("inf")
    for point in b_points:
        for i in range(1, len(a_points)):
            best = min(best, _point_segment_distance_m(point, a_points[i - 1], a_points[i]))
            if best == 0:
                return 0
    for point in a_points:
        for i in range(1, len(b_points)):
            best = min(best, _point_segment_distance_m(point, b_points[i - 1], b_points[i]))
            if best == 0:
                return 0
    return best


def _active_events():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM traffic_events ORDER BY severity, starts_at").fetchall()
    events = [dict(row) for row in rows]
    return [event for event in events if _active(event)]


def _is_route_warning_event(event):
    if event["severity"] == "info":
        return False
    title = (event.get("title") or "").lower()
    if "speed_management" in title or "speed management" in title:
        return False
    return event["event_type"] in {"closure", "roadworks", "traffic_measure", "event"}


def _warning_dedupe_key(event, geometry):
    title = (event.get("title") or "").lower()
    for suffix in [
        "_speed_management",
        "_closed_lanes",
        "_narrow_lanes",
        "_hardshoulder_usage",
        "_traffic_measure",
    ]:
        if suffix in title:
            return title.split(suffix, 1)[0]
    points = _event_points(geometry)
    if points:
        bounds = _bbox(points)
        return f"{event.get('event_type')}:{event.get('severity')}:{','.join(str(round(value, 5)) for value in bounds)}"
    return f"{event.get('event_type')}:{event.get('severity')}:{event.get('raw_source_id')}"


def warnings_for_route(trip_id, route=None):
    if not config.TRAFFIC_WARNINGS_ENABLED:
        return []
    if route is None:
        from route_service import get_route_for_trip

        route = get_route_for_trip(trip_id)
    coordinates = route.get("route", {}).get("coordinates") if route.get("route") else []
    if not coordinates or len(coordinates) < 2:
        return []
    corridor_m = config.TRAFFIC_ROUTE_CORRIDOR_METERS
    route_bounds = _expand_bbox(_bbox(coordinates), corridor_m)
    route_match_points = _downsample_points(coordinates, max_points=180)
    warnings = []
    seen = set()
    for event in _active_events():
        if not _is_route_warning_event(event):
            continue
        geometry = json.loads(event["geometry_geojson"])
        dedupe_key = _warning_dedupe_key(event, geometry)
        if dedupe_key in seen:
            continue
        event_points = _event_points(geometry)
        event_bounds = _expand_bbox(_bbox(event_points), corridor_m)
        if not _bbox_intersects(route_bounds, event_bounds):
            continue
        event_match_points = _downsample_points(event_points, max_points=80)
        distance = _line_distance_m(route_match_points, event_match_points)
        if distance <= corridor_m:
            event["geometry"] = geometry
            event["distance_m"] = round(distance)
            warnings.append(event)
            seen.add(dedupe_key)
    return warnings


def hard_closures_for_leg(points):
    if not config.TRAFFIC_WARNINGS_ENABLED:
        return []
    leg_line = [[float(point["longitude"]), float(point["latitude"])] for point in points]
    corridor_m = config.TRAFFIC_ROUTE_CORRIDOR_METERS
    leg_bounds = _expand_bbox(_bbox(leg_line), corridor_m)
    closures = []
    for event in _active_events():
        if event["severity"] != "closed" and event["event_type"] != "closure":
            continue
        geometry = json.loads(event["geometry_geojson"])
        event_points = _event_points(geometry)
        event_bounds = _expand_bbox(_bbox(event_points), corridor_m)
        if not _bbox_intersects(leg_bounds, event_bounds):
            continue
        distance = _line_distance_m(leg_line, event_points)
        if distance <= corridor_m:
            event["geometry"] = geometry
            event["distance_m"] = round(distance)
            closures.append(event)
    return closures


def avoid_areas_for_events(events, buffer_degrees=0.003):
    features = []
    for event in events:
        geometry = event.get("geometry") or json.loads(event["geometry_geojson"])
        points = _event_points(geometry)
        if not points:
            continue
        lons = [point[0] for point in points]
        lats = [point[1] for point in points]
        west, east = min(lons) - buffer_degrees, max(lons) + buffer_degrees
        south, north = min(lats) - buffer_degrees, max(lats) + buffer_degrees
        ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
        features.append({
            "type": "Feature",
            "id": f"closure_{event['event_id']}",
            "properties": {"name": event["title"]},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return {"type": "FeatureCollection", "features": features}
