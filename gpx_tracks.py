"""Helpers for importing user-uploaded GPX tracks."""
import math
import os
from xml.etree import ElementTree


class GpxTrackError(ValueError):
    """Raised when an uploaded GPX file cannot be used as a trip track."""


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(node, name):
    return [child for child in list(node) if _local_name(child.tag) == name]


def _descendants(node, name):
    return [child for child in node.iter() if _local_name(child.tag) == name]


def _child_text(node, name):
    for child in _children(node, name):
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _coordinates(nodes):
    coordinates = []
    for point in nodes:
        try:
            lat = float(point.attrib["lat"])
            lon = float(point.attrib["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        coordinates.append([lon, lat])
    return coordinates


def _distance_m(coordinates):
    total = 0.0
    radius = 6371008.8
    for index in range(1, len(coordinates)):
        lon1, lat1 = coordinates[index - 1]
        lon2, lat2 = coordinates[index]
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lon2 - lon1)
        a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        total += radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return total


def _waypoints(root):
    waypoints = []
    for waypoint in _descendants(root, "wpt"):
        try:
            lat = float(waypoint.attrib["lat"])
            lon = float(waypoint.attrib["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        waypoints.append({
            "name": _child_text(waypoint, "name") or "Waypoint",
            "latitude": lat,
            "longitude": lon,
        })
    return waypoints


def _metadata_name(root):
    for metadata in _children(root, "metadata"):
        name = _child_text(metadata, "name")
        if name:
            return name
    return ""


def parse_gpx_track(filename, payload):
    """Return normalized track data from GPX payload bytes."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise GpxTrackError(f"Invalid GPX file: {exc}") from exc

    if _local_name(root.tag) != "gpx":
        raise GpxTrackError("Uploaded file is not a GPX document.")

    tracks = _descendants(root, "trk")
    routes = _descendants(root, "rte")
    source_nodes = tracks or routes
    point_name = "trkpt" if tracks else "rtept"
    coordinates = []
    for source in source_nodes:
        coordinates.extend(_coordinates(_descendants(source, point_name)))

    if len(coordinates) < 2:
        raise GpxTrackError("GPX file must contain at least two track or route points.")

    first_source_name = ""
    if source_nodes:
        first_source_name = _child_text(source_nodes[0], "name")
    fallback_name = os.path.splitext(os.path.basename(filename or ""))[0] or "GPX Track"
    name = _metadata_name(root) or first_source_name or fallback_name

    return {
        "name": name,
        "line": {"type": "LineString", "coordinates": coordinates},
        "waypoints": _waypoints(root),
        "distance_m": _distance_m(coordinates),
        "point_count": len(coordinates),
        "source_kind": "track" if tracks else "route",
    }
