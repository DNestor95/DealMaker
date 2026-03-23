"""
Settings routes — view / update app-level configuration.

GET  /settings   → show current .env values (masked)
POST /settings   → update and persist to .env
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, redirect, render_template, request, url_for

bp = Blueprint("settings", __name__, url_prefix="/settings")

ENV_KEYS = [
    ("TOPREP_API_URL", "TopRep API URL", False),
    ("TOPREP_AUTH_TOKEN", "Auth Token (JWT)", True),
    ("SUPABASE_ANON_KEY", "Supabase Anon Key", True),
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
    return render_template("settings.html", fields=fields)


@bp.route("/", methods=["POST"])
def save_settings():
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
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return redirect(url_for("settings.settings"))
