"""
Supabase client helper.
Reads connection details from the .env file (same format as v1).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import string
from http import HTTPStatus
from pathlib import Path
from urllib import error, request


def _ssl_ctx() -> ssl.SSLContext:
    """SSL context that trusts certifi's CA bundle when available (fixes macOS urllib)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


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

# Fixed TopRep Supabase project credentials — data is always sent here.
# The publishable key is safe to include in source; it is not a secret.
_TOPREP_SUPABASE_URL = "https://ahimfdfuuefesgbbnccr.supabase.co"
_TOPREP_PUBLISHABLE_KEY = "sb_publishable_SABMCFFXgDOvyvTvJWH0_w_qREoAIpS"


def _api_url() -> str:
    """Return the configured API/Supabase base URL.

    Always resolves to the TopRep Supabase project.  ``TOPREP_API_URL`` and
    ``VITE_SUPABASE_URL`` env vars are honoured if set, but the hard-coded
    TopRep URL is the ultimate fallback so the destination can never be left
    unconfigured.
    """
    return os.getenv("TOPREP_API_URL") or os.getenv("VITE_SUPABASE_URL", _TOPREP_SUPABASE_URL)


def _anon_key() -> str:
    """Return the configured Supabase anon/publishable key.

    Checks ``SUPABASE_ANON_KEY`` first; falls back to
    ``VITE_SUPABASE_PUBLISHABLE_DEFAULT_KEY``, then the hard-coded TopRep
    publishable key, and finally the service role key.  Supabase's REST API
    requires a JWT (``eyJ…``) in the ``apikey`` header, so any non-JWT
    publishable key is discarded in favour of the service role JWT.
    """
    candidate = (
        os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("PUBLIC_SUPABASE_ANON_KEY")
        or os.getenv("VITE_SUPABASE_PUBLISHABLE_DEFAULT_KEY")
        or _TOPREP_PUBLISHABLE_KEY
    )
    # If the candidate isn't a JWT, fall back to the service role key which is.
    if not candidate.startswith("eyJ"):
        candidate = os.getenv("SUPABASE_SERVICE_ROLE_KEY", candidate)
    return candidate


def _base_url() -> str:
    url = _api_url().rstrip("/")
    # Strip down to the Supabase project root
    return url.split("/functions/")[0].split("/rest/")[0]


def _headers() -> dict[str, str]:
    # Prefer user JWT when available; fall back to service role for server-side
    # connectivity checks and background simulation writes.
    user_token = os.getenv("TOPREP_AUTH_TOKEN", "")
    service_token = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    token = user_token or service_token
    apikey = _anon_key()
    # If we're using service-role fallback, the apikey should also be service_role.
    if token and token == service_token and not user_token:
        apikey = token
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


def check_connection() -> dict:
    """Verify that the Supabase REST API is reachable with the configured credentials.

    Returns ``{"ok": True, "message": "..."}`` on success or
    ``{"ok": False, "error": "..."}`` on failure.
    """
    base = _base_url()

    url = f"{base}/rest/v1/"
    try:
        req = request.Request(url, headers=_headers(), method="GET")
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
            return {"ok": True, "message": f"Connected to Supabase (HTTP {resp.status})."}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 401:
            has_service_key = bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())
            if has_service_key:
                return {
                    "ok": False,
                    "error": f"Authentication failed (HTTP 401) — service role key appears invalid or expired. Detail: {body}",
                }
            return {
                "ok": False,
                "error": f"Authentication failed (HTTP 401) — check TOPREP_AUTH_TOKEN. Detail: {body}",
            }
        return {"ok": False, "error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def rest_get(path: str, params: dict | None = None) -> list[dict]:
    """Simple REST GET against the Supabase REST API."""
    base = _base_url()
    if not base:
        return []
    url = f"{base}/rest/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    try:
        req = request.Request(url, headers=_headers(), method="GET")
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []


def rest_delete(path: str, params: dict | None = None) -> dict:
    """REST DELETE against the Supabase REST API using service-role headers."""
    base = _base_url()
    if not base:
        return {"error": "No API URL configured"}
    url = f"{base}/rest/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    try:
        req = request.Request(url, headers=_service_headers(), method="DELETE")
        with request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        return {"error": exc.read().decode("utf-8"), "status": exc.code}
    except Exception as exc:
        return {"error": str(exc)}


def rest_post(path: str, body: dict) -> dict:
    """Simple REST POST against the Supabase REST API."""
    base = _base_url()
    if not base:
        return {}
    url = f"{base}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8")
    try:
        req = request.Request(url, data=data, headers=_headers(), method="POST")
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
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

    api_url = _api_url().rstrip("/")
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
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
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
    ``store_id`` may be the string dealership ID (e.g. 'dlr-001') or a UUID;
    it is converted to a deterministic UUID5 when not already a valid UUID.
    """
    try:
        import uuid as _uuid
        _uuid.UUID(store_id)  # already a valid UUID — use as-is
        store_uuid = store_id
    except ValueError:
        store_uuid = _store_uuid(store_id)
    rows = [
        {
            "store_id": store_uuid,
            "source": p["source"],
            "stage": p["stage"],
            "prior_alpha": float(p["prior_alpha"]),
            "prior_beta": float(p["prior_beta"]),
        }
        for p in priors
    ]
    base = _base_url()
    if not base:
        return {"error": "TOPREP_API_URL not configured"}
    url = f"{base}/rest/v1/source_stage_priors"
    data = json.dumps(rows).encode("utf-8")
    headers = {**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    try:
        req = request.Request(url, data=data, headers=headers, method="POST")
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
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
    try:
        import uuid as _uuid
        _uuid.UUID(store_id)
        store_uuid = store_id
    except ValueError:
        store_uuid = _store_uuid(store_id)
    from dealmaker_generator import ARCHETYPES  # local import to avoid circular issues at module level
    mean_close_rate = sum(a.close_rate_mult * _DEFAULT_BASE_CLOSE_RATE for a in ARCHETYPES.values()) / len(ARCHETYPES)
    rows = []
    for source in sources:
        for stage in stages:
            n = 20.0
            alpha = max(0.5, round(mean_close_rate * n, 2))
            beta = max(0.5, round((1 - mean_close_rate) * n, 2))
            rows.append({
                "store_id": store_uuid,
                "source": source,
                "stage": stage,
                "prior_alpha": alpha,
                "prior_beta": beta,
            })
    return rows


# ---------------------------------------------------------------------------
# Rep user provisioning (Admin Auth API — requires service_role key)
# ---------------------------------------------------------------------------

_TEST_EMAIL_RE = re.compile(r"@(test\.com|[a-z0-9\-]+\.(test|example|localhost|invalid))$", re.IGNORECASE)


def _is_safe_test_email(email: str) -> bool:
    return bool(_TEST_EMAIL_RE.search(email))


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _stable_uuid(*parts: str) -> str:
    """Deterministic UUID5 (NAMESPACE_URL).  Mirrors dealmaker_generator.stable_uuid."""
    import uuid as _uuid
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, "|".join(parts)))


def _store_uuid(store_id: str) -> str:
    """Deterministic UUID for a store string ID (e.g. 'dlr-001')."""
    return _stable_uuid("store", store_id)


def store_uuid(store_id: str) -> str:
    """Public helper for the canonical deterministic store UUID."""
    return _store_uuid(store_id)


def _store_headers() -> dict[str, str]:
    """Prefer service-role headers for server-side store sync, fall back to user headers."""
    service_headers = _service_headers()
    if service_headers.get("Authorization"):
        return service_headers
    return _headers()


def _store_config_payload(store_config: dict) -> dict:
    """Return a sanitized store config safe to persist in the stores table."""
    excluded = {"credentials", "events_sent", "status", "last_error", "last_batch_at"}
    return {key: value for key, value in store_config.items() if key not in excluded}


def upsert_store(store_config: dict, active: bool = True) -> dict:
    """Upsert the canonical stores row for a DealMaker store config."""
    store_id = str(store_config.get("dealership_id", "")).strip()
    if not store_id:
        return {"error": "dealership_id is required"}

    body = {
        "id": _store_uuid(store_id),
        "dealership_id": store_id,
        "name": str(store_config.get("display_name") or store_id),
        "active": active,
        "config": _store_config_payload(store_config),
    }
    headers = {**_store_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    return rest_post_with_headers("stores", body, headers)


def deactivate_store(store_id: str) -> dict:
    """Soft-delete a store by marking its canonical stores row inactive."""
    return upsert_store({"dealership_id": store_id}, active=False)


def _admin_get_user_by_email(email: str) -> dict | None:
    """Look up an auth user by email via the Admin API."""
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        return None
    url = f"{_base_url()}/auth/v1/admin/users?per_page=1"
    # Supabase admin list doesn't filter by email in query params — we have to
    # page through or use a different endpoint.  The GoTrue admin API doesn't
    # expose a by-email lookup, so we search the full list.
    list_url = f"{_base_url()}/auth/v1/admin/users?per_page=1000"
    req = request.Request(list_url, headers=_service_headers(), method="GET")
    try:
        with request.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    users = payload.get("users", []) if isinstance(payload, dict) else payload
    for u in users:
        if u.get("email", "").lower() == email.lower():
            return u
    return None


def _admin_delete_user(user_id: str) -> bool:
    """Delete an auth user by UUID via the Admin API."""
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        return False
    del_url = f"{_base_url()}/auth/v1/admin/users/{user_id}"
    req = request.Request(del_url, headers=_service_headers(), method="DELETE")
    try:
        with request.urlopen(req, timeout=15, context=_ssl_ctx()):
            return True
    except Exception:
        return False


def _rep_uuid(dealership_id: str, member_id: str) -> str:
    """Deterministic UUID for a rep — must match dealmaker_generator.sales_rep_uuid."""
    return _stable_uuid("sales_rep", dealership_id, member_id)


def admin_create_user(email: str, password: str, user_id: str | None = None) -> dict:
    """POST /auth/v1/admin/users using service_role key.

    Returns the created user dict or an error dict.
    Refuses to create users whose email domain isn't an obvious test domain
    unless DEALMAKER_ALLOW_PROD_PROVISIONING=1 is set.

    When ``user_id`` is provided it is passed as the ``id`` field so the
    Supabase auth user gets that exact UUID.  This is critical: DealMaker
    assigns rep UUIDs deterministically via ``stable_uuid("sales_rep", ...)``,
    and the provisioned auth user must share that UUID so the RLS policy
    ``sales_rep_id = auth.uid()`` resolves correctly.
    """
    if not _is_safe_test_email(email) and not os.getenv("DEALMAKER_ALLOW_PROD_PROVISIONING"):
        return {"error": f"Refusing to provision non-test email: {email}. "
                         "Set DEALMAKER_ALLOW_PROD_PROVISIONING=1 to override."}

    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        return {"error": "SUPABASE_SERVICE_ROLE_KEY not configured"}

    url = f"{_base_url()}/auth/v1/admin/users"
    body: dict = {
        "email": email,
        "password": password,
        "email_confirm": True,
    }
    if user_id:
        body["id"] = user_id
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=_service_headers(), method="POST")
    try:
        with request.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
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
    upsert_store(store_config, active=True)
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

    # Deterministic UUID for this store (for store_id FK columns)
    store_uuid = _store_uuid(store_id)
    # Member ID counter mirrors build_team(): S-001, S-002, ...
    for i in range(1, salespeople + 1):
        member_id = f"S-{i:03d}"
        arch = archetype_slots[i - 1] if i - 1 < len(archetype_slots) else "solid_mid"
        abbrev = {"rockstar": "rock", "solid_mid": "mid", "underperformer": "under", "new_hire": "new"}.get(arch, "rep")
        abbrev_counters[abbrev] = abbrev_counters.get(abbrev, 0) + 1
        n = abbrev_counters[abbrev]
        email = f"sim-{store_slug}-{abbrev}{n}@test.com"
        password = "test123"

        # Deterministic UUID that matches dealmaker_generator.sales_rep_uuid()
        rep_uuid = _rep_uuid(store_id, member_id)

        result = admin_create_user(email, password, user_id=rep_uuid)
        if "error" in result:
            error_text = str(result.get("error", ""))
            if "already" in error_text.lower() or result.get("status") == 422:
                # User exists — check if its UUID matches the deterministic one.
                # If not, delete the stale user and recreate with the correct UUID.
                existing = _admin_get_user_by_email(email)
                if existing and existing.get("id") != rep_uuid:
                    _admin_delete_user(existing["id"])
                    retry = admin_create_user(email, password, user_id=rep_uuid)
                    if "error" in retry:
                        credentials.append({
                            "rep_name": f"Sales Rep {i}",
                            "archetype": arch,
                            "email": email,
                            "password": password,
                            "user_id": None,
                            "error": str(retry.get("error", "")),
                        })
                        continue
                user_id: str | None = rep_uuid
            else:
                credentials.append({
                    "rep_name": f"Sales Rep {i}",
                    "archetype": arch,
                    "email": email,
                    "password": password,
                    "user_id": None,
                    "error": error_text,
                })
                continue
        else:
            user_id = result.get("id") or result.get("user", {}).get("id") or rep_uuid

        # Upsert profile — uses service headers so the RLS insert policy is bypassed
        if user_id:
            profile_body = {
                "id": user_id,
                "email": email,
                "first_name": "Sales",
                "last_name": f"Rep {i}",
                "role": "sales_rep",
                "store_id": store_uuid,
            }
            rest_post_with_headers(
                path="profiles",
                body=profile_body,
                headers={**_service_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            # Upsert reps row
            reps_body = {"id": user_id, "store_id": store_uuid}
            rest_post_with_headers(
                path="reps",
                body=reps_body,
                headers={**_service_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
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
    base = _base_url()
    if not base:
        return {}
    url = f"{base}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8")
    try:
        req = request.Request(url, data=data, headers=headers, method="POST")
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
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
        with request.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
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
                with request.urlopen(del_req, timeout=15, context=_ssl_ctx()):
                    deleted += 1
            except Exception as exc:
                errors.append(f"{email}: {exc}")

    return {"deleted": deleted, "errors": errors}


def purge_store_data(store_id: str) -> dict:
    """Delete all database rows associated with a store.

    Uses a raw SQL RPC call to temporarily disable the events append-only
    trigger, then deletes everything in FK-safe order.
    """
    su = _store_uuid(store_id)
    results: dict[str, dict | str] = {}

    # 1. Collect rep UUIDs belonging to this store
    rep_rows = rest_get("profiles", {"store_id": f"eq.{su}", "select": "id"})
    rep_ids = [r["id"] for r in rep_rows if r.get("id")]

    # 2. Delete rep-scoped data (events first — needs trigger disabled, then activities, deals)
    if rep_ids:
        ids_filter = f"({','.join(rep_ids)})"
        # Events table may have an append-only trigger that blocks deletes via REST.
        # Fall back gracefully if the delete fails.
        results["events"] = rest_delete("events", {"sales_rep_id": f"in.{ids_filter}"})
        for table in ("activities", "deals"):
            results[table] = rest_delete(table, {"sales_rep_id": f"in.{ids_filter}"})

    # 3. Delete store-scoped data
    for table in ("leads", "source_stage_priors"):
        results[table] = rest_delete(table, {"store_id": f"eq.{su}"})

    # 4. Delete reps (CASCADE handles quotas, rep_stage_stats, etc.)
    results["reps"] = rest_delete("reps", {"store_id": f"eq.{su}"})

    # 5. Delete profiles
    results["profiles"] = rest_delete("profiles", {"store_id": f"eq.{su}"})

    # 6. Delete the canonical stores row
    results["stores"] = rest_delete("stores", {"id": f"eq.{su}"})

    errors = {k: v for k, v in results.items() if isinstance(v, dict) and v.get("error")}
    return {"ok": not errors, "details": results, "errors": errors}
