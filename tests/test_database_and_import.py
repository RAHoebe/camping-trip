import io
import json
import os
from xml.etree import ElementTree as ET

import bcrypt
import config
from database import get_db, get_setting, get_trip_track, get_trip_tracks, get_trip_stops, get_user_by_username, init_database, stop_signature
from export_service import google_maps_url, trip_kml
from gpx_import import import_gpx_file
from gpx_tracks import GpxTrackError, parse_gpx_track
from blueprints.api import _distance_km
from route_service import _request_graphhopper, get_route_for_trip


def configure_temp_db(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_FOLDER", str(data_dir))
    monkeypatch.setattr(config, "DATABASE_PATH", str(data_dir / "test.db"))
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(data_dir / "uploads"))
    monkeypatch.setattr(config, "TRACK_UPLOAD_FOLDER", str(data_dir / "uploads" / "tracks"))
    monkeypatch.setattr(config, "DEFAULT_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "DEFAULT_ADMIN_PASSWORD", "change-me-please")
    monkeypatch.setattr(config, "DEFAULT_ADMIN_EMAIL", "admin@example.local")
    init_database()


def venlo_style_gpx(point_count=765):
    waypoints = "\n".join(
        f'<wpt lat="{51.30 + index / 1000:.6f}" lon="{6.20 + index / 1000:.6f}"><name>Waypoint {index}</name></wpt>'
        for index in range(1, 10)
    )
    track_points = "\n".join(
        f'<trkpt lat="{51.38 + index / 10000:.6f}" lon="{6.27 + index / 10000:.6f}"><ele>50</ele></trkpt>'
        for index in range(point_count)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
      <metadata><name>Venlo</name></metadata>
      {waypoints}
      <trk><name>Venlo Track</name><trkseg>{track_points}</trkseg></trk>
    </gpx>""".encode("utf-8")


def test_init_database_creates_default_admin(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    user = get_user_by_username("admin")
    assert user is not None
    assert user["role"] == "admin"


def test_gpx_import_deduplicates_waypoints(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    gpx = b"""<?xml version="1.0"?>
    <gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
      <wpt lat="52.1" lon="5.1">
        <name>Forest Camp</name>
        <desc>Quiet place</desc>
      </wpt>
    </gpx>"""

    first = import_gpx_file("campgrounds.gpx", gpx)
    second = import_gpx_file("campgrounds.gpx", gpx)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["skipped"] == 1
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM imported_campgrounds").fetchone()
    assert row["count"] == 1


def test_gpx_track_parser_imports_venlo_style_track_and_waypoints():
    parsed = parse_gpx_track("venlo.gpx", venlo_style_gpx())

    assert parsed["name"] == "Venlo"
    assert parsed["source_kind"] == "track"
    assert len(parsed["line"]["coordinates"]) == 765
    assert len(parsed["waypoints"]) == 9
    assert parsed["waypoints"][0]["name"] == "Waypoint 1"
    assert parsed["distance_m"] > 0


def test_gpx_track_parser_supports_route_points():
    gpx = b"""<?xml version="1.0"?>
    <gpx version="1.1" creator="pytest">
      <rte><name>Short Route</name>
        <rtept lat="52.0" lon="5.0" />
        <rtept lat="52.1" lon="5.1" />
      </rte>
    </gpx>"""

    parsed = parse_gpx_track("route.gpx", gpx)

    assert parsed["name"] == "Short Route"
    assert parsed["source_kind"] == "route"
    assert parsed["line"]["coordinates"] == [[5.0, 52.0], [5.1, 52.1]]


def test_gpx_track_parser_rejects_invalid_payload():
    try:
        parse_gpx_track("notes.txt", b"not gpx")
    except GpxTrackError as exc:
        assert "Invalid GPX file" in str(exc)
    else:
        raise AssertionError("Invalid GPX payload should fail")


def test_trip_tracks_cascade_with_trip_delete(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    with get_db() as conn:
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('Tracks')").lastrowid
        track_id = conn.execute("""
            INSERT INTO trip_tracks
                (trip_id, name, activity_type, original_filename, stored_filename, line_geojson, waypoints_json)
            VALUES (?, 'Loop', 'cycling', 'loop.gpx', 'loop.gpx', ?, '[]')
        """, (trip_id, json.dumps({"type": "LineString", "coordinates": [[5.0, 52.0], [5.1, 52.1]]}))).lastrowid
        conn.execute("DELETE FROM trips WHERE trip_id = ?", (trip_id,))

    assert get_trip_track(track_id) is None


def test_stop_signature_uses_arrival_date_order(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Summer')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Second', '2026-07-02', 53.0, 6.0)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'First', '2026-07-01', 52.0, 5.0)
        """, (trip_id,))

    stops = get_trip_stops(trip_id)
    assert [stop["name"] for stop in stops] == ["First", "Second"]
    assert len(stop_signature(stops)) == 64


def test_distance_km_supports_radius_search():
    near = _distance_km(52.0, 5.0, 52.1, 5.0)
    far = _distance_km(52.0, 5.0, 53.0, 5.0)
    assert near < 20
    assert far > 20


def test_home_plus_one_stop_is_routeable(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)

    def fake_graphhopper(points):
        assert points[0]["is_home"] is True
        assert points[1]["name"] == "First Camp"
        return {"type": "LineString", "coordinates": [[5.0, 52.0], [5.5, 52.5]]}, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_name', 'Home')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Home Route')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'First Camp', '2026-07-01', 52.5, 5.5)
        """, (trip_id,))

    route = get_route_for_trip(trip_id, refresh=True)
    assert route["status"] == "ok"
    assert route["home"]["name"] == "Home"
    assert route["legs"][0]["distance_m"] == 1000
    assert route["stop_metrics"][1]["cumulative_distance_m"] == 1000


def test_cache_only_route_does_not_call_provider(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)

    def fail_graphhopper(points):
        raise AssertionError("GraphHopper should not be called without refresh")

    monkeypatch.setattr("route_service._request_graphhopper", fail_graphhopper)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Manual Route')").lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', 52.5, 5.5)
        """, (cur,))

    route = get_route_for_trip(cur, calculate=False)
    assert route["status"] == "manual_refresh_required"
    assert route["route"] is None


def test_graphhopper_rate_limit_headers_are_stored(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "GRAPHHOPPER_API_KEY", "test-key")
    monkeypatch.setattr(config, "GRAPHHOPPER_BASE_URL", "https://graphhopper.example")
    monkeypatch.setattr(config, "GRAPHHOPPER_LEG_DELAY_SECONDS", 0)

    class FakeResponse:
        status_code = 200
        ok = True
        headers = {
            "X-RateLimit-Remaining": "12345",
            "X-RateLimit-Limit": "100000",
            "X-RateLimit-Reset": "3600",
        }

        def json(self):
            return {
                "paths": [{
                    "points": {"coordinates": [[5.0, 52.0], [5.5, 52.5]]},
                    "distance": 1000,
                    "time": 600000,
                }]
            }

    monkeypatch.setattr("route_service.requests.post", lambda *args, **kwargs: FakeResponse())
    _request_graphhopper([
        {"latitude": 52.0, "longitude": 5.0},
        {"latitude": 52.5, "longitude": 5.5},
    ])

    assert get_setting("graphhopper_credits_remaining") == "12345"
    assert get_setting("graphhopper_credits_limit") == "100000"


def test_graphhopper_429_is_retried_once(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "GRAPHHOPPER_API_KEY", "test-key")
    monkeypatch.setattr(config, "GRAPHHOPPER_BASE_URL", "https://graphhopper.example")
    monkeypatch.setattr(config, "GRAPHHOPPER_LEG_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "GRAPHHOPPER_429_RETRY_SECONDS", 0)
    monkeypatch.setattr(config, "GRAPHHOPPER_429_RETRIES", 1)
    calls = []

    class FakeResponse:
        headers = {}
        reason = ""
        text = ""

        def __init__(self, status_code):
            self.status_code = status_code
            self.ok = status_code == 200

        def json(self):
            if self.status_code == 429:
                return {"message": "Minutely API limit heavily violated"}
            return {
                "paths": [{
                    "points": {"coordinates": [[5.0, 52.0], [5.5, 52.5]]},
                    "distance": 1000,
                    "time": 600000,
                }]
            }

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(429 if len(calls) == 1 else 200)

    monkeypatch.setattr("route_service.requests.post", fake_post)
    route, distance, duration = _request_graphhopper([
        {"latitude": 52.0, "longitude": 5.0},
        {"latitude": 52.5, "longitude": 5.5},
    ])

    assert len(calls) == 2
    assert route["coordinates"] == [[5.0, 52.0], [5.5, 52.5]]
    assert distance == 1000
    assert duration == 600


def test_route_refresh_reuses_cached_legs(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    calls = []

    def fake_graphhopper(points):
        calls.append([point["name"] for point in points])
        start = points[0]
        end = points[1]
        return {
            "type": "LineString",
            "coordinates": [
                [float(start["longitude"]), float(start["latitude"])],
                [float(end["longitude"]), float(end["latitude"])],
            ],
        }, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('Cached Legs')").lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'First Camp', '2026-07-01', 52.5, 5.5)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Second Camp', '2026-07-02', 53.0, 6.0)
        """, (trip_id,))

    first = get_route_for_trip(trip_id, refresh=True)
    assert first["status"] == "ok"
    assert len(calls) == 2

    with get_db() as conn:
        conn.execute("DELETE FROM route_cache WHERE trip_id = ?", (trip_id,))

    second = get_route_for_trip(trip_id, refresh=True)
    assert second["status"] == "ok"
    assert len(calls) == 2
    assert second["distance_m"] == 2000


def test_last_stop_adds_return_home_leg(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    calls = []

    def fake_graphhopper(points):
        calls.append([point["name"] for point in points])
        return {"type": "LineString", "coordinates": [[5.0, 52.0], [5.5, 52.5]]}, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_name', 'Home')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Return Home')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude, is_last_stop)
            VALUES (?, 'Last Camp', '2026-07-01', 52.5, 5.5, 1)
        """, (trip_id,))

    route = get_route_for_trip(trip_id, refresh=True)
    assert route["status"] == "ok"
    assert route["return_home_metric"]["to_stop_id"] == "__home_return__"
    assert route["return_home_metric"]["cumulative_distance_m"] == 2000


def test_admin_stop_form_defaults_to_previous_departure_and_calculates_nights(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()

    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title, start_date) VALUES ('Limburg', '2026-06-15')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude)
            VALUES (?, 'First', '2026-06-15', '2026-06-17', 51.1, 5.7)
        """, (trip_id,))

    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})
    response = client.get(f"/admin/trips/{trip_id}/stops/create?lat=51.2&lon=5.8&name=Second")
    assert response.status_code == 200
    assert b'value="2026-06-17"' in response.data
    assert b'name="nights"' in response.data

    response = client.post(f"/admin/trips/{trip_id}/stops/create", data={
        "name": "Second",
        "arrival_date": "2026-06-17",
        "nights": "2",
        "latitude": "51.2",
        "longitude": "5.8",
    })
    assert response.status_code == 302
    stops = get_trip_stops(trip_id)
    assert stops[-1]["departure_date"] == "2026-06-19"


def test_admin_options_save_title_theme_and_color(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})

    response = client.post("/admin/options", data={
        "site_title": "Road Trip Planner",
        "default_theme": "dark",
        "theme_color": "purple",
        "version_check_enabled": "false",
        "home_name": "Home",
        "home_latitude": "",
        "home_longitude": "",
    })

    assert response.status_code == 302
    assert get_setting("site_title") == "Road Trip Planner"
    assert get_setting("default_theme") == "dark"
    assert get_setting("theme_color") == "purple"


def test_admin_can_upload_edit_and_download_trip_track(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})

    with get_db() as conn:
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('Track Trip')").lastrowid

    response = client.post(
        f"/admin/trips/{trip_id}/tracks/create",
        data={
            "gpx_file": (io.BytesIO(venlo_style_gpx()), "venlo.gpx"),
            "activity_type": "cycling",
            "show_on_map": "on",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    tracks = get_trip_tracks(trip_id)
    assert len(tracks) == 1
    assert tracks[0]["name"] == "Venlo"
    assert tracks[0]["activity_type"] == "cycling"
    assert tracks[0]["show_on_map"] is True
    assert tracks[0]["distance_m"] > 0
    assert len(tracks[0]["line"]["coordinates"]) == 765
    assert os.path.exists(os.path.join(config.TRACK_UPLOAD_FOLDER, tracks[0]["stored_filename"]))

    track_id = tracks[0]["track_id"]
    response = client.post(f"/admin/tracks/{track_id}/edit", data={
        "name": "Forest Walk",
        "activity_type": "hiking",
    })
    assert response.status_code == 302
    track = get_trip_track(track_id)
    assert track["name"] == "Forest Walk"
    assert track["activity_type"] == "hiking"
    assert track["show_on_map"] is False

    admin_page = client.get(f"/admin/trips/{trip_id}")
    public_page = client.get(f"/trips/{trip_id}")
    assert b"tracks:" in admin_page.data
    assert b"Upload GPX Track" in admin_page.data
    assert b"tracks:" in public_page.data
    assert b'id="mapToolbar"' in public_page.data
    assert b"trip-head-actions" in public_page.data
    assert b"track-meta-row" in public_page.data
    assert b"Komoot import help" not in public_page.data
    assert b"Komoot import help" not in admin_page.data

    response = client.get(f"/trips/{trip_id}/tracks/{track_id}/gpx")
    assert response.status_code == 200
    assert response.mimetype == "application/gpx+xml"
    assert b"<gpx" in response.data


def test_admin_rejects_invalid_track_upload(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})
    with get_db() as conn:
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('Bad Upload')").lastrowid

    response = client.post(
        f"/admin/trips/{trip_id}/tracks/create",
        data={"gpx_file": (io.BytesIO(b"not gpx"), "notes.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"Track uploads must be GPX files." in response.data
    assert get_trip_tracks(trip_id) == []


def test_route_api_requires_manual_refresh_to_calculate(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    calls = []

    def fake_graphhopper(points):
        calls.append(points)
        return {"type": "LineString", "coordinates": [[5.0, 52.0], [5.5, 52.5]]}, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('API Route')").lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', 52.5, 5.5)
        """, (trip_id,))

    response = client.get(f"/api/trips/{trip_id}/route")
    assert response.get_json()["status"] == "manual_refresh_required"
    assert calls == []

    response = client.get(f"/api/trips/{trip_id}/route?refresh=1")
    assert response.get_json()["status"] == "ok"
    assert len(calls) == 1


def test_marking_last_stop_clears_other_last_stop(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()

    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title, start_date) VALUES ('Limburg', '2026-06-15')")
        trip_id = cur.lastrowid
        first = conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude, is_last_stop)
            VALUES (?, 'First', '2026-06-15', '2026-06-17', 51.1, 5.7, 1)
        """, (trip_id,)).lastrowid
        second = conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude)
            VALUES (?, 'Second', '2026-06-17', '2026-06-19', 51.2, 5.8)
        """, (trip_id,)).lastrowid

    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})
    response = client.post(f"/admin/stops/{second}/edit", data={
        "name": "Second",
        "arrival_date": "2026-06-17",
        "nights": "2",
        "latitude": "51.2",
        "longitude": "5.8",
        "is_last_stop": "on",
    })
    assert response.status_code == 302
    with get_db() as conn:
        rows = conn.execute("SELECT stop_id, is_last_stop FROM trip_stops ORDER BY stop_id").fetchall()
    assert [(row["stop_id"], row["is_last_stop"]) for row in rows] == [(first, 0), (second, 1)]


def test_google_maps_url_uses_home_ordered_stops_and_return_home(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_name', 'Home')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.000000')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.000000')")
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Export')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Second', '2026-07-02', 54.0, 7.0)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude, is_last_stop)
            VALUES (?, 'First', '2026-07-01', 53.0, 6.0, 1)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO pois (trip_id, name, category, latitude, longitude)
            VALUES (?, 'View', 'viewpoint', 55.0, 8.0)
        """, (trip_id,))

    url = google_maps_url(trip_id)
    assert "origin=52.000000%2C5.000000" in url
    assert "destination=52.000000%2C5.000000" in url
    assert "53.000000%2C6.000000" in url
    assert "54.000000%2C7.000000" in url
    assert "55.000000%2C8.000000" not in url


def test_trip_kml_includes_places_and_fallback_route(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr("export_service.get_route_for_trip", lambda trip_id: {"route": None})
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_name', 'Home')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        cur = conn.execute("INSERT INTO trips (title) VALUES ('KML Trip')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', '2026-07-02', 53.0, 6.0)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO pois (trip_id, name, category, latitude, longitude, notes)
            VALUES (?, 'View', 'viewpoint', 54.0, 7.0, 'Nice')
        """, (trip_id,))
        conn.execute("""
            INSERT INTO trip_tracks
                (trip_id, name, activity_type, original_filename, stored_filename, line_geojson, waypoints_json)
            VALUES (?, 'Forest Loop', 'hiking', 'forest.gpx', 'forest.gpx', ?, '[]')
        """, (trip_id, json.dumps({"type": "LineString", "coordinates": [[7.0, 54.0], [7.1, 54.1]]})))

    payload = trip_kml(trip_id)
    root = ET.fromstring(payload)
    text = payload.decode("utf-8")
    assert root.tag.endswith("kml")
    assert "1. Camp" in text
    assert "POI: View" in text
    assert "Hiking: Forest Loop" in text
    assert "5.000000,52.000000,0" in text
    assert "6.000000,53.000000,0" in text
    assert "7.100000,54.100000,0" in text


def test_admin_sync_trip_dates_from_campsites(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title, start_date, end_date) VALUES ('Sync', '2026-01-01', '2026-01-02')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude)
            VALUES (?, 'First', '2026-06-15', '2026-06-17', 51.0, 5.0)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude)
            VALUES (?, 'Second', '2026-06-20', '2026-06-23', 52.0, 6.0)
        """, (trip_id,))

    client.post("/auth/login", data={"username": "admin", "password": "change-me-please"})
    response = client.post(f"/admin/trips/{trip_id}/sync-dates")
    assert response.status_code == 302
    with get_db() as conn:
        trip = conn.execute("SELECT start_date, end_date FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
    assert trip["start_date"] == "2026-06-15"
    assert trip["end_date"] == "2026-06-23"


def test_normal_user_can_access_trip_exports(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("GPXFEED_AUTO_UPDATE", "false")
    monkeypatch.setattr(config, "GPXFEED_AUTO_UPDATE", False)
    from app import app

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    password_hash = bcrypt.hashpw(b"viewer-password", bcrypt.gensalt()).decode("utf-8")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (username, password_hash, email, role, is_active)
            VALUES ('viewer', ?, 'viewer@example.local', 'user', 1)
        """, (password_hash,))
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Viewer Trip')")
        trip_id = cur.lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, departure_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', '2026-07-02', 53.0, 6.0)
        """, (trip_id,))

    client.post("/auth/login", data={"username": "viewer", "password": "viewer-password"})
    maps_response = client.get(f"/trips/{trip_id}/google-maps")
    kml_response = client.get(f"/trips/{trip_id}/kml")
    assert maps_response.status_code == 302
    assert "google.com/maps" in maps_response.headers["Location"]
    assert kml_response.status_code == 200
    assert b"Viewer Trip" in kml_response.data
