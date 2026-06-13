"""Authentication routes."""
from datetime import datetime
from functools import wraps

import bcrypt
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

import config
from database import (
    ban_ip,
    get_db,
    get_failed_attempts,
    get_user_by_username,
    is_ip_banned,
    record_login_attempt,
    update_last_login,
    User,
)

auth_bp = Blueprint("auth", __name__)


def admin_required(func):
    @wraps(func)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("trips.list_trips"))
        return func(*args, **kwargs)

    return wrapped


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("trips.list_trips"))

    client_ip = request.remote_addr
    if is_ip_banned(client_ip):
        return render_template("login.html", banned=True), 403

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"

        if not username or not password:
            flash("Please enter both username and password.", "warning")
            return render_template("login.html")

        user_data = get_user_by_username(username)
        if user_data and bcrypt.checkpw(password.encode("utf-8"), user_data["password_hash"].encode("utf-8")):
            if not user_data["is_active"]:
                flash("Your account has been deactivated.", "danger")
                return render_template("login.html")

            record_login_attempt(client_ip, username, True)
            user = User(
                user_data["user_id"],
                user_data["username"],
                user_data["email"],
                user_data["first_name"],
                user_data["last_name"],
                user_data["role"],
                bool(user_data["is_active"]),
            )
            login_user(user, remember=remember)
            update_last_login(user.user_id)
            flash(f"Welcome back, {user.display_name}!", "success")
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/") and not next_page.startswith("//"):
                return redirect(next_page)
            return redirect(url_for("trips.list_trips"))

        record_login_attempt(client_ip, username, False)
        fail_count = get_failed_attempts(client_ip, config.LOGIN_FAIL_WINDOW)
        if fail_count >= config.LOGIN_FAIL_THRESHOLD:
            ban_ip(
                client_ip,
                f"Too many failed login attempts ({fail_count} in {config.LOGIN_FAIL_WINDOW} min)",
                config.LOGIN_BAN_DURATION,
            )
            return render_template("login.html", banned=True), 403
        flash("Invalid username or password.", "danger")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("auth.login"))
