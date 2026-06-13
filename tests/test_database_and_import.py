import os
from xml.etree import ElementTree as ET

import bcrypt
import config
from database import get_db, get_setting, get_trip_stops, get_user_by_username, init_database, stop_signature
from export_service import google_maps_url, trip_kml
from gpx_import import import_gpx_file
from blueprints.api import _distance_km
from route_service import _request_graphhopper, get_route_for_trip
from traffic_service import parse_datex_events, warnings_for_route


def configure_temp_db(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_FOLDER", str(data_dir))
    monkeypatch.setattr(config, "DATABASE_PATH", str(data_dir / "test.db"))
    monkeypatch.setattr(config, "DEFAULT_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "DEFAULT_ADMIN_PASSWORD", "change-me-please")
    monkeypatch.setattr(config, "DEFAULT_ADMIN_EMAIL", "admin@example.local")
    init_database()


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


def test_graphhopper_rate_limit_headers_are_stored(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "GRAPHHOPPER_API_KEY", "test-key")
    monkeypatch.setattr(config, "GRAPHHOPPER_BASE_URL", "https://graphhopper.example")

    class FakeResponse:
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

    payload = trip_kml(trip_id)
    root = ET.fromstring(payload)
    text = payload.decode("utf-8")
    assert root.tag.endswith("kml")
    assert "1. Camp" in text
    assert "POI: View" in text
    assert "5.000000,52.000000,0" in text
    assert "6.000000,53.000000,0" in text


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


def test_datex_fixture_parses_to_normalized_traffic_event():
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <d2LogicalModel>
      <payloadPublication>
        <situation>
          <situationRecord id="closure-1">
            <situationRecordCreationReference>A2 closure</situationRecordCreationReference>
            <overallStartTime>2026-07-01T08:00:00+00:00</overallStartTime>
            <overallEndTime>2026-07-01T18:00:00+00:00</overallEndTime>
            <roadName>A2</roadName>
            <comment>Road closed for works</comment>
            <groupOfLocations>
              <locationForDisplay>
                <latitude>51.5000</latitude>
                <longitude>5.5000</longitude>
              </locationForDisplay>
            </groupOfLocations>
          </situationRecord>
        </situation>
      </payloadPublication>
    </d2LogicalModel>"""

    events = parse_datex_events(payload)
    assert len(events) == 1
    assert events[0]["event_type"] == "closure"
    assert events[0]["severity"] == "closed"
    assert events[0]["road_name"] == "A2"
    assert "5.5" in events[0]["geometry_geojson"]


def test_datex_lane_restrictions_are_not_full_closures():
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <d2LogicalModel>
      <payloadPublication>
        <situation>
          <situationRecord id="lanes-1">
            <situationRecordCreationReference>RWS01_M1_CLOSED_LANES_D2</situationRecordCreationReference>
            <comment>Closed lanes on A12</comment>
            <groupOfLocations>
              <locationForDisplay>
                <latitude>52.0000</latitude>
                <longitude>5.0000</longitude>
              </locationForDisplay>
            </groupOfLocations>
          </situationRecord>
        </situation>
      </payloadPublication>
    </d2LogicalModel>"""

    events = parse_datex_events(payload)
    assert events[0]["event_type"] == "traffic_measure"
    assert events[0]["severity"] == "major"

    narrow_payload = payload.replace(b"RWS01_M1_CLOSED_LANES_D2", b"RWS01_M1_NARROW_LANES_D2").replace(b"Closed lanes", b"Narrow lanes")
    narrow_events = parse_datex_events(narrow_payload)
    assert narrow_events[0]["event_type"] == "traffic_measure"
    assert narrow_events[0]["severity"] == "major"


def test_datex_junction_closure_is_full_closure():
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <d2LogicalModel>
      <payloadPublication>
        <situation>
          <situationRecord id="junction-1">
            <situationRecordCreationReference>RWS01_M1_JUNCTION_CLOSURE_1_D2</situationRecordCreationReference>
            <comment>Junction closure</comment>
            <groupOfLocations>
              <locationForDisplay>
                <latitude>52.0000</latitude>
                <longitude>5.0000</longitude>
              </locationForDisplay>
            </groupOfLocations>
          </situationRecord>
        </situation>
      </payloadPublication>
    </d2LogicalModel>"""

    events = parse_datex_events(payload)
    assert events[0]["event_type"] == "closure"
    assert events[0]["severity"] == "closed"


def test_traffic_warnings_match_route_corridor_and_ignore_far_events(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "TRAFFIC_WARNINGS_ENABLED", True)
    monkeypatch.setattr(config, "TRAFFIC_ROUTE_CORRIDOR_METERS", 1000)
    route = {"route": {"type": "LineString", "coordinates": [[5.0, 52.0], [6.0, 52.0]]}}
    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Traffic')").lastrowid
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'closure', 'closed', 'Near closure', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00',
                    '{"type":"Point","coordinates":[5.5,52.001]}', 'near')
        """)
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'closure', 'closed', 'Far closure', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00',
                    '{"type":"Point","coordinates":[5.5,53.0]}', 'far')
        """)
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'unknown', 'info', 'Near informational detour', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00',
                    '{"type":"LineString","coordinates":[[5.4,52.001],[5.6,52.001]]}', 'info-near')
        """)

    warnings = warnings_for_route(cur, route=route)
    assert [warning["title"] for warning in warnings] == ["Near closure"]


def test_traffic_warnings_hide_speed_management_duplicates(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "TRAFFIC_WARNINGS_ENABLED", True)
    monkeypatch.setattr(config, "TRAFFIC_ROUTE_CORRIDOR_METERS", 1000)
    route = {"route": {"type": "LineString", "coordinates": [[5.0, 52.0], [6.0, 52.0]]}}
    geometry = '{"type":"LineString","coordinates":[[5.4,52.001],[5.6,52.001]]}'
    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Traffic')").lastrowid
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'traffic_measure', 'major', 'RWS01_M1_SPEED_MANAGEMENT_D2',
                    '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', ?, 'speed')
        """, (geometry,))
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'traffic_measure', 'major', 'RWS01_M1_CLOSED_LANES_D2',
                    '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', ?, 'lanes')
        """, (geometry,))

    warnings = warnings_for_route(cur, route=route)
    assert [warning["title"] for warning in warnings] == ["RWS01_M1_CLOSED_LANES_D2"]


def test_traffic_warnings_do_not_change_normal_route(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    calls = []

    def fake_graphhopper(points, avoid_events=None):
        calls.append(avoid_events)
        return {"type": "LineString", "coordinates": [[5.0, 52.0], [6.0, 52.0]]}, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    monkeypatch.setattr(config, "ROUTE_AVOID_CLOSURES_ENABLED", True)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO trips (title) VALUES ('Normal')").lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', 52.0, 6.0)
        """, (cur,))
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")

    route = get_route_for_trip(cur, refresh=True)
    assert route["status"] == "ok"
    assert calls == [None]


def test_hard_closure_avoidance_attempts_only_intersecting_closure(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)
    avoid_calls = []

    def fake_graphhopper(points, avoid_events=None):
        avoid_calls.append(avoid_events)
        return {"type": "LineString", "coordinates": [[float(points[0]["longitude"]), float(points[0]["latitude"])], [float(points[1]["longitude"]), float(points[1]["latitude"])]]}, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    monkeypatch.setattr(config, "ROUTE_AVOID_CLOSURES_ENABLED", True)
    monkeypatch.setattr(config, "TRAFFIC_ROUTE_CORRIDOR_METERS", 1000)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('Avoid')").lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', 52.0, 6.0)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'closure', 'closed', 'Near closure', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00',
                    '{"type":"Point","coordinates":[5.5,52.001]}', 'near')
        """)

    route = get_route_for_trip(trip_id, refresh=True, avoid_closures=True)
    assert route["status"] == "ok"
    assert avoid_calls[0] and avoid_calls[0][0]["title"] == "Near closure"
    assert route["legs"][0]["closure_avoidance"] == "avoided"


def test_failed_hard_closure_avoidance_falls_back_to_original_route(monkeypatch, tmp_path):
    configure_temp_db(monkeypatch, tmp_path)

    def fake_graphhopper(points, avoid_events=None):
        if avoid_events:
            raise RuntimeError("avoid not supported")
        return {"type": "LineString", "coordinates": [[5.0, 52.0], [6.0, 52.0]]}, 1000, 600

    monkeypatch.setattr("route_service._request_graphhopper", fake_graphhopper)
    monkeypatch.setattr(config, "ROUTE_AVOID_CLOSURES_ENABLED", True)
    monkeypatch.setattr(config, "TRAFFIC_ROUTE_CORRIDOR_METERS", 1000)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_latitude', '52.0')")
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('home_longitude', '5.0')")
        trip_id = conn.execute("INSERT INTO trips (title) VALUES ('Fallback')").lastrowid
        conn.execute("""
            INSERT INTO trip_stops (trip_id, name, arrival_date, latitude, longitude)
            VALUES (?, 'Camp', '2026-07-01', 52.0, 6.0)
        """, (trip_id,))
        conn.execute("""
            INSERT INTO traffic_events (source, country, event_type, severity, title, starts_at, ends_at, geometry_geojson, raw_source_id)
            VALUES ('test', 'NL', 'closure', 'closed', 'Near closure', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00',
                    '{"type":"Point","coordinates":[5.5,52.001]}', 'near')
        """)

    route = get_route_for_trip(trip_id, refresh=True, avoid_closures=True)
    assert route["status"] == "ok"
    assert route["legs"][0]["closure_avoidance"] == "failed"
    assert "avoid not supported" in route["legs"][0]["closure_avoidance_error"]
