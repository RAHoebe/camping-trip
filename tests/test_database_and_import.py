import os

import config
from database import get_db, get_trip_stops, get_user_by_username, init_database, stop_signature
from gpx_import import import_gpx_file
from blueprints.api import _distance_km
from route_service import get_route_for_trip


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
