"""Authentication routes.

GET  /login   → login form
POST /login   → validate credentials and set session
GET  /logout  → clear session and redirect to login
"""
from __future__ import annotations

import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from urllib.parse import urlparse

bp = Blueprint("auth", __name__)

_USERNAME = "dnestor95"
_PASSWORD = "Dn0497539"


def _is_safe_next(url: str) -> bool:
    """Return True only for relative (same-origin) redirect targets."""
    parsed = urlparse(url)
    return not parsed.netloc and not parsed.scheme


@bp.route("/login", methods=["GET"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("stores.index"))
    return render_template("login.html")


@bp.route("/login", methods=["POST"])
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    username_ok = secrets.compare_digest(username, _USERNAME)
    password_ok = secrets.compare_digest(password, _PASSWORD)
    if username_ok and password_ok:
        session["logged_in"] = True
        next_url = request.args.get("next", "")
        if not _is_safe_next(next_url):
            next_url = url_for("stores.index")
        return redirect(next_url or url_for("stores.index"))
    return render_template("login.html", error="Invalid username or password.")


@bp.route("/logout")
def logout():
    session.pop("logged_in", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
