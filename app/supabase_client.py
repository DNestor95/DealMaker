"""
Supabase client helper.
Reads connection details from the .env file (same format as v1).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import string
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

# Default base close rate — matches generate_events() default; used for prior auto-calculation
_DEFAULT_BASE_CLOSE_RATE = 0.36


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


def _service_headers() -> dict[str, str]:
    """Headers using the service_role key for Admin Auth API calls."""
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    h: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if service_key:
        h["Authorization"] = f"Bearer {service_key}"
        h["apikey"] = service_key
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
    """Post a single event to the TopRep /api/events endpoint.

    Uses the configured TOPREP_API_URL directly. For edge function URLs
    (``/functions/v1/``), this posts to the function URL as-is.  For all
    other Supabase URLs the event is routed to ``{supabase_root}/api/events``.

    Note: bulk delivery (simulation loop, backfill) should use
    ``dealmaker_generator.send_events_to_api`` which handles edge-function
    batch payloads correctly.
    """
    from urllib.parse import urlparse

    api_url = os.getenv("TOPREP_API_URL", "").rstrip("/")
    if not api_url:
        return {"error": "TOPREP_API_URL not configured"}

    parsed = urlparse(api_url)
    path = parsed.path

    # Use the URL as-is when it already targets a specific endpoint path.
    # Route bare Supabase project URLs to /api/events.
    if (
        path.startswith("/functions/v1/")
        or path.startswith("/api/events")
        or path.startswith("/rest/v1/")
    ):
        url = api_url
    else:
        # Bare Supabase project URL — append /api/events
        base = f"{parsed.scheme}://{parsed.netloc}"
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


# ---------------------------------------------------------------------------
# Bayesian prior seeding
# ---------------------------------------------------------------------------

def seed_source_stage_priors(
    store_id: str,
    priors: list[dict],
) -> dict:
    """Upsert source_stage_priors rows for a store.

    Each entry in ``priors`` must have: source, stage, prior_alpha, prior_beta.
    """
    rows = [
        {
            "store_id": store_id,
            "source": p["source"],
            "stage": p["stage"],
            "prior_alpha": float(p["prior_alpha"]),
            "prior_beta": float(p["prior_beta"]),
        }
        for p in priors
    ]
    url = f"{_base_url()}/rest/v1/source_stage_priors"
    data = json.dumps(rows).encode("utf-8")
    headers = {**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return {"ok": True, "rows": len(rows), "response": raw}
    except error.HTTPError as exc:
        return {"error": exc.read().decode("utf-8"), "status": exc.code}
    except Exception as exc:
        return {"error": str(exc)}


def priors_from_archetypes(store_id: str, sources: list[str], stages: list[str]) -> list[dict]:
    """Auto-calculate source_stage_priors from archetype close rates.

    Uses Beta distribution parameters:
      - alpha proportional to archetype close rate (observations that succeeded)
      - beta = N - alpha  (observations that didn't)
    N is set to 20 as a weak-prior sample size.
    """
    from dealmaker_generator import ARCHETYPES  # local import to avoid circular issues at module level
    mean_close_rate = sum(a.close_rate_mult * _DEFAULT_BASE_CLOSE_RATE for a in ARCHETYPES.values()) / len(ARCHETYPES)
    rows = []
    for source in sources:
        for stage in stages:
            n = 20.0
            alpha = max(0.5, round(mean_close_rate * n, 2))
            beta = max(0.5, round((1 - mean_close_rate) * n, 2))
            rows.append({
                "store_id": store_id,
                "source": source,
                "stage": stage,
                "prior_alpha": alpha,
                "prior_beta": beta,
            })
    return rows


# ---------------------------------------------------------------------------
# Rep user provisioning (Admin Auth API — requires service_role key)
# ---------------------------------------------------------------------------

_TEST_EMAIL_RE = re.compile(r"@[a-z0-9\-]+\.(test|example|localhost|invalid)$", re.IGNORECASE)


def _is_safe_test_email(email: str) -> bool:
    return bool(_TEST_EMAIL_RE.search(email))


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def admin_create_user(email: str, password: str) -> dict:
    """POST /auth/v1/admin/users using service_role key.

    Returns the created user dict or an error dict.
    Refuses to create users whose email domain isn't an obvious test domain
    unless DEALMAKER_ALLOW_PROD_PROVISIONING=1 is set.
    """
    if not _is_safe_test_email(email) and not os.getenv("DEALMAKER_ALLOW_PROD_PROVISIONING"):
        return {"error": f"Refusing to provision non-test email: {email}. "
                         "Set DEALMAKER_ALLOW_PROD_PROVISIONING=1 to override."}

    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        return {"error": "SUPABASE_SERVICE_ROLE_KEY not configured"}

    url = f"{_base_url()}/auth/v1/admin/users"
    body = {
        "email": email,
        "password": password,
        "email_confirm": True,
    }
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=_service_headers(), method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        return {"error": exc.read().decode("utf-8"), "status": exc.code}
    except Exception as exc:
        return {"error": str(exc)}


def provision_store_reps(store_config: dict) -> list[dict]:
    """Create auth users + upsert profiles + reps rows for all sales reps in a store.

    Returns a list of credential dicts: {rep_name, archetype, email, password, user_id}.
    """
    store_id = store_config.get("dealership_id", "unknown")
    # Build a slug: lowercase, replace non-alphanum with hyphen
    store_slug = re.sub(r"[^a-z0-9]+", "-", store_id.lower()).strip("-")
    archetype_dist: dict[str, int] = store_config.get("archetype_dist", {})
    salespeople = store_config.get("salespeople", 0)

    # Build ordered archetype list (same as build_team)
    archetype_slots: list[str] = []
    for arch_key in ["rockstar", "solid_mid", "underperformer", "new_hire"]:
        archetype_slots.extend([arch_key] * archetype_dist.get(arch_key, 0))

    credentials: list[dict] = []
    abbrev_counters: dict[str, int] = {}

    for i in range(1, salespeople + 1):
        arch = archetype_slots[i - 1] if i - 1 < len(archetype_slots) else "solid_mid"
        abbrev = {"rockstar": "rock", "solid_mid": "mid", "underperformer": "under", "new_hire": "new"}.get(arch, "rep")
        abbrev_counters[abbrev] = abbrev_counters.get(abbrev, 0) + 1
        n = abbrev_counters[abbrev]
        email = f"sim-{store_slug}-{abbrev}{n}@dealmaker.test"
        password = _generate_password()

        result = admin_create_user(email, password)
        if "error" in result:
            credentials.append({
                "rep_name": f"Sales Rep {i}",
                "archetype": arch,
                "email": email,
                "password": password,
                "user_id": None,
                "error": result["error"],
            })
            continue

        user_id = result.get("id") or result.get("user", {}).get("id")

        # Upsert profile
        if user_id:
            profile_body = {
                "id": user_id,
                "first_name": f"Sales",
                "last_name": f"Rep {i}",
                "role": "sales_rep",
                "store_id": store_id,
            }
            rest_post_with_headers(
                path="profiles",
                body=profile_body,
                headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            # Upsert reps row
            reps_body = {"id": user_id, "store_id": store_id}
            rest_post_with_headers(
                path="reps",
                body=reps_body,
                headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            )

        credentials.append({
            "rep_name": f"Sales Rep {i}",
            "archetype": arch,
            "email": email,
            "password": password,
            "user_id": user_id,
        })

    return credentials


def rest_post_with_headers(path: str, body: dict, headers: dict) -> dict:
    """REST POST with custom headers (internal helper)."""
    url = f"{_base_url()}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        return {"error": exc.read().decode("utf-8"), "status": exc.code}
    except Exception as exc:
        return {"error": str(exc)}


def deprovision_store_reps(store_id: str) -> dict:
    """Delete auth users whose email matches the sim-{store_slug}-* pattern."""
    store_slug = re.sub(r"[^a-z0-9]+", "-", store_id.lower()).strip("-")
    pattern = f"sim-{store_slug}-"

    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        return {"error": "SUPABASE_SERVICE_ROLE_KEY not configured"}

    # List all admin users
    list_url = f"{_base_url()}/auth/v1/admin/users?per_page=1000"
    req = request.Request(list_url, headers=_service_headers(), method="GET")
    try:
        with request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except Exception as exc:
        return {"error": str(exc)}

    users = payload.get("users", []) if isinstance(payload, dict) else payload
    deleted = 0
    errors: list[str] = []

    for user in users:
        email = user.get("email", "")
        if email.startswith(pattern):
            uid = user.get("id")
            if not uid:
                continue
            del_url = f"{_base_url()}/auth/v1/admin/users/{uid}"
            del_req = request.Request(del_url, headers=_service_headers(), method="DELETE")
            try:
                with request.urlopen(del_req, timeout=15):
                    deleted += 1
            except Exception as exc:
                errors.append(f"{email}: {exc}")

    return {"deleted": deleted, "errors": errors}
