"""
Supabase client helper.
Reads connection details from the .env file (same format as v1).
"""
from __future__ import annotations

import json
import os
from http import HTTPStatus
from pathlib import Path
from urllib import error, request


def _load_env() -> None:
    path = Path(__file__).parent.parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()


def _base_url() -> str:
    url = os.getenv("TOPREP_API_URL", "").rstrip("/")
    # Strip down to the Supabase project root
    return url.split("/functions/")[0].split("/rest/")[0]


def _headers() -> dict[str, str]:
    token = os.getenv("TOPREP_AUTH_TOKEN", "")
    apikey = os.getenv("SUPABASE_ANON_KEY", "")
    h: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    if apikey:
        h["apikey"] = apikey
    return h


def rest_get(path: str, params: dict | None = None) -> list[dict]:
    """Simple REST GET against the Supabase REST API."""
    url = f"{_base_url()}/rest/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = request.Request(url, headers=_headers(), method="GET")
    try:
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []


def rest_post(path: str, body: dict) -> dict:
    """Simple REST POST against the Supabase REST API."""
    url = f"{_base_url()}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        return {"error": exc.read().decode("utf-8"), "status": exc.code}
    except Exception as exc:
        return {"error": str(exc)}


def post_event(event: dict) -> dict:
    """Post a single event to the TopRep /api/events endpoint."""
    api_url = os.getenv("TOPREP_API_URL", "")
    if not api_url:
        return {"error": "TOPREP_API_URL not configured"}
    base = api_url.rstrip("/").split("/functions/")[0].split("/rest/")[0]
    url = f"{base}/api/events"
    data = json.dumps(event).encode("utf-8")
    req = request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        return {"error": exc.read().decode("utf-8"), "status": exc.code}
    except Exception as exc:
        return {"error": str(exc)}


def get_profiles(role: str | None = None) -> list[dict]:
    params: dict[str, str] = {"select": "id,first_name,last_name,role,store_id", "order": "created_at.asc"}
    if role:
        params["role"] = f"eq.{role}"
    return rest_get("profiles", params)
