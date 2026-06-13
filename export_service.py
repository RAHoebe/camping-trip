"""Trip export helpers for Google Maps and KML."""
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from database import get_home_location, get_trip, get_trip_pois, get_trip_stops
from route_service import get_route_for_trip


def _point_label(point):
    return f"{float(point['latitude']):.6f},{float(point['longitude']):.6f}"


def _trip_points(home, stops):
    points = []
    if home:
        points.append(home)
    points.extend(stops)
    if home and any(stop["is_last_stop"] if "is_last_stop" in stop.keys() else 0 for stop in stops):
        points.append(home)
    return points


def google_maps_url(trip_id):
    stops = get_trip_stops(trip_id)
    home = get_home_location()
    points = _trip_points(home, stops)
    if not points:
        return "https://www.google.com/maps"
    if len(points) == 1:
        return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": _point_label(points[0])})

    origin = points[0]
    destination = points[-1]
    waypoints = points[1:-1]
    params = {
        "api": "1",
        "travelmode": "driving",
        "origin": _point_label(origin),
        "destination": _point_label(destination),
    }
    if waypoints:
        params["waypoints"] = "|".join(_point_label(point) for point in waypoints)
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def _coord_text(lon, lat):
    return f"{float(lon):.6f},{float(lat):.6f},0"


def _add_placemark(folder, name, description, lon, lat):
    placemark = ET.SubElement(folder, "Placemark")
    ET.SubElement(placemark, "name").text = name
    if description:
        ET.SubElement(placemark, "description").text = description
    point = ET.SubElement(placemark, "Point")
    ET.SubElement(point, "coordinates").text = _coord_text(lon, lat)


def _fallback_line_coordinates(home, stops):
    points = _trip_points(home, stops)
    return [[float(point["longitude"]), float(point["latitude"])] for point in points]


def trip_kml(trip_id):
    trip = get_trip(trip_id)
    stops = get_trip_stops(trip_id)
    pois = get_trip_pois(trip_id)
    home = get_home_location()
    route = get_route_for_trip(trip_id)
    line_coordinates = []
    if route.get("route") and route["route"].get("coordinates"):
        line_coordinates = route["route"]["coordinates"]
    else:
        line_coordinates = _fallback_line_coordinates(home, stops)

    ET.register_namespace("", "http://www.opengis.net/kml/2.2")
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, "Document")
    ET.SubElement(document, "name").text = trip["title"] if trip else "Camping Trip"

    places = ET.SubElement(document, "Folder")
    ET.SubElement(places, "name").text = "Places"
    if home:
        _add_placemark(places, home["name"] or "Home", "Home location", home["longitude"], home["latitude"])
    for index, stop in enumerate(stops, start=1):
        dates = f"{stop['arrival_date']} to {stop['departure_date']}" if stop["departure_date"] else stop["arrival_date"]
        _add_placemark(places, f"{index}. {stop['name']}", dates, stop["longitude"], stop["latitude"])
    for poi in pois:
        description = poi["category"] or "POI"
        if poi["notes"]:
            description = f"{description}: {poi['notes']}"
        _add_placemark(places, f"POI: {poi['name']}", description, poi["longitude"], poi["latitude"])

    if line_coordinates:
        placemark = ET.SubElement(document, "Placemark")
        ET.SubElement(placemark, "name").text = "Route"
        line = ET.SubElement(placemark, "LineString")
        ET.SubElement(line, "tessellate").text = "1"
        ET.SubElement(line, "coordinates").text = " ".join(
            _coord_text(coord[0], coord[1]) for coord in line_coordinates
        )

    return ET.tostring(kml, encoding="utf-8", xml_declaration=True)
