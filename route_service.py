"""Routing provider integration and cache handling."""
import hashlib
import json
import logging
import threading
import time

import requests

import config
from database import get_db, get_home_location, get_setting, get_trip_stops, rows_to_dicts, set_setting, stop_signature

_graphhopper_lock = threading.Lock()
_last_graphhopper_request_at = 0.0


def _geojson_line(coordinates):
    return {"type": "LineString", "coordinates": coordinates}


def _point_value(point, key, default=None):
    try:
        return point[key]
    except (KeyError, IndexError, TypeError):
        return default


def _record_graphhopper_rate_limits(headers):
    remaining = headers.get("X-RateLimit-Remaining")
    limit = headers.get("X-RateLimit-Limit")
    reset = headers.get("X-RateLimit-Reset")
    if remaining is None:
        return
    set_setting("graphhopper_credits_remaining", str(remaining))
    if limit is not None:
        set_setting("graphhopper_credits_limit", str(limit))
    if reset is not None:
        set_setting("graphhopper_credits_reset", str(reset))


def get_graphhopper_credit_status():
    remaining = get_setting("graphhopper_credits_remaining")
    if remaining is None:
        return None
    return {
        "remaining": remaining,
        "limit": get_setting("graphhopper_credits_limit"),
        "reset": get_setting("graphhopper_credits_reset"),
    }


def _graphhopper_wait_for_slot():
    global _last_graphhopper_request_at
    delay = max(0.0, float(getattr(config, "GRAPHHOPPER_LEG_DELAY_SECONDS", 0) or 0))
    if delay <= 0:
        return
    with _graphhopper_lock:
        now = time.monotonic()
        wait_seconds = (_last_graphhopper_request_at + delay) - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _last_graphhopper_request_at = time.monotonic()


def _retry_after_seconds(response):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return max(0.0, float(getattr(config, "GRAPHHOPPER_429_RETRY_SECONDS", 65) or 65))


def _post_graphhopper(url, payload):
    _graphhopper_wait_for_slot()
    return requests.post(
        url,
        params={"key": config.GRAPHHOPPER_API_KEY},
        json=payload,
        timeout=config.ROUTE_TIMEOUT_SECONDS,
    )


def _request_graphhopper(stops):
    if not config.GRAPHHOPPER_API_KEY:
        raise RuntimeError("GRAPHHOPPER_API_KEY is not configured")
    url = f"{config.GRAPHHOPPER_BASE_URL.rstrip('/')}/route"
    payload = {
        "points": [[float(stop["longitude"]), float(stop["latitude"])] for stop in stops],
        "profile": "car",
        "locale": "en",
        "points_encoded": False,
        "instructions": False,
    }
    response = _post_graphhopper(url, payload)
    _record_graphhopper_rate_limits(response.headers)
    retries = max(0, int(getattr(config, "GRAPHHOPPER_429_RETRIES", 0) or 0))
    while getattr(response, "status_code", None) == 429 and retries > 0:
        time.sleep(_retry_after_seconds(response))
        response = _post_graphhopper(url, payload)
        _record_graphhopper_rate_limits(response.headers)
        retries -= 1
    if not response.ok:
        raise RuntimeError(f"GraphHopper route failed ({response.status_code}): {_response_message(response)}")
    data = response.json()
    path = data.get("paths", [{}])[0]
    coordinates = path.get("points", {}).get("coordinates")
    if not coordinates:
        raise RuntimeError("GraphHopper did not return route coordinates")
    return _geojson_line(coordinates), path.get("distance"), path.get("time", 0) / 1000


def _request_osrm(stops):
    coord_text = ";".join(f"{float(stop['longitude'])},{float(stop['latitude'])}" for stop in stops)
    url = f"{config.OSRM_BASE_URL.rstrip('/')}/route/v1/driving/{coord_text}"
    response = requests.get(
        url,
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=config.ROUTE_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise RuntimeError(f"OSRM route failed ({response.status_code}): {_response_message(response)}")
    data = response.json()
    route = data.get("routes", [{}])[0]
    coordinates = route.get("geometry", {}).get("coordinates")
    if not coordinates:
        raise RuntimeError("OSRM did not return route coordinates")
    return _geojson_line(coordinates), route.get("distance"), route.get("duration")


def _response_message(response):
    try:
        data = response.json()
        message = data.get("message") or data.get("error") or str(data)
    except ValueError:
        message = response.text
    message = (message or response.reason or "provider error").strip()
    return message[:500]


def _request_provider(points, provider):
    if provider == "osrm":
        return _request_osrm(points)
    return _request_graphhopper(points)


def _leg_signature(start, end):
    payload = {
        "start": {
            "latitude": round(float(start["latitude"]), 6),
            "longitude": round(float(start["longitude"]), 6),
        },
        "end": {
            "latitude": round(float(end["latitude"]), 6),
            "longitude": round(float(end["longitude"]), 6),
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _get_leg_route(start, end, provider):
    signature = _leg_signature(start, end)
    cache_hours = max(0.0, float(getattr(config, "ROUTE_LEG_CACHE_HOURS", 24) or 0))
    if cache_hours > 0:
        with get_db() as conn:
            cached = conn.execute("""
                SELECT route_geojson, distance_m, duration_s
                FROM route_leg_cache
                WHERE provider = ? AND leg_signature = ?
                  AND created_at >= datetime('now', ?)
            """, (provider, signature, f"-{cache_hours} hours")).fetchone()
        if cached:
            return json.loads(cached["route_geojson"]), cached["distance_m"], cached["duration_s"]

    leg_route, distance_m, duration_s = _request_provider([start, end], provider)
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO route_leg_cache
                (provider, leg_signature, route_geojson, distance_m, duration_s)
            VALUES (?, ?, ?, ?, ?)
        """, (provider, signature, json.dumps(leg_route), distance_m, duration_s))
    return leg_route, distance_m, duration_s


def _route_signature(home, stops):
    payload = {"home": home, "stops": []}
    for stop in stops:
        payload["stops"].append({
            "stop_id": stop["stop_id"],
            "arrival_date": stop["arrival_date"],
            "departure_date": stop["departure_date"],
            "lat": round(float(stop["latitude"]), 6),
            "lon": round(float(stop["longitude"]), 6),
            "is_last_stop": int(stop["is_last_stop"] or 0) if "is_last_stop" in stop.keys() else 0,
        })
    return json.dumps(payload, sort_keys=True)


def _route_points(home, stops):
    points = []
    if home:
        points.append({
            "name": home["name"],
            "latitude": home["latitude"],
            "longitude": home["longitude"],
            "stop_id": "__home_start__",
            "is_home": True,
        })
    points.extend(stops)
    if home and any((stop["is_last_stop"] if "is_last_stop" in stop.keys() else 0) for stop in stops):
        points.append({
            "name": home["name"],
            "latitude": home["latitude"],
            "longitude": home["longitude"],
            "stop_id": "__home_return__",
            "is_home": True,
            "is_return_home": True,
        })
    return points


def _build_route_and_leg_metrics(points, provider):
    legs = []
    cumulative_distance = 0
    cumulative_duration = 0
    stitched_coordinates = []
    for index in range(1, len(points)):
        start = points[index - 1]
        end = points[index]
        try:
            leg_route, distance_m, duration_s = _get_leg_route(start, end, provider)
        except Exception as exc:
            raise RuntimeError(f"Could not calculate route from {start['name']} to {end['name']}: {exc}") from exc

        coordinates = leg_route.get("coordinates") or []
        if coordinates:
            if stitched_coordinates and coordinates[0] == stitched_coordinates[-1]:
                stitched_coordinates.extend(coordinates[1:])
            else:
                stitched_coordinates.extend(coordinates)

        if distance_m is not None:
            cumulative_distance += distance_m
        if duration_s is not None:
            cumulative_duration += duration_s

        leg = {
            "from_name": _point_value(start, "name", "Previous"),
            "to_name": _point_value(end, "name", "Next"),
            "to_stop_id": _point_value(end, "stop_id"),
            "distance_m": distance_m,
            "duration_s": duration_s,
            "cumulative_distance_m": cumulative_distance if distance_m is not None else None,
            "cumulative_duration_s": cumulative_duration if duration_s is not None else None,
        }
        legs.append(leg)
    return _geojson_line(stitched_coordinates), legs, cumulative_distance, cumulative_duration


def _stop_metrics_from_legs(legs):
    metrics = {}
    return_home = None
    for leg in legs or []:
        stop_id = leg.get("to_stop_id")
        if stop_id == "__home_return__":
            return_home = leg
        elif stop_id is not None and not str(stop_id).startswith("__home"):
            metrics[int(stop_id)] = leg
    return metrics, return_home


def _empty_route_response(status, message, stop_dicts, home, provider):
    return {
        "status": status,
        "message": message,
        "stops": stop_dicts,
        "home": home,
        "route": None,
        "legs": [],
        "stop_metrics": {},
        "return_home_metric": None,
        "distance_m": None,
        "duration_s": None,
        "provider": provider,
        "graphhopper_credits": get_graphhopper_credit_status() if provider == "graphhopper" else None,
    }


def get_route_for_trip(trip_id, refresh=False, calculate=True):
    stops = get_trip_stops(trip_id)
    stop_dicts = rows_to_dicts(stops)
    home = get_home_location()
    provider = config.ROUTE_PROVIDER
    points = _route_points(home, stops)
    if len(points) < 2:
        needed = "Add a campsite to calculate a route from home." if home else "Add at least two campsites to calculate a route."
        return _empty_route_response("not_enough_stops", needed, stop_dicts, home, provider)

    signature_suffix = _route_signature(home, stops)
    signature = stop_signature([{"stop_id": "route", "arrival_date": signature_suffix, "departure_date": "", "latitude": 0, "longitude": 0}])
    graphhopper_credits = get_graphhopper_credit_status() if provider == "graphhopper" else None

    if not refresh:
        with get_db() as conn:
            cached = conn.execute("""
                SELECT * FROM route_cache
                WHERE trip_id = ? AND provider = ? AND stop_signature = ?
            """, (trip_id, provider, signature)).fetchone()
            if cached and cached["route_legs_json"]:
                legs = json.loads(cached["route_legs_json"]) if cached["route_legs_json"] else []
                return {
                    "status": cached["status"],
                    "message": cached["error_message"],
                    "stops": stop_dicts,
                    "home": home,
                    "route": json.loads(cached["route_geojson"]) if cached["route_geojson"] else None,
                    "legs": legs,
                    "stop_metrics": _stop_metrics_from_legs(legs)[0],
                    "return_home_metric": _stop_metrics_from_legs(legs)[1],
                    "distance_m": cached["distance_m"],
                    "duration_s": cached["duration_s"],
                    "provider": provider,
                    "cached": True,
                    "graphhopper_credits": graphhopper_credits,
                }

    if not calculate:
        return _empty_route_response(
            "manual_refresh_required",
            "Route not calculated yet. Use Refresh route when you are ready.",
            stop_dicts,
            home,
            provider,
        )

    try:
        route, legs, distance_m, duration_s = _build_route_and_leg_metrics(points, provider)
        status = "ok"
        error_message = None
    except Exception as exc:
        logging.warning("Route calculation failed for trip %s: %s", trip_id, exc)
        route = None
        distance_m = None
        duration_s = None
        legs = []
        status = "error"
        error_message = str(exc)

    if status == "ok":
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO route_cache
                    (trip_id, provider, stop_signature, route_geojson, route_legs_json, distance_m, duration_s, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trip_id,
                provider,
                signature,
                json.dumps(route),
                json.dumps(legs),
                distance_m,
                duration_s,
                status,
                error_message,
            ))

    return {
        "status": status,
        "message": error_message,
        "stops": stop_dicts,
        "home": home,
        "route": route,
        "legs": legs,
        "stop_metrics": _stop_metrics_from_legs(legs)[0],
        "return_home_metric": _stop_metrics_from_legs(legs)[1],
        "distance_m": distance_m,
        "duration_s": duration_s,
        "provider": provider,
        "cached": False,
        "graphhopper_credits": get_graphhopper_credit_status() if provider == "graphhopper" else None,
    }
