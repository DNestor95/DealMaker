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

from app.supabase_client import _TOPREP_SUPABASE_URL

bp = Blueprint("settings", __name__, url_prefix="/settings")

# Fields that users are allowed to edit.  Destination URL and publishable key
# are intentionally excluded — the TopRep Supabase server is the only valid
# target and the publishable key is baked into the application.
ENV_KEYS = [
    ("TOPREP_AUTH_TOKEN", "Auth Token (JWT)", True),
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
    """Test the live Supabase REST API connection and return JSON."""
    from app.supabase_client import check_connection  # local import avoids circular dep

    result = check_connection()
    return jsonify(result)
