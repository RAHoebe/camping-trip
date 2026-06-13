"""JSON API endpoints used by the map UI."""
import math

from flask import Blueprint, abort, jsonify, request
from flask_login import login_required

from blueprints.auth import admin_required
from database import get_db, get_home_location, get_trip, rows_to_dicts, set_setting
from route_service import get_route_for_trip

api_bp = Blueprint("api", __name__)


@api_bp.route("/trips/<int:trip_id>/route")
@login_required
def trip_route(trip_id):
    if not get_trip(trip_id):
        abort(404)
    refresh = request.args.get("refresh") == "1"
    return jsonify(get_route_for_trip(trip_id, refresh=refresh))


@api_bp.route("/campgrounds/search")
@admin_required
def campground_search():
    q = request.args.get("q", "").strip()
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius_km = request.args.get("radius_km", 20.0, type=float)
    north = request.args.get("north", type=float)
    south = request.args.get("south", type=float)
    east = request.args.get("east", type=float)
    west = request.args.get("west", type=float)

    clauses = []
    params = []
    if q:
        clauses.append("(name LIKE ? OR description LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if None not in (lat, lon) and radius_km:
        lat_delta = radius_km / 111.32
        lon_delta = radius_km / (111.32 * max(abs(math.cos(math.radians(lat))), 0.01))
        clauses.append("latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?")
        params.extend([lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta])
    if None not in (north, south, east, west):
        clauses.append("latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?")
        params.extend([south, north, west, east])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT campground_id, name, latitude, longitude, description, website, phone
            FROM imported_campgrounds
            {where}
            ORDER BY name
            LIMIT 250
        """, params).fetchall()
    campgrounds = rows_to_dicts(rows)
    if None not in (lat, lon) and radius_km:
        filtered = []
        for campground in campgrounds:
            distance_km = _distance_km(lat, lon, campground["latitude"], campground["longitude"])
            if distance_km <= radius_km:
                campground["distance_km"] = round(distance_km, 2)
                filtered.append(campground)
        campgrounds = sorted(filtered, key=lambda item: item["distance_km"])[:100]
    return jsonify({"campgrounds": campgrounds})


@api_bp.route("/home-location", methods=["POST"])
@admin_required
def update_home_location():
    payload = request.get_json(silent=True) or {}
    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "Latitude and longitude are required."}), 400
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return jsonify({"error": "Latitude or longitude is out of range."}), 400

    name = str(payload.get("name") or "Home").strip() or "Home"
    set_setting("home_name", name)
    set_setting("home_latitude", f"{latitude:.6f}")
    set_setting("home_longitude", f"{longitude:.6f}")
    with get_db() as conn:
        conn.execute("DELETE FROM route_cache")
    return jsonify({"home": get_home_location()})


def _distance_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
