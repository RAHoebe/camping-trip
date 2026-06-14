"""Admin routes for users, trips, stops, POIs, tracks, and GPX imports."""
import json
import os
import uuid

import bcrypt
from datetime import datetime, timedelta
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.utils import secure_filename

from blueprints.auth import admin_required
import config
from database import get_db, get_all_settings, get_home_location, get_trip, get_trip_pois, get_trip_stops, get_trip_track, get_trip_tracks, log_audit, rows_to_dicts, set_setting
from gpx_import import get_gpxfeed_status, update_gpxfeed_if_needed
from gpx_tracks import GpxTrackError, parse_gpx_track
from route_service import get_route_for_trip

admin_bp = Blueprint("admin", __name__)
TRACK_ACTIVITY_TYPES = {"cycling", "hiking"}


def _float_field(name):
    value = request.form.get(name, "").strip()
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name.replace('_', ' ').title()} must be a number.")


def _track_upload_path(stored_filename):
    base = os.path.abspath(config.TRACK_UPLOAD_FOLDER)
    path = os.path.abspath(os.path.join(base, stored_filename or ""))
    if not path.startswith(base + os.sep):
        raise ValueError("Invalid stored track filename.")
    return path


def _remove_track_file(track):
    try:
        path = _track_upload_path(track["stored_filename"])
    except (KeyError, ValueError):
        return
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _track_activity_type():
    activity_type = request.form.get("activity_type", "cycling").strip()
    return activity_type if activity_type in TRACK_ACTIVITY_TYPES else "cycling"


def _calculate_departure_date(arrival_date, nights):
    try:
        night_count = int(nights)
    except (TypeError, ValueError):
        night_count = 1
    night_count = max(1, night_count)
    try:
        arrival = datetime.strptime(arrival_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, night_count
    return (arrival + timedelta(days=night_count)).isoformat(), night_count


def _default_arrival_date(trip_id, trip_start_date=None):
    with get_db() as conn:
        row = conn.execute("""
            SELECT COALESCE(departure_date, arrival_date) AS next_date
            FROM trip_stops
            WHERE trip_id = ?
            ORDER BY arrival_date DESC, COALESCE(departure_date, arrival_date) DESC, stop_id DESC
            LIMIT 1
        """, (trip_id,)).fetchone()
    if row and row["next_date"]:
        return row["next_date"]
    return trip_start_date or ""


def _nights_for_stop(stop):
    if not stop:
        return 1
    try:
        arrival = datetime.strptime(str(stop["arrival_date"])[:10], "%Y-%m-%d").date()
        departure = datetime.strptime(str(stop["departure_date"])[:10], "%Y-%m-%d").date()
        return max(1, (departure - arrival).days)
    except (TypeError, ValueError):
        return 1


def _parsed_notes(notes):
    tags = []
    freeform = []
    for part in str(notes or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            tags.append((key.strip(), value.strip()))
        else:
            freeform.append(part)
    return tags, "\n".join(freeform)


def _clean_value(value):
    value = str(value or "").strip()
    if value.lower() in {"", "none", "null"}:
        return ""
    return value


def _first_value(*values):
    for value in values:
        value = _clean_value(value)
        if value:
            return value
    return ""


def _tag_lookup(tags, *keys):
    tag_map = {key.lower(): value for key, value in tags}
    for key in keys:
        value = _clean_value(tag_map.get(key.lower()))
        if value:
            return value
    return ""


def _address_from_tags(tags):
    street = _tag_lookup(tags, "addr:street")
    house_number = _tag_lookup(tags, "addr:housenumber")
    postcode = _tag_lookup(tags, "addr:postcode")
    city = _tag_lookup(tags, "addr:city")
    line_one = " ".join(part for part in [street, house_number] if part)
    line_two = " ".join(part for part in [postcode, city] if part)
    return ", ".join(part for part in [line_one, line_two] if part)


def _source_detail_label(key):
    labels = {
        "addr:city": "City",
        "addr:housenumber": "House number",
        "addr:postcode": "Postcode",
        "addr:street": "Street",
        "contact:phone": "Phone",
        "contact:website": "Website",
        "source:date": "Source date",
        "tourism": "Type",
    }
    return labels.get(key.lower(), key.replace(":", " ").replace("_", " ").title())


def _stop_form_details(stop):
    tags, freeform = _parsed_notes(stop["notes"] if stop else "")
    duplicate_keys = {
        "addr:city",
        "addr:housenumber",
        "addr:postcode",
        "addr:street",
        "contact:phone",
        "contact:website",
        "email",
        "mobile",
        "name",
        "phone",
        "website",
    }
    source_details = [
        {"label": _source_detail_label(key), "value": value}
        for key, value in tags
        if _clean_value(value) and key.lower() not in duplicate_keys
    ]
    return {
        "address": _first_value(stop["address"] if stop else "", _address_from_tags(tags)),
        "website": _first_value(stop["website"] if stop else "", _tag_lookup(tags, "website", "contact:website")),
        "phone": _first_value(stop["phone"] if stop else "", _tag_lookup(tags, "phone", "contact:phone", "mobile")),
        "booking_reference": _clean_value(stop["booking_reference"] if stop else ""),
        "personal_notes": freeform if tags else _clean_value(stop["notes"] if stop else ""),
        "source_details": source_details,
    }


def _last_stop_for_trip(trip_id, excluding_stop_id=None):
    params = [trip_id]
    where = "trip_id = ? AND is_last_stop = 1"
    if excluding_stop_id:
        where += " AND stop_id != ?"
        params.append(excluding_stop_id)
    with get_db() as conn:
        return conn.execute(f"SELECT stop_id, name FROM trip_stops WHERE {where} LIMIT 1", params).fetchone()


def _stop_form_context(trip, stop=None, default_arrival_date=None, nights=None):
    return {
        "trip": trip,
        "stop": stop,
        "default_arrival_date": default_arrival_date or _default_arrival_date(trip["trip_id"], trip["start_date"]),
        "nights": nights or _nights_for_stop(stop),
        "stop_details": _stop_form_details(stop),
        "existing_last_stop": _last_stop_for_trip(trip["trip_id"], stop["stop_id"] if stop else None),
    }


def _trip_warnings(stops, home):
    warnings = []
    if stops and not home:
        warnings.append("No home location is set, so the route starts at the first campsite.")
    previous = None
    for stop in stops:
        try:
            float(stop["latitude"])
            float(stop["longitude"])
        except (TypeError, ValueError):
            warnings.append(f"{stop['name']} has missing or invalid coordinates.")

        arrival = None
        departure = None
        try:
            arrival = datetime.strptime(str(stop["arrival_date"])[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            warnings.append(f"{stop['name']} has an invalid arrival date.")
        try:
            departure = datetime.strptime(str(stop["departure_date"])[:10], "%Y-%m-%d").date() if stop["departure_date"] else None
        except (TypeError, ValueError):
            warnings.append(f"{stop['name']} has an invalid departure date.")
        if arrival and departure and departure < arrival:
            warnings.append(f"{stop['name']} departs before it arrives.")
        if previous and arrival and previous["departure"] and arrival < previous["departure"]:
            warnings.append(f"{stop['name']} overlaps with {previous['name']}.")
        if arrival:
            previous = {"name": stop["name"], "departure": departure or arrival}
    return warnings


@admin_bp.route("/")
@admin_required
def index():
    with get_db() as conn:
        stats = {
            "trips": conn.execute("SELECT COUNT(*) AS count FROM trips").fetchone()["count"],
            "stops": conn.execute("SELECT COUNT(*) AS count FROM trip_stops").fetchone()["count"],
            "pois": conn.execute("SELECT COUNT(*) AS count FROM pois").fetchone()["count"],
            "tracks": conn.execute("SELECT COUNT(*) AS count FROM trip_tracks").fetchone()["count"],
            "campgrounds": conn.execute("SELECT COUNT(*) AS count FROM imported_campgrounds").fetchone()["count"],
            "users": conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"],
        }
        recent_trips = conn.execute("SELECT * FROM trips ORDER BY updated_at DESC, trip_id DESC LIMIT 6").fetchall()
    return render_template("admin/index.html", stats=stats, recent_trips=recent_trips)


@admin_bp.route("/users")
@admin_required
def users_list():
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("admin/users.html", users=users)


@admin_bp.route("/users/create", methods=["GET", "POST"])
@admin_required
def create_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        if not username or not email or len(password) < 8:
            flash("Username, email, and a password of at least 8 characters are required.", "danger")
            return render_template("admin/user_form.html", user=None)
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        try:
            with get_db() as conn:
                cur = conn.execute("""
                    INSERT INTO users (username, password_hash, email, first_name, last_name, role, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (username, password_hash, email, first_name, last_name, role, 1 if request.form.get("is_active") == "on" else 0))
                user_id = cur.lastrowid
            log_audit(current_user.user_id, "CREATE_USER", f"Created user {username}", "user", user_id)
            flash("User created.", "success")
            return redirect(url_for("admin.users_list"))
        except Exception as exc:
            flash(f"Could not create user: {exc}", "danger")
    return render_template("admin/user_form.html", user=None)


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        role = request.form.get("role", "user")
        is_active = 1 if request.form.get("is_active") == "on" else 0
        with get_db() as conn:
            conn.execute("""
                UPDATE users SET email = ?, first_name = ?, last_name = ?, role = ?, is_active = ?
                WHERE user_id = ?
            """, (email, first_name, last_name, role, is_active, user_id))
        log_audit(current_user.user_id, "UPDATE_USER", f"Updated user {user['username']}", "user", user_id)
        flash("User updated.", "success")
        return redirect(url_for("admin.users_list"))
    return render_template("admin/user_form.html", user=user)


@admin_bp.route("/users/<int:user_id>/password", methods=["POST"])
@admin_required
def reset_password(user_id):
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_db() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (password_hash, user_id))
    log_audit(current_user.user_id, "RESET_PASSWORD", "Reset user password", "user", user_id)
    flash("Password reset.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    if user_id == current_user.user_id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("admin.users_list"))
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            abort(404)
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    log_audit(current_user.user_id, "DELETE_USER", f"Deleted user {user['username']}", "user", user_id)
    flash("User deleted.", "success")
    return redirect(url_for("admin.users_list"))


@admin_bp.route("/trips")
@admin_required
def trips_list():
    with get_db() as conn:
        trips = conn.execute("""
            SELECT t.*,
                   COUNT(DISTINCT s.stop_id) AS stop_count,
                   COUNT(DISTINCT p.poi_id) AS poi_count,
                   COUNT(DISTINCT tr.track_id) AS track_count
            FROM trips t
            LEFT JOIN trip_stops s ON s.trip_id = t.trip_id
            LEFT JOIN pois p ON p.trip_id = t.trip_id
            LEFT JOIN trip_tracks tr ON tr.trip_id = t.trip_id
            GROUP BY t.trip_id
            ORDER BY COALESCE(t.start_date, '9999-12-31'), t.title
        """).fetchall()
    return render_template("admin/trips.html", trips=trips)


@admin_bp.route("/trips/create", methods=["GET", "POST"])
@admin_required
def create_trip():
    return _trip_form()


@admin_bp.route("/trips/<int:trip_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_trip(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    return _trip_form(trip)


def _trip_form(trip=None):
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        start_date = request.form.get("start_date") or None
        end_date = request.form.get("end_date") or None
        if not title:
            flash("Trip title is required.", "danger")
            return render_template("admin/trip_form.html", trip=trip)
        with get_db() as conn:
            if trip:
                conn.execute("""
                    UPDATE trips SET title = ?, description = ?, start_date = ?, end_date = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE trip_id = ?
                """, (title, description, start_date, end_date, trip["trip_id"]))
                trip_id = trip["trip_id"]
                action = "UPDATE_TRIP"
            else:
                cur = conn.execute("""
                    INSERT INTO trips (title, description, start_date, end_date, created_by)
                    VALUES (?, ?, ?, ?, ?)
                """, (title, description, start_date, end_date, current_user.user_id))
                trip_id = cur.lastrowid
                action = "CREATE_TRIP"
        log_audit(current_user.user_id, action, title, "trip", trip_id)
        flash("Trip saved.", "success")
        return redirect(url_for("admin.manage_trip", trip_id=trip_id))
    return render_template("admin/trip_form.html", trip=trip)


@admin_bp.route("/trips/<int:trip_id>")
@admin_required
def manage_trip(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    stops = get_trip_stops(trip_id)
    pois = get_trip_pois(trip_id)
    tracks = get_trip_tracks(trip_id)
    route = get_route_for_trip(trip_id, calculate=False)
    home = get_home_location()
    return render_template(
        "admin/manage_trip.html",
        trip=trip,
        stops=stops,
        pois=pois,
        tracks=tracks,
        route=route,
        warnings=_trip_warnings(stops, home),
        stops_json=rows_to_dicts(stops),
        pois_json=rows_to_dicts(pois),
        tracks_json=tracks,
        home_json=home,
    )


@admin_bp.route("/trips/<int:trip_id>/delete", methods=["POST"])
@admin_required
def delete_trip(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    with get_db() as conn:
        conn.execute("DELETE FROM trips WHERE trip_id = ?", (trip_id,))
    log_audit(current_user.user_id, "DELETE_TRIP", trip["title"], "trip", trip_id)
    flash("Trip deleted.", "success")
    return redirect(url_for("admin.trips_list"))


@admin_bp.route("/trips/<int:trip_id>/sync-dates", methods=["POST"])
@admin_required
def sync_trip_dates(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    stops = get_trip_stops(trip_id)
    if not stops:
        flash("Add at least one campsite before syncing trip dates.", "warning")
        return redirect(url_for("admin.manage_trip", trip_id=trip_id))
    start_date = stops[0]["arrival_date"]
    end_date = stops[-1]["departure_date"] or stops[-1]["arrival_date"]
    with get_db() as conn:
        conn.execute(
            "UPDATE trips SET start_date = ?, end_date = ?, updated_at = CURRENT_TIMESTAMP WHERE trip_id = ?",
            (start_date, end_date, trip_id),
        )
    log_audit(current_user.user_id, "SYNC_TRIP_DATES", trip["title"], "trip", trip_id)
    flash("Trip dates synced from campsites.", "success")
    return redirect(url_for("admin.manage_trip", trip_id=trip_id))


@admin_bp.route("/trips/<int:trip_id>/stops/create", methods=["GET", "POST"])
@admin_required
def create_stop(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    return _stop_form(trip)


@admin_bp.route("/stops/<int:stop_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_stop(stop_id):
    with get_db() as conn:
        stop = conn.execute("SELECT * FROM trip_stops WHERE stop_id = ?", (stop_id,)).fetchone()
    if not stop:
        abort(404)
    trip = get_trip(stop["trip_id"])
    return _stop_form(trip, stop)


def _stop_form(trip, stop=None):
    if request.method == "POST":
        try:
            latitude = _float_field("latitude")
            longitude = _float_field("longitude")
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "admin/stop_form.html",
                **_stop_form_context(
                    trip,
                    stop,
                    default_arrival_date=request.form.get("arrival_date"),
                    nights=request.form.get("nights"),
                ),
            )

        fields = {
            "name": request.form.get("name", "").strip(),
            "arrival_date": request.form.get("arrival_date", "").strip(),
            "latitude": latitude,
            "longitude": longitude,
            "address": request.form.get("address", "").strip(),
            "website": request.form.get("website", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "booking_reference": request.form.get("booking_reference", "").strip(),
            "notes": request.form.get("personal_notes", "").strip(),
            "is_last_stop": 1 if request.form.get("is_last_stop") == "on" else 0,
        }
        if not fields["name"] or not fields["arrival_date"]:
            flash("Name and arrival date are required.", "danger")
            return render_template("admin/stop_form.html", **_stop_form_context(trip, stop))
        departure_date, night_count = _calculate_departure_date(fields["arrival_date"], request.form.get("nights"))
        fields["departure_date"] = departure_date

        with get_db() as conn:
            if fields["is_last_stop"]:
                params = [trip["trip_id"]]
                query = "UPDATE trip_stops SET is_last_stop = 0 WHERE trip_id = ?"
                if stop:
                    query += " AND stop_id != ?"
                    params.append(stop["stop_id"])
                conn.execute(query, params)
            if stop:
                conn.execute("""
                    UPDATE trip_stops SET name = ?, arrival_date = ?, departure_date = ?, latitude = ?,
                        longitude = ?, address = ?, website = ?, phone = ?, booking_reference = ?,
                        notes = ?, is_last_stop = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE stop_id = ?
                """, (
                    fields["name"],
                    fields["arrival_date"],
                    fields["departure_date"],
                    fields["latitude"],
                    fields["longitude"],
                    fields["address"],
                    fields["website"],
                    fields["phone"],
                    fields["booking_reference"],
                    fields["notes"],
                    fields["is_last_stop"],
                    stop["stop_id"],
                ))
                stop_id = stop["stop_id"]
            else:
                cur = conn.execute("""
                    INSERT INTO trip_stops
                        (trip_id, name, arrival_date, departure_date, latitude, longitude, address,
                         website, phone, booking_reference, notes, is_last_stop)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trip["trip_id"],
                    fields["name"],
                    fields["arrival_date"],
                    fields["departure_date"],
                    fields["latitude"],
                    fields["longitude"],
                    fields["address"],
                    fields["website"],
                    fields["phone"],
                    fields["booking_reference"],
                    fields["notes"],
                    fields["is_last_stop"],
                ))
                stop_id = cur.lastrowid
            conn.execute("DELETE FROM route_cache WHERE trip_id = ?", (trip["trip_id"],))
        log_audit(current_user.user_id, "SAVE_STOP", fields["name"], "stop", stop_id)
        flash("Campsite saved.", "success")
        return redirect(url_for("admin.manage_trip", trip_id=trip["trip_id"]))
    return render_template(
        "admin/stop_form.html",
        **_stop_form_context(trip, stop, default_arrival_date=request.args.get("arrival_date")),
    )


@admin_bp.route("/stops/<int:stop_id>/delete", methods=["POST"])
@admin_required
def delete_stop(stop_id):
    with get_db() as conn:
        stop = conn.execute("SELECT * FROM trip_stops WHERE stop_id = ?", (stop_id,)).fetchone()
        if not stop:
            abort(404)
        conn.execute("DELETE FROM trip_stops WHERE stop_id = ?", (stop_id,))
        conn.execute("DELETE FROM route_cache WHERE trip_id = ?", (stop["trip_id"],))
    log_audit(current_user.user_id, "DELETE_STOP", stop["name"], "stop", stop_id)
    flash("Campsite deleted.", "success")
    return redirect(url_for("admin.manage_trip", trip_id=stop["trip_id"]))


@admin_bp.route("/trips/<int:trip_id>/pois/create", methods=["GET", "POST"])
@admin_required
def create_poi(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    return _poi_form(trip)


@admin_bp.route("/pois/<int:poi_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_poi(poi_id):
    with get_db() as conn:
        poi = conn.execute("SELECT * FROM pois WHERE poi_id = ?", (poi_id,)).fetchone()
    if not poi:
        abort(404)
    trip = get_trip(poi["trip_id"])
    return _poi_form(trip, poi)


def _poi_form(trip, poi=None):
    stops = get_trip_stops(trip["trip_id"])
    if request.method == "POST":
        try:
            latitude = _float_field("latitude")
            longitude = _float_field("longitude")
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("admin/poi_form.html", trip=trip, poi=poi, stops=stops)

        fields = {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "custom").strip(),
            "stop_id": request.form.get("stop_id") or None,
            "latitude": latitude,
            "longitude": longitude,
            "address": request.form.get("address", "").strip(),
            "website": request.form.get("website", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
        if not fields["name"]:
            flash("POI name is required.", "danger")
            return render_template("admin/poi_form.html", trip=trip, poi=poi, stops=stops)
        with get_db() as conn:
            if poi:
                conn.execute("""
                    UPDATE pois SET name = ?, category = ?, stop_id = ?, latitude = ?, longitude = ?,
                        address = ?, website = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE poi_id = ?
                """, (*fields.values(), poi["poi_id"]))
                poi_id = poi["poi_id"]
            else:
                cur = conn.execute("""
                    INSERT INTO pois (trip_id, name, category, stop_id, latitude, longitude, address, website, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (trip["trip_id"], *fields.values()))
                poi_id = cur.lastrowid
        log_audit(current_user.user_id, "SAVE_POI", fields["name"], "poi", poi_id)
        flash("POI saved.", "success")
        return redirect(url_for("admin.manage_trip", trip_id=trip["trip_id"]))
    return render_template("admin/poi_form.html", trip=trip, poi=poi, stops=stops)


@admin_bp.route("/pois/<int:poi_id>/delete", methods=["POST"])
@admin_required
def delete_poi(poi_id):
    with get_db() as conn:
        poi = conn.execute("SELECT * FROM pois WHERE poi_id = ?", (poi_id,)).fetchone()
        if not poi:
            abort(404)
        conn.execute("DELETE FROM pois WHERE poi_id = ?", (poi_id,))
    log_audit(current_user.user_id, "DELETE_POI", poi["name"], "poi", poi_id)
    flash("POI deleted.", "success")
    return redirect(url_for("admin.manage_trip", trip_id=poi["trip_id"]))


@admin_bp.route("/trips/<int:trip_id>/tracks/create", methods=["GET", "POST"])
@admin_required
def create_track(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    if request.method == "POST":
        upload = request.files.get("gpx_file")
        if not upload or not upload.filename:
            flash("Choose a GPX file to upload.", "danger")
            return render_template("admin/track_form.html", trip=trip, track=None)
        original_filename = secure_filename(upload.filename) or "track.gpx"
        if not original_filename.lower().endswith(".gpx"):
            flash("Track uploads must be GPX files.", "danger")
            return render_template("admin/track_form.html", trip=trip, track=None)
        payload = upload.read()
        try:
            parsed = parse_gpx_track(original_filename, payload)
        except GpxTrackError as exc:
            flash(str(exc), "danger")
            return render_template("admin/track_form.html", trip=trip, track=None)

        os.makedirs(config.TRACK_UPLOAD_FOLDER, exist_ok=True)
        stored_filename = f"{uuid.uuid4().hex}-{original_filename}"
        path = _track_upload_path(stored_filename)
        with open(path, "wb") as track_file:
            track_file.write(payload)

        name = request.form.get("name", "").strip() or parsed["name"]
        activity_type = _track_activity_type()
        show_on_map = 1 if request.form.get("show_on_map", "on") == "on" else 0
        try:
            with get_db() as conn:
                track_id = conn.execute("""
                    INSERT INTO trip_tracks
                        (trip_id, name, activity_type, show_on_map, original_filename, stored_filename,
                         line_geojson, distance_m, waypoints_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trip_id,
                    name,
                    activity_type,
                    show_on_map,
                    original_filename,
                    stored_filename,
                    json.dumps(parsed["line"]),
                    parsed["distance_m"],
                    json.dumps(parsed["waypoints"]),
                )).lastrowid
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            raise
        log_audit(current_user.user_id, "SAVE_TRACK", name, "track", track_id)
        flash("Track uploaded.", "success")
        return redirect(url_for("admin.manage_trip", trip_id=trip_id))
    return render_template("admin/track_form.html", trip=trip, track=None)


@admin_bp.route("/tracks/<int:track_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_track(track_id):
    track = get_trip_track(track_id)
    if not track:
        abort(404)
    trip = get_trip(track["trip_id"])
    if not trip:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Track name is required.", "danger")
            return render_template("admin/track_form.html", trip=trip, track=track)
        activity_type = _track_activity_type()
        show_on_map = 1 if request.form.get("show_on_map") == "on" else 0
        with get_db() as conn:
            conn.execute("""
                UPDATE trip_tracks
                SET name = ?, activity_type = ?, show_on_map = ?, updated_at = CURRENT_TIMESTAMP
                WHERE track_id = ?
            """, (name, activity_type, show_on_map, track_id))
        log_audit(current_user.user_id, "SAVE_TRACK", name, "track", track_id)
        flash("Track saved.", "success")
        return redirect(url_for("admin.manage_trip", trip_id=track["trip_id"]))
    return render_template("admin/track_form.html", trip=trip, track=track)


@admin_bp.route("/tracks/<int:track_id>/delete", methods=["POST"])
@admin_required
def delete_track(track_id):
    track = get_trip_track(track_id)
    if not track:
        abort(404)
    with get_db() as conn:
        conn.execute("DELETE FROM trip_tracks WHERE track_id = ?", (track_id,))
    _remove_track_file(track)
    log_audit(current_user.user_id, "DELETE_TRACK", track["name"], "track", track_id)
    flash("Track deleted.", "success")
    return redirect(url_for("admin.manage_trip", trip_id=track["trip_id"]))


@admin_bp.route("/campgrounds", methods=["GET", "POST"])
@admin_required
def campgrounds():
    if request.method == "POST":
        try:
            result = update_gpxfeed_if_needed(force=True)
        except Exception as exc:
            flash(f"Could not update GpxFeed campground data: {exc}", "danger")
            return redirect(url_for("admin.campgrounds"))
        flash(f"GpxFeed update finished. {result.get('campground_count', 0)} campgrounds are available.", "success")
        for error in result.get("errors", [])[:5]:
            flash(error, "warning")
        log_audit(current_user.user_id, "UPDATE_GPXFEED", "Updated GpxFeed campground data", "campground", None)
        return redirect(url_for("admin.campgrounds"))

    q = request.args.get("q", "").strip()
    params = []
    where = ""
    if q:
        where = "WHERE name LIKE ? OR description LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    with get_db() as conn:
        campgrounds = conn.execute(f"""
            SELECT * FROM imported_campgrounds
            {where}
            ORDER BY imported_at DESC, name
            LIMIT 200
        """, params).fetchall()
        trips = conn.execute("SELECT trip_id, title FROM trips ORDER BY COALESCE(start_date, '9999-12-31'), title").fetchall()
    return render_template(
        "admin/campgrounds.html",
        campgrounds=campgrounds,
        trips=trips,
        q=q,
        status=get_gpxfeed_status(),
    )


@admin_bp.route("/campgrounds/<int:campground_id>/add", methods=["POST"])
@admin_required
def choose_campground_trip(campground_id):
    trip_id = request.form.get("trip_id", type=int)
    if not trip_id:
        flash("Choose a trip first.", "danger")
        return redirect(url_for("admin.campgrounds"))
    return redirect(url_for("admin.add_campground_to_trip", trip_id=trip_id, campground_id=campground_id))


@admin_bp.route("/trips/<int:trip_id>/campgrounds/<int:campground_id>/add", methods=["GET", "POST"])
@admin_required
def add_campground_to_trip(trip_id, campground_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    with get_db() as conn:
        campground = conn.execute("SELECT * FROM imported_campgrounds WHERE campground_id = ?", (campground_id,)).fetchone()
    if not campground:
        abort(404)
    if request.method == "POST":
        arrival_date = request.form.get("arrival_date", "").strip()
        if not arrival_date:
            flash("Arrival date is required.", "danger")
            return render_template("admin/add_campground.html", trip=trip, campground=campground, default_arrival_date=_default_arrival_date(trip_id, trip["start_date"]), nights=1)
        departure_date, night_count = _calculate_departure_date(arrival_date, request.form.get("nights"))
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO trip_stops
                    (trip_id, campground_id, name, arrival_date, departure_date, latitude, longitude,
                     website, phone, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trip_id,
                campground_id,
                campground["name"],
                arrival_date,
                departure_date,
                campground["latitude"],
                campground["longitude"],
                campground["website"],
                campground["phone"],
                campground["description"],
            ))
            conn.execute("DELETE FROM route_cache WHERE trip_id = ?", (trip_id,))
            stop_id = cur.lastrowid
        log_audit(current_user.user_id, "ADD_IMPORTED_CAMPGROUND", campground["name"], "stop", stop_id)
        flash("Campground added to trip.", "success")
        return redirect(url_for("admin.manage_trip", trip_id=trip_id))
    return render_template(
        "admin/add_campground.html",
        trip=trip,
        campground=campground,
        default_arrival_date=_default_arrival_date(trip_id, trip["start_date"]),
        nights=1,
    )


@admin_bp.route("/options", methods=["GET", "POST"])
@admin_required
def options():
    if request.method == "POST":
        site_title = request.form.get("site_title", "Camping Trip Planner").strip() or "Camping Trip Planner"
        default_theme = request.form.get("default_theme", "light")
        if default_theme not in ("light", "dark"):
            default_theme = "light"
        theme_color = request.form.get("theme_color", "green")
        if theme_color not in ("purple", "red", "green", "cyan", "blue", "yellow"):
            theme_color = "green"
        set_setting("site_title", site_title)
        set_setting("default_theme", default_theme)
        set_setting("theme_color", theme_color)
        version_check_enabled = request.form.get("version_check_enabled", "false")
        if version_check_enabled not in ("true", "false"):
            version_check_enabled = "true"
        set_setting("version_check_enabled", version_check_enabled)
        set_setting("home_name", request.form.get("home_name", "Home").strip() or "Home")
        set_setting("home_latitude", request.form.get("home_latitude", "").strip())
        set_setting("home_longitude", request.form.get("home_longitude", "").strip())
        with get_db() as conn:
            conn.execute("DELETE FROM route_cache")
        flash("Options saved.", "success")
        return redirect(url_for("admin.options"))
    return render_template("admin/options.html", settings=get_all_settings())
