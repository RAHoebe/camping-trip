"""Trip browsing routes for logged-in users."""
from flask import Blueprint, abort, render_template
from flask_login import login_required

from database import get_home_location, get_trip, get_trip_pois, get_trip_stops, list_trips as db_list_trips, rows_to_dicts
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
    route = get_route_for_trip(trip_id)
    return render_template(
        "trip_detail.html",
        trip=trip,
        stops=stops,
        pois=pois,
        route=route,
        stops_json=rows_to_dicts(stops),
        pois_json=rows_to_dicts(pois),
        home_json=get_home_location(),
    )
