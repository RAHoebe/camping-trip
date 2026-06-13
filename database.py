"""SQLite database helpers for Camping Trip Planner."""
import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import bcrypt
from flask_login import UserMixin

import config


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class User(UserMixin):
    def __init__(self, user_id, username, email, first_name, last_name, role, is_active=True):
        self.user_id = user_id
        self.id = user_id
        self.username = username
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.role = role
        self.is_active_flag = is_active

    @property
    def display_name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        if self.first_name:
            return self.first_name
        return self.username

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_active(self):
        return self.is_active_flag

    def get_id(self):
        return str(self.id)


def _column_names(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _ensure_column(cursor, table, name, ddl):
    if name not in _column_names(cursor, table):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_database():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                first_name TEXT,
                last_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action_type TEXT NOT NULL,
                action_details TEXT,
                target_type TEXT,
                target_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (admin_id) REFERENCES users(user_id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                username TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_ips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL UNIQUE,
                reason TEXT,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                banned_by TEXT DEFAULT 'auto'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS security_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                ip_address TEXT,
                username TEXT,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                trip_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                start_date DATE,
                end_date DATE,
                is_public INTEGER DEFAULT 1,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS imported_campgrounds (
                campground_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'manual',
                source_id TEXT,
                name TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                description TEXT,
                website TEXT,
                phone TEXT,
                country TEXT,
                raw_data TEXT,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_id)
            )
        """)
        _ensure_column(cursor, "imported_campgrounds", "source_file", "TEXT")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_imported_campgrounds_source_name_coords ON imported_campgrounds(source, name, latitude, longitude)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trip_stops (
                stop_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                campground_id INTEGER,
                name TEXT NOT NULL,
                arrival_date DATE NOT NULL,
                departure_date DATE,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                address TEXT,
                website TEXT,
                phone TEXT,
                booking_reference TEXT,
                notes TEXT,
                is_last_stop INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trip_id) REFERENCES trips(trip_id) ON DELETE CASCADE,
                FOREIGN KEY (campground_id) REFERENCES imported_campgrounds(campground_id) ON DELETE SET NULL
            )
        """)
        _ensure_column(cursor, "trip_stops", "is_last_stop", "INTEGER DEFAULT 0")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pois (
                poi_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                stop_id INTEGER,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'custom',
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                address TEXT,
                website TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trip_id) REFERENCES trips(trip_id) ON DELETE CASCADE,
                FOREIGN KEY (stop_id) REFERENCES trip_stops(stop_id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS route_cache (
                cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                stop_signature TEXT NOT NULL,
                route_geojson TEXT,
                distance_m REAL,
                duration_s REAL,
                status TEXT NOT NULL DEFAULT 'ok',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(trip_id, provider, stop_signature),
                FOREIGN KEY (trip_id) REFERENCES trips(trip_id) ON DELETE CASCADE
            )
        """)
        _ensure_column(cursor, "route_cache", "route_legs_json", "TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS traffic_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                country TEXT,
                event_type TEXT NOT NULL DEFAULT 'unknown',
                severity TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                description TEXT,
                starts_at TEXT,
                ends_at TEXT,
                road_name TEXT,
                geometry_geojson TEXT NOT NULL,
                raw_source_id TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, raw_source_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS traffic_update_status (
                source TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                message TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                event_count INTEGER DEFAULT 0
            )
        """)

        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp ON audit_logs(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address)",
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_timestamp ON login_attempts(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_banned_ips_ip ON banned_ips(ip_address)",
            "CREATE INDEX IF NOT EXISTS idx_trips_dates ON trips(start_date, end_date)",
            "CREATE INDEX IF NOT EXISTS idx_trip_stops_trip_dates ON trip_stops(trip_id, arrival_date, departure_date)",
            "CREATE INDEX IF NOT EXISTS idx_pois_trip ON pois(trip_id)",
            "CREATE INDEX IF NOT EXISTS idx_imported_campgrounds_name ON imported_campgrounds(name)",
            "CREATE INDEX IF NOT EXISTS idx_traffic_events_source ON traffic_events(source)",
            "CREATE INDEX IF NOT EXISTS idx_traffic_events_time ON traffic_events(starts_at, ends_at)",
            "CREATE INDEX IF NOT EXISTS idx_traffic_events_type ON traffic_events(event_type, severity)",
        ]
        for index_sql in indexes:
            cursor.execute(index_sql)

        defaults = {
            "site_title": "Camping Trip Planner",
            "default_theme": "light",
            "theme_color": "green",
            "version_check_enabled": "true",
            "home_name": "Home",
            "home_latitude": "",
            "home_longitude": "",
        }
        for key, value in defaults.items():
            cursor.execute("INSERT OR IGNORE INTO site_settings (key, value) VALUES (?, ?)", (key, value))

        cursor.execute("SELECT COUNT(*) AS count FROM users")
        if cursor.fetchone()["count"] == 0:
            password_hash = bcrypt.hashpw(
                config.DEFAULT_ADMIN_PASSWORD.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            cursor.execute("""
                INSERT INTO users (username, password_hash, email, first_name, last_name, role, is_active)
                VALUES (?, ?, ?, 'Admin', 'User', 'admin', 1)
            """, (config.DEFAULT_ADMIN_USERNAME, password_hash, config.DEFAULT_ADMIN_EMAIL))
            logging.warning("Created default admin user '%s'. Change the password after first login.", config.DEFAULT_ADMIN_USERNAME)


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute("""
            SELECT user_id, username, email, first_name, last_name, role, is_active
            FROM users WHERE user_id = ?
        """, (user_id,)).fetchone()
        if not row:
            return None
        return User(row["user_id"], row["username"], row["email"], row["first_name"], row["last_name"], row["role"], bool(row["is_active"]))


def get_user_by_username(username):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def update_last_login(user_id):
    with get_db() as conn:
        conn.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))


def get_all_settings():
    with get_db() as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM site_settings")}


def get_setting(key, default=None):
    return get_all_settings().get(key, default)


def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)", (key, value))


def get_home_location():
    settings = get_all_settings()
    try:
        latitude = float(settings.get("home_latitude", ""))
        longitude = float(settings.get("home_longitude", ""))
    except (TypeError, ValueError):
        return None
    return {
        "name": settings.get("home_name") or "Home",
        "latitude": latitude,
        "longitude": longitude,
    }


def log_audit(admin_id, action_type, action_details, target_type=None, target_id=None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO audit_logs (admin_id, action_type, action_details, target_type, target_id)
            VALUES (?, ?, ?, ?, ?)
        """, (admin_id, action_type, action_details, target_type, target_id))


def record_login_attempt(ip_address, username, success):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO login_attempts (ip_address, username, success)
            VALUES (?, ?, ?)
        """, (ip_address, username, 1 if success else 0))
        conn.execute("""
            INSERT INTO security_log (event_type, ip_address, username, details)
            VALUES (?, ?, ?, ?)
        """, ("LOGIN_SUCCESS" if success else "LOGIN_FAILURE", ip_address, username, "Login attempt"))


def get_failed_attempts(ip_address, window_minutes):
    since = (datetime.now() - timedelta(minutes=window_minutes)).isoformat(sep=" ")
    with get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS count FROM login_attempts
            WHERE ip_address = ? AND success = 0 AND timestamp >= ?
        """, (ip_address, since)).fetchone()
        return row["count"]


def ban_ip(ip_address, reason, duration_minutes, banned_by="auto"):
    expires_at = (datetime.now() + timedelta(minutes=duration_minutes)).isoformat(sep=" ")
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO banned_ips (ip_address, reason, expires_at, banned_by)
            VALUES (?, ?, ?, ?)
        """, (ip_address, reason, expires_at, banned_by))
        conn.execute("""
            INSERT INTO security_log (event_type, ip_address, details)
            VALUES ('IP_BANNED', ?, ?)
        """, (ip_address, reason))


def is_ip_banned(ip_address):
    cleanup_expired_bans()
    with get_db() as conn:
        row = conn.execute("SELECT id FROM banned_ips WHERE ip_address = ?", (ip_address,)).fetchone()
        return row is not None


def cleanup_expired_bans():
    with get_db() as conn:
        conn.execute("""
            DELETE FROM banned_ips
            WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP
        """)


def list_trips():
    with get_db() as conn:
        return conn.execute("""
            SELECT t.*,
                   COUNT(DISTINCT s.stop_id) AS stop_count,
                   COUNT(DISTINCT p.poi_id) AS poi_count
            FROM trips t
            LEFT JOIN trip_stops s ON s.trip_id = t.trip_id
            LEFT JOIN pois p ON p.trip_id = t.trip_id
            GROUP BY t.trip_id
            ORDER BY COALESCE(t.start_date, '9999-12-31'), t.title
        """).fetchall()


def get_trip(trip_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()


def get_trip_stops(trip_id):
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM trip_stops
            WHERE trip_id = ?
            ORDER BY arrival_date, COALESCE(departure_date, arrival_date), stop_id
        """, (trip_id,)).fetchall()


def get_trip_pois(trip_id):
    with get_db() as conn:
        return conn.execute("""
            SELECT p.*, s.name AS stop_name
            FROM pois p
            LEFT JOIN trip_stops s ON s.stop_id = p.stop_id
            WHERE p.trip_id = ?
            ORDER BY p.category, p.name
        """, (trip_id,)).fetchall()


def stop_signature(stops):
    payload = [
        {
            "stop_id": stop["stop_id"],
            "arrival_date": stop["arrival_date"],
            "departure_date": stop["departure_date"],
            "lat": round(float(stop["latitude"]), 6),
            "lon": round(float(stop["longitude"]), 6),
        }
        for stop in stops
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
