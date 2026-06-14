"""Trip browsing routes for logged-in users."""
import os

from flask import Blueprint, Response, abort, redirect, render_template, send_file
from flask_login import login_required

import config
from database import get_home_location, get_trip, get_trip_pois, get_trip_stops, get_trip_track, get_trip_tracks, list_trips as db_list_trips, rows_to_dicts
from export_service import google_maps_url, trip_kml
from route_service import get_route_for_trip

trips_bp = Blueprint("trips", __name__)


@trips_bp.route("/")
@login_required
def list_trips():
    return render_template("trips_list.html", trips=db_list_trips())


@trips_bp.route("/<int:trip_id>")
@login_required
def trip_detail(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    stops = get_trip_stops(trip_id)
    pois = get_trip_pois(trip_id)
    tracks = get_trip_tracks(trip_id)
    route = get_route_for_trip(trip_id, calculate=False)
    return render_template(
        "trip_detail.html",
        trip=trip,
        stops=stops,
        pois=pois,
        tracks=tracks,
        route=route,
        stops_json=rows_to_dicts(stops),
        pois_json=rows_to_dicts(pois),
        tracks_json=tracks,
        home_json=get_home_location(),
    )


@trips_bp.route("/<int:trip_id>/google-maps")
@login_required
def google_maps(trip_id):
    if not get_trip(trip_id):
        abort(404)
    return redirect(google_maps_url(trip_id))


@trips_bp.route("/<int:trip_id>/kml")
@login_required
def download_kml(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    filename = "".join(char if char.isalnum() else "-" for char in trip["title"].lower()).strip("-") or "trip"
    return Response(
        trip_kml(trip_id),
        mimetype="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": f"attachment; filename={filename}.kml"},
    )


@trips_bp.route("/<int:trip_id>/tracks/<int:track_id>/gpx")
@login_required
def download_track_gpx(trip_id, track_id):
    if not get_trip(trip_id):
        abort(404)
    track = get_trip_track(track_id)
    if not track or track["trip_id"] != trip_id:
        abort(404)
    base = os.path.abspath(config.TRACK_UPLOAD_FOLDER)
    path = os.path.abspath(os.path.join(base, track["stored_filename"]))
    if not path.startswith(base + os.sep) or not os.path.exists(path):
        abort(404)
    return send_file(
        path,
        mimetype="application/gpx+xml",
        as_attachment=True,
        download_name=track["original_filename"],
    )
