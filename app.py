"""Camping Trip Planner Flask application."""
import logging
import os
import sys
import threading
from datetime import datetime

from flask import Flask, abort, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from database import cleanup_expired_bans, get_all_settings, get_user_by_id, init_database, is_ip_banned

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)
app.config.from_object(config)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per minute"], storage_uri="memory://")
app.limiter = limiter

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "info"

os.makedirs(config.DATA_FOLDER, exist_ok=True)
os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
init_database()


def _start_gpxfeed_background_update():
    if not config.GPXFEED_AUTO_UPDATE:
        return

    def worker():
        try:
            from gpx_import import update_gpxfeed_if_needed

            update_gpxfeed_if_needed(force=False)
        except Exception as exc:
            app.logger.warning("GpxFeed auto-update failed: %s", exc)

    thread = threading.Thread(target=worker, name="gpxfeed-update", daemon=True)
    thread.start()


_start_gpxfeed_background_update()


_version_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")
try:
    with open(_version_file, "r", encoding="utf-8") as version_file:
        APP_VERSION = version_file.read().strip()
except Exception:
    APP_VERSION = "unknown"


@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(int(user_id))


@app.before_request
def check_ip_ban():
    if request.path.startswith("/static/"):
        return
    if is_ip_banned(request.remote_addr):
        abort(403)


_request_counter = {"count": 0}


@app.before_request
def periodic_ban_cleanup():
    _request_counter["count"] += 1
    if _request_counter["count"] >= 100:
        _request_counter["count"] = 0
        cleanup_expired_bans()


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if config.SESSION_COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.template_filter("format_date")
def format_date_filter(date_str):
    if not date_str:
        return ""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        return d.strftime("%#d %B, %Y") if os.name == "nt" else d.strftime("%-d %B, %Y")
    except (TypeError, ValueError):
        return str(date_str)


@app.template_filter("date_span")
def date_span_filter(start, end):
    if start and end:
        return f"{format_date_filter(start)} to {format_date_filter(end)}"
    return format_date_filter(start or end)


@app.template_filter("format_km")
def format_km_filter(meters):
    if meters is None:
        return ""
    try:
        return f"{float(meters) / 1000:.0f} km"
    except (TypeError, ValueError):
        return ""


@app.template_filter("format_travel_time")
def format_travel_time_filter(seconds):
    if seconds is None:
        return ""
    try:
        total_minutes = int(round(float(seconds) / 60))
    except (TypeError, ValueError):
        return ""
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


@app.template_filter("trip_days")
def trip_days_filter(stops):
    if not stops:
        return 0
    dates = []
    for stop in stops:
        for key in ("arrival_date", "departure_date"):
            value = stop[key] if key in stop.keys() else None
            if value:
                try:
                    dates.append(datetime.strptime(str(value)[:10], "%Y-%m-%d").date())
                except ValueError:
                    pass
    if len(dates) < 2:
        return 1 if dates else 0
    return max(1, (max(dates) - min(dates)).days)


@app.context_processor
def inject_globals():
    settings = get_all_settings()
    update_available = False
    latest_version = None
    docker_hub_page = None
    if (
        current_user.is_authenticated
        and current_user.is_admin
        and settings.get("version_check_enabled", "true") == "true"
    ):
        from version_check import DOCKER_HUB_PAGE, get_latest_version, is_update_available

        latest_version = get_latest_version()
        if latest_version and is_update_available(APP_VERSION, latest_version):
            update_available = True
            docker_hub_page = DOCKER_HUB_PAGE

    return {
        "now": datetime.now,
        "app_version": APP_VERSION,
        "site_title": settings.get("site_title", "Camping Trip Planner"),
        "default_theme": settings.get("default_theme", "light"),
        "theme_color": settings.get("theme_color", "green"),
        "update_available": update_available,
        "latest_version": latest_version,
        "docker_hub_page": docker_hub_page,
    }


from blueprints.auth import auth_bp
from blueprints.trips import trips_bp
from blueprints.api import api_bp
from blueprints.admin import admin_bp

app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(trips_bp, url_prefix="/trips")
app.register_blueprint(api_bp, url_prefix="/api")
app.register_blueprint(admin_bp, url_prefix="/admin")

limiter.limit("5 per minute")(app.view_functions["auth.login"])


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("trips.list_trips"))
    return redirect(url_for("auth.login"))


@app.errorhandler(403)
def forbidden_error(error):
    return render_template("403.html"), 403


@app.errorhandler(404)
def not_found_error(error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    return render_template("500.html"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8034, debug=config.DEBUG)
