"""
Settings routes — view / update app-level configuration.

GET  /settings   → show current .env values (masked)
POST /settings   → update and persist to .env

Note: the data destination (Supabase URL and publishable key) is fixed and
cannot be changed via the UI.  Only credentials and optional app URL are
user-configurable.
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from dealmaker_postgres import check_database_connection, database_url_from_env
from app.supabase_client import _TOPREP_SUPABASE_URL

bp = Blueprint("settings", __name__, url_prefix="/settings")

# Fields that users are allowed to edit.  Destination URL and publishable key
# are intentionally excluded — the TopRep Supabase server is the only valid
# target and the publishable key is baked into the application.
ENV_KEYS = [
    ("TOPREP_AUTH_TOKEN", "Auth Token (JWT)", True),
    ("DATABASE_URL", "Postgres Connection URL", True),
    ("SUPABASE_SERVICE_ROLE_KEY", "Supabase Service Role Key (Admin Auth)", True),
    ("TOPREP_APP_URL", "TopRep App URL (for QA login links)", False),
]


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


@bp.route("/", methods=["GET"])
def settings():
    fields = []
    for key, label, secret in ENV_KEYS:
        val = os.getenv(key, "")
        fields.append({
            "key": key,
            "label": label,
            "secret": secret,
            "display": _mask(val) if (secret and val) else val,
            "configured": bool(val),
        })
    return render_template("settings.html", fields=fields, fixed_destination=_TOPREP_SUPABASE_URL)


@bp.route("/", methods=["POST"])
def save_settings():
    # On Vercel the project root is read-only; only /tmp is writable.
    if os.environ.get("VERCEL"):
        env_path = Path("/tmp/.env")
    else:
        env_path = Path(__file__).parent.parent.parent / ".env"

    existing: dict[str, str] = {}

    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip().strip('"').strip("'")

    for key, _, _ in ENV_KEYS:
        new_val = request.form.get(key, "").strip()
        if new_val:
            existing[key] = new_val
            os.environ[key] = new_val

    lines = [f'{k}="{v}"' for k, v in existing.items()]
    try:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        flash("Settings saved.", "success")
    except OSError as exc:
        flash(f"Settings applied for this session but could not be persisted: {exc}", "warning")

    return redirect(url_for("settings.settings"))


@bp.route("/test-connection", methods=["POST"])
def test_connection():
    """Test the configured direct Postgres or Supabase REST connection and return JSON."""
    if database_url_from_env().strip():
        return jsonify(check_database_connection())

    from app.supabase_client import check_connection  # local import avoids circular dep

    result = check_connection()
    return jsonify(result)


@bp.route("/fetch-token", methods=["POST"])
def fetch_token():
    """Sign in to Supabase with email + password and save the resulting JWT as TOPREP_AUTH_TOKEN.

    The credentials are used server-side only and are never stored or logged.
    """
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password are required."}), 400

    import json
    import ssl
    from urllib import error as url_error, request as url_request

    from app.supabase_client import _TOPREP_SUPABASE_URL, _anon_key, _ssl_ctx

    auth_url = f"{_TOPREP_SUPABASE_URL}/auth/v1/token?grant_type=password"
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "apikey": _anon_key(),
    }
    req = url_request.Request(auth_url, data=body, headers=headers, method="POST")
    try:
        with url_request.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except url_error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:300]
        return jsonify({"ok": False, "error": f"HTTP {exc.code}: {body_text}"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    token = payload.get("access_token")
    if not token:
        return jsonify({"ok": False, "error": "No access_token in response. Check your credentials."}), 400

    # Persist to environment and .env file
    os.environ["TOPREP_AUTH_TOKEN"] = token

    if os.environ.get("VERCEL"):
        env_path = Path("/tmp/.env")
    else:
        env_path = Path(__file__).parent.parent.parent / ".env"

    existing: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip().strip('"').strip("'")

    existing["TOPREP_AUTH_TOKEN"] = token
    lines = [f'{k}="{v}"' for k, v in existing.items()]
    try:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass  # Session-only; already set in os.environ

    # Return just a prefix so the token isn't fully exposed in the JSON response
    return jsonify({"ok": True, "token_prefix": token[:20] + "..."})
