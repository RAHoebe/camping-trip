"""Routing provider integration and cache handling."""
import json
import logging

import requests

import config
from database import get_db, get_home_location, get_setting, get_trip_stops, rows_to_dicts, set_setting, stop_signature


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


def _request_graphhopper(stops, avoid_events=None):
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
    if avoid_events:
        from traffic_service import avoid_areas_for_events

        areas = avoid_areas_for_events(avoid_events)
        if areas.get("features"):
            payload["custom_model"] = {
                "areas": areas,
                "priority": [
                    {"if": f"in_{feature['id']}", "multiply_by": 0}
                    for feature in areas["features"]
                ],
            }
    response = requests.post(
        url,
        params={"key": config.GRAPHHOPPER_API_KEY},
        json=payload,
        timeout=config.ROUTE_TIMEOUT_SECONDS,
    )
    _record_graphhopper_rate_limits(response.headers)
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


def _request_provider(points, provider, avoid_events=None):
    if provider == "osrm":
        return _request_osrm(points)
    if avoid_events is None:
        return _request_graphhopper(points)
    return _request_graphhopper(points, avoid_events=avoid_events)


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


def _build_route_and_leg_metrics(points, provider, avoid_closures=False):
    legs = []
    cumulative_distance = 0
    cumulative_duration = 0
    stitched_coordinates = []
    for index in range(1, len(points)):
        start = points[index - 1]
        end = points[index]
        closure_events = []
        avoidance_status = None
        avoidance_error = None
        try:
            if avoid_closures and provider == "graphhopper" and config.ROUTE_AVOID_CLOSURES_ENABLED:
                from traffic_service import hard_closures_for_leg

                closure_events = hard_closures_for_leg([start, end])
                if closure_events:
                    try:
                        leg_route, distance_m, duration_s = _request_provider([start, end], provider, avoid_events=closure_events)
                        avoidance_status = "avoided"
                    except Exception as exc:
                        avoidance_status = "failed"
                        avoidance_error = str(exc)
                        leg_route, distance_m, duration_s = _request_provider([start, end], provider)
                else:
                    leg_route, distance_m, duration_s = _request_provider([start, end], provider)
            else:
                leg_route, distance_m, duration_s = _request_provider([start, end], provider)
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
        if closure_events:
            leg["traffic_warnings"] = [
                {
                    "event_id": event["event_id"],
                    "title": event["title"],
                    "event_type": event["event_type"],
                    "severity": event["severity"],
                    "road_name": event["road_name"],
                }
                for event in closure_events
            ]
            leg["closure_avoidance"] = avoidance_status
            if avoidance_error:
                leg["closure_avoidance_error"] = avoidance_error
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


def get_route_for_trip(trip_id, refresh=False, avoid_closures=False):
    stops = get_trip_stops(trip_id)
    stop_dicts = rows_to_dicts(stops)
    home = get_home_location()
    provider = config.ROUTE_PROVIDER
    points = _route_points(home, stops)
    if len(points) < 2:
        needed = "Add a campsite to calculate a route from home." if home else "Add at least two campsites to calculate a route."
        return {
            "status": "not_enough_stops",
            "message": needed,
            "stops": stop_dicts,
            "home": home,
            "route": None,
            "distance_m": None,
            "duration_s": None,
            "provider": provider,
            "graphhopper_credits": get_graphhopper_credit_status() if provider == "graphhopper" else None,
        }

    signature_suffix = _route_signature(home, stops)
    if avoid_closures:
        signature_suffix += "|avoid_closures"
    signature = stop_signature([{"stop_id": "route", "arrival_date": signature_suffix, "departure_date": "", "latitude": 0, "longitude": 0}])
    graphhopper_credits = get_graphhopper_credit_status() if provider == "graphhopper" else None

    if not refresh and not avoid_closures:
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

    try:
        route, legs, distance_m, duration_s = _build_route_and_leg_metrics(points, provider, avoid_closures=avoid_closures)
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

    if status == "ok" and not avoid_closures:
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
        "avoid_closures": avoid_closures,
        "graphhopper_credits": get_graphhopper_credit_status() if provider == "graphhopper" else None,
    }
