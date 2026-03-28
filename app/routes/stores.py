"""
Store / Dealership routes.

GET  /              → list all configured stores
GET  /stores/new    → new store form
POST /stores/new    → create store + seed initial data into TopRep DB
GET  /stores/<id>   → store detail / live stats
GET  /stores/<id>/edit   → edit store form (pre-populated)
POST /stores/<id>/edit   → save edits in-place
POST /stores/<id>/delete      → remove store from session + persistence
POST /stores/<id>/backfill    → generate historical data for a date range
GET  /stores/<id>/sync-info   → JSON: rep UUIDs + sim params for TopRep alignment
POST /stores/<id>/provision   → provision QA auth users for all reps
POST /stores/<id>/deprovision → delete provisioned auth users
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from dealmaker_postgres import database_url_from_env, is_postgres_dsn
from app.supabase_client import (
    deprovision_store_reps,
    get_profiles,
    priors_from_archetypes,
    provision_store_reps,
    rest_get,
    seed_source_stage_priors,
    _api_url as _supabase_api_url,
    _anon_key as _supabase_anon_key,
)

# Absolute path to the project root so output/ is always found regardless of CWD.
_APP_ROOT = Path(__file__).parent.parent.parent

# Make the project root importable so dealmaker_generator can be found.
sys.path.insert(0, str(_APP_ROOT))
from dealmaker_generator import (  # noqa: E402
    ARCHETYPES,
    SCENARIO_REGISTRY,
    build_team,
    generate_events,
    normalize_delivery_url,
    send_events_to_api,
)

bp = Blueprint("stores", __name__)

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _resolve_output_dir() -> Path:
    """Return a writable output directory.

    Vercel (and other serverless runtimes) mount the deployment bundle on a
    read-only filesystem.  ``/tmp`` is always writable, so we fall back to
    ``/tmp/dealmaker_output`` when the preferred ``output/`` directory cannot
    be created.
    """
    preferred = _APP_ROOT / "output"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path("/tmp") / "dealmaker_output"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


_OUTPUT_DIR = _resolve_output_dir()
_STORES_FILE = _OUTPUT_DIR / "stores_config.json"


def _load_stores() -> dict[str, dict]:
    if _STORES_FILE.exists():
        try:
            data = json.loads(_STORES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_stores(stores: dict[str, dict]) -> None:
    _STORES_FILE.write_text(json.dumps(stores, indent=2), encoding="utf-8")


def _parse_hire_dates(store: dict) -> list[date | None]:
    """Convert stored new_hire_dates (list of ISO strings | None) to date objects."""
    result: list[date | None] = []
    for raw in store.get("new_hire_dates", []):
        if raw:
            try:
                result.append(date.fromisoformat(str(raw)))
            except (ValueError, TypeError):
                result.append(None)
        else:
            result.append(None)
    return result


def _parse_store_form(data, existing: dict | None = None) -> dict:
    """Parse a store create/edit form submission into a store config dict.

    ``existing`` is the current stored store dict (for edit); it provides
    the dealership_id (which can't be renamed) and preserves runtime fields.
    """
    store_id = (existing["dealership_id"] if existing else data.get("dealership_id", "").strip())

    archetype_dist = {
        "rockstar": int(data.get("arch_rockstar", 1)),
        "solid_mid": int(data.get("arch_solid_mid", 5)),
        "underperformer": int(data.get("arch_underperformer", 1)),
        "new_hire": int(data.get("arch_new_hire", 1)),
    }

    # Collect one hire date per new hire slot (validate format)
    new_hire_count = archetype_dist["new_hire"]
    new_hire_dates: list[str | None] = []
    for n in range(1, new_hire_count + 1):
        raw_date = data.get(f"new_hire_date_{n}", "").strip()
        if raw_date:
            try:
                date.fromisoformat(raw_date)  # validate — raises ValueError if invalid
                new_hire_dates.append(raw_date)
            except ValueError:
                new_hire_dates.append(None)   # silently drop invalid date; form never submits invalid via <input type=date>
        else:
            new_hire_dates.append(None)

    # Time-acceleration: validate custom multiplier
    raw_mult = data.get("sim_speed_multiplier", "").strip()
    try:
        sim_speed_multiplier = max(1.0, float(raw_mult)) if raw_mult else 1.0
    except ValueError:
        sim_speed_multiplier = 1.0

    raw_days_total = data.get("sim_days_total", "").strip()
    try:
        sim_days_total = max(0, int(raw_days_total)) if raw_days_total else 0
    except ValueError:
        sim_days_total = 0

    # Validate sim_start_date format
    raw_sim_start = data.get("sim_start_date", "").strip()
    if raw_sim_start:
        try:
            date.fromisoformat(raw_sim_start)
        except ValueError:
            raw_sim_start = ""

    return {
        "dealership_id": store_id,
        "salespeople": int(data.get("salespeople", 8)),
        "managers": int(data.get("managers", 2)),
        "bdc_agents": int(data.get("bdc_agents", 3)),
        "daily_leads": int(data.get("daily_leads", 20)),
        "lead_sources": data.getlist("lead_sources") or ["internet", "phone", "showroom"],
        "deal_statuses": data.getlist("deal_statuses") or ["lead", "qualified", "closed_won", "closed_lost"],
        "activity_types": data.getlist("activity_types") or ["call", "email", "meeting"],
        "activity_outcomes": data.getlist("activity_outcomes") or ["connected", "appt_set", "showed", "sold"],
        "deal_amount_min": int(data.get("deal_amount_min", 12000)),
        "deal_amount_max": int(data.get("deal_amount_max", 68000)),
        "gross_profit_min": int(data.get("gross_profit_min", 700)),
        "gross_profit_max": int(data.get("gross_profit_max", 6000)),
        "close_rate_pct": int(data.get("close_rate_pct", 36)),
        "status_advance_pct": int(data.get("status_advance_pct", 88)),
        "contact_rate_pct": int(data.get("contact_rate_pct", 72)),
        "appointment_rate_pct": int(data.get("appointment_rate_pct", 55)),
        "showroom_rate_pct": int(data.get("showroom_rate_pct", 65)),
        "negotiation_rate_pct": int(data.get("negotiation_rate_pct", 80)),
        "activities_per_deal_min": int(data.get("activities_per_deal_min", 2)),
        "activities_per_deal_max": int(data.get("activities_per_deal_max", 6)),
        "archetype_dist": archetype_dist,
        "new_hire_dates": new_hire_dates,
        "month_shape": data.get("month_shape", "flat"),
        "default_scenarios": data.getlist("default_scenarios"),
        "delivery": data.get("delivery", "file"),
        "batch_days": int(data.get("batch_days", 1)),
        "every_seconds": int(data.get("every_seconds", 10)),
        "seed": int(data.get("seed", 42)),
        # Time-acceleration fields
        "sim_speed_preset": data.get("sim_speed_preset", "realtime"),
        "sim_speed_multiplier": sim_speed_multiplier,
        "sim_days_total": sim_days_total,
        "sim_start_date": raw_sim_start,
        # Preserve runtime-only fields
        "status": (existing or {}).get("status", "stopped"),
        "events_sent": (existing or {}).get("events_sent", 0),
        "credentials": (existing or {}).get("credentials", []),
    }


# ---------------------------------------------------------------------------
# In-memory store registry (backed by JSON file)
# ---------------------------------------------------------------------------
_stores: dict[str, dict] = _load_stores()

# Reset runtime-only fields on startup
for _s in _stores.values():
    _s["status"] = "stopped"
    _s.setdefault("events_sent", 0)
    _s.setdefault("credentials", [])
    _s.setdefault("new_hire_dates", [])
    # Time-acceleration defaults for stores created before this feature
    _s.setdefault("sim_speed_preset", "realtime")
    _s.setdefault("sim_speed_multiplier", 1.0)
    _s.setdefault("sim_days_total", 0)
    _s.setdefault("sim_start_date", "")
    _s.setdefault("contact_rate_pct", 72)
    _s.setdefault("appointment_rate_pct", 55)
    _s.setdefault("showroom_rate_pct", 65)
    _s.setdefault("negotiation_rate_pct", 80)


STORE_TEMPLATES = {
    "custom": {"label": "Custom (blank)", "salespeople": 8, "managers": 2, "bdc_agents": 3,
               "daily_leads": 20, "close_rate_pct": 36, "month_shape": "flat",
               "archetype_dist": {"rockstar": 1, "solid_mid": 5, "underperformer": 1, "new_hire": 1}},
    "high_volume_internet": {"label": "High-Volume Internet Store", "salespeople": 12, "managers": 3,
                             "bdc_agents": 5, "daily_leads": 40, "close_rate_pct": 30, "month_shape": "realistic",
                             "archetype_dist": {"rockstar": 2, "solid_mid": 7, "underperformer": 2, "new_hire": 1}},
    "rural_walkin": {"label": "Rural Walk-In Store", "salespeople": 4, "managers": 1, "bdc_agents": 1,
                     "daily_leads": 8, "close_rate_pct": 45, "month_shape": "realistic",
                     "archetype_dist": {"rockstar": 1, "solid_mid": 2, "underperformer": 1, "new_hire": 0}},
    "bdc_heavy_phone": {"label": "BDC-Heavy Phone Store", "salespeople": 6, "managers": 2, "bdc_agents": 8,
                        "daily_leads": 25, "close_rate_pct": 33, "month_shape": "realistic",
                        "archetype_dist": {"rockstar": 1, "solid_mid": 4, "underperformer": 1, "new_hire": 0}},
}

# Speed presets shared between stores (form) and simulation (thread logic).
# multiplier = simulated seconds per real second.
SPEED_PRESETS: dict[str, dict] = {
    "realtime":         {"label": "Realtime (1×)",               "multiplier": 1.0},
    "1day_per_minute":  {"label": "1 day per minute (1,440×)",   "multiplier": 1440.0},
    "1week_per_hour":   {"label": "1 week per hour (168×)",      "multiplier": 168.0},
    "1month_per_hour":  {"label": "1 month per hour (720×)",     "multiplier": 720.0},
    "1month_per_10min": {"label": "1 month per 10 min (4,320×)", "multiplier": 4320.0},
    "custom":           {"label": "Custom multiplier",           "multiplier": None},
}

_FORM_CONTEXT = dict(
    lead_sources=["internet", "phone", "showroom", "referral", "service", "walkin"],
    deal_statuses=["lead", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"],
    activity_types=["call", "email", "meeting", "demo", "note"],
    activity_outcomes=[
        "connected", "no_answer", "left_vm", "appt_set",
        "showed", "no_show", "sold", "lost", "negotiating", "follow_up",
    ],
    rep_roles=["sales_rep", "manager", "bdc"],
    store_templates=STORE_TEMPLATES,
    scenario_keys=["slow_industry_month", "manager_on_vacation", "bdc_underperforming",
                   "inventory_shortage", "strong_incentive_month", "high_heat_weekend"],
    month_shapes=["flat", "realistic", "front_loaded"],
    speed_presets=SPEED_PRESETS,
)


@bp.route("/")
def index():
    return render_template("stores/list.html", stores=list(_stores.values()))


@bp.route("/stores/new", methods=["GET"])
def new_store():
    return render_template("stores/new.html", **_FORM_CONTEXT)


@bp.route("/stores/new", methods=["POST"])
def create_store():
    data = request.form

    store_id = data.get("dealership_id", "").strip()
    if not store_id:
        return render_template("stores/new.html", error="Dealership ID is required.", **_FORM_CONTEXT)

    store = _parse_store_form(data)
    _stores[store_id] = store
    _save_stores(_stores)

    # Auto-seed Bayesian priors if connected
    if os.getenv("TOPREP_API_URL"):
        prior_rows = priors_from_archetypes(
            store_id=store_id,
            sources=store["lead_sources"],
            stages=[s for s in store["deal_statuses"] if s not in ("closed_won", "closed_lost")],
        )
        seed_source_stage_priors(store_id, prior_rows)

    # Auto-provision QA auth users when service key is available
    anchor = ""
    if os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        credentials = provision_store_reps(store)
        store["credentials"] = credentials
        _save_stores(_stores)
        success_count = sum(1 for c in credentials if not c.get("error"))
        anchor = "#provisioning"
        if success_count:
            flash(
                f"✓ Store '{store_id}' created and {success_count} rep login(s) provisioned. "
                "Scroll down to see credentials.",
                "success",
            )
        else:
            flash(
                f"Store '{store_id}' created. Rep provisioning ran but encountered errors — check credentials below.",
                "warning",
            )
    else:
        flash(
            f"Store '{store_id}' created. Add SUPABASE_SERVICE_ROLE_KEY in Settings to provision rep logins.",
            "info",
        )

    return redirect(url_for("stores.store_detail", store_id=store_id) + anchor)


@bp.route("/stores/<store_id>/edit", methods=["GET"])
def edit_store(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return render_template("404.html"), 404
    return render_template("stores/edit.html", store=store, **_FORM_CONTEXT)


@bp.route("/stores/<store_id>/edit", methods=["POST"])
def update_store(store_id: str):
    existing = _stores.get(store_id)
    if not existing:
        return render_template("404.html"), 404

    # Stop a running simulation before mutating store config
    from app.routes.simulation import _runners
    thread = _runners.get(store_id)
    if thread and thread.is_alive():
        thread.stop()
        thread.join(timeout=5)
        if thread.is_alive():
            # Thread didn't exit cleanly; mark stopped anyway and let OS clean up at next GC
            import logging
            logging.getLogger(__name__).warning(
                "Simulation thread for %s did not stop within 5s during edit; proceeding.", store_id
            )
        existing["status"] = "stopped"

    updated = _parse_store_form(request.form, existing=existing)
    # Preserve the dealership_id (can't be renamed via edit)
    updated["dealership_id"] = store_id
    _stores[store_id] = updated
    _save_stores(_stores)

    flash(f"Store '{store_id}' updated.", "success")
    return redirect(url_for("stores.store_detail", store_id=store_id))


@bp.route("/stores/<store_id>")
def store_detail(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return render_template("404.html"), 404

    # Try to pull live rep profiles tied to this store from TopRep
    profiles = get_profiles()
    store_profiles = [p for p in profiles if p.get("store_id") == store_id]

    service_key_configured = bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    toprep_url_configured = bool(os.getenv("TOPREP_APP_URL", ""))
    toprep_app_url = os.getenv("TOPREP_APP_URL", "").rstrip("/")

    return render_template(
        "stores/detail.html",
        store=store,
        profiles=store_profiles,
        archetypes=ARCHETYPES,
        scenario_registry=SCENARIO_REGISTRY,
        service_key_configured=service_key_configured,
        toprep_url_configured=toprep_url_configured,
        toprep_app_url=toprep_app_url,
        speed_presets=SPEED_PRESETS,
    )


@bp.route("/stores/<store_id>/delete", methods=["POST"])
def delete_store(store_id: str):
    _stores.pop(store_id, None)
    _save_stores(_stores)
    flash(f"Store '{store_id}' deleted.", "info")
    return redirect(url_for("stores.index"))


@bp.route("/stores/<store_id>/sync-info")
def sync_info(store_id: str):
    """Return the deterministic rep UUIDs and simulation parameters needed
    to align TopRep with the data DealMaker will generate.

    GET  /stores/<store_id>/sync-info        → JSON
    GET  /stores/<store_id>/sync-info?fmt=html  → redirect to detail page panel (anchor)
    """
    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404

    from dealmaker_generator import build_team, sales_rep_uuid

    team = build_team(
        salespeople=store["salespeople"],
        managers=store["managers"],
        bdc_agents=store["bdc_agents"],
        archetype_dist=store.get("archetype_dist"),
        new_hire_dates=_parse_hire_dates(store),
    )

    reps = []
    for member in team:
        rep_uuid = sales_rep_uuid(store_id, member)
        reps.append({
            "name": member.name,
            "role": member.role,
            "archetype": member.archetype,
            "member_id": member.member_id,
            "sales_rep_id": rep_uuid,
        })

    # Compute expected event volume range based on store config
    daily_leads = store.get("daily_leads", 20)
    batch_days = store.get("batch_days", 1)
    activities_min = store.get("activities_per_deal_min", 2)
    activities_max = store.get("activities_per_deal_max", 6)
    # events per lead: 1 deal.created + ~N status changes (3-5) + 2*activities
    events_low = daily_leads * batch_days * (1 + 2 + 2 * activities_min)
    events_high = daily_leads * batch_days * (1 + 5 + 2 * activities_max)

    sim_start = store.get("sim_start_date") or "wall clock (now)"
    sim_days = store.get("sim_days_total", 0)
    speed_label = SPEED_PRESETS.get(
        store.get("sim_speed_preset", "realtime"),
        SPEED_PRESETS["realtime"],
    )["label"]

    payload = {
        "dealership_id": store_id,
        "simulation": {
            "start_date": sim_start,
            "total_days": sim_days if sim_days else "indefinite",
            "speed_preset": store.get("sim_speed_preset", "realtime"),
            "speed_label": speed_label,
            "batch_days": batch_days,
            "daily_leads": daily_leads,
            "close_rate_pct": store.get("close_rate_pct", 36),
            "seed": store.get("seed", 42),
            "month_shape": store.get("month_shape", "flat"),
            "default_scenarios": store.get("default_scenarios", []),
            "est_events_per_batch": f"{events_low}–{events_high}",
        },
        "event_contract": {
            "types": ["deal.created", "deal.status_changed", "activity.scheduled",
                      "activity.completed", "rep_quota_updated"],
            "status_field_for_deal_status_changed": "new_status",
            "timestamps": "UTC ISO-8601 (simulated time — not wall clock)",
            "deal_id_algorithm": "uuid5(NAMESPACE_URL, 'deal|<dealership_id>|<YYYYMMDD>|<n>')",
            "rep_id_algorithm": "uuid5(NAMESPACE_URL, 'sales_rep|<dealership_id>|<member_id>')",
        },
        "reps": reps,
    }
    return jsonify(payload)


@bp.route("/stores/<store_id>/backfill", methods=["POST"])
def backfill_store(store_id: str):
    """Generate historical events for a date range and write to file / push to API."""
    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404

    start_str = request.form.get("start_date", "")
    end_str = request.form.get("end_date", "")
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    days = max(1, (end_dt - start_dt).days + 1)

    team = build_team(
        salespeople=store["salespeople"],
        managers=store["managers"],
        bdc_agents=store["bdc_agents"],
        archetype_dist=store.get("archetype_dist"),
        new_hire_dates=_parse_hire_dates(store),
    )

    scenarios = request.form.getlist("scenarios") or store.get("default_scenarios", [])
    month_shape = request.form.get("month_shape", store.get("month_shape", "flat"))
    delivery = request.form.get("delivery", store.get("delivery", "file"))

    events = generate_events(
        start_date=start_dt,
        days=days,
        daily_leads=store["daily_leads"],
        team=team,
        dealership_id=store_id,
        seed=store["seed"],
        base_close_rate=store["close_rate_pct"] / 100.0,
        deal_amount_min=store["deal_amount_min"],
        deal_amount_max=store["deal_amount_max"],
        gross_profit_min=store["gross_profit_min"],
        gross_profit_max=store["gross_profit_max"],
        activities_min=store["activities_per_deal_min"],
        activities_max=store["activities_per_deal_max"],
        month_shape=month_shape,
        scenarios=scenarios,
    )

    output_dir = _APP_ROOT / "output" / "stores"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{store_id}_backfill_{start_str}_{end_str}.jsonl"
    with out_file.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev.to_dict(), separators=(",", ":")) + "\n")

    errors_count = 0
    if delivery in {"api", "both"}:
        raw_url = os.getenv("TOPREP_API_URL", "").strip() or database_url_from_env().strip() or _supabase_api_url()
        api_url = normalize_delivery_url(raw_url)
        auth_token = os.getenv("TOPREP_AUTH_TOKEN", "")
        supabase_apikey = os.getenv("SUPABASE_ANON_KEY", "") or _supabase_anon_key()
        if not is_postgres_dsn(api_url) and not auth_token.strip():
            return jsonify({
                "error": "Authentication failed (HTTP 401) — check TOPREP_AUTH_TOKEN.",
                "hint": "Set TOPREP_AUTH_TOKEN in Settings before running an API-delivery backfill.",
            }), 401
        if api_url:
            result = send_events_to_api(events, api_url, auth_token, supabase_apikey)
            errors_count = result["failed"]
        else:
            errors_count = len(events)

    return jsonify({
        "events": len(events),
        "days": days,
        "file": str(out_file),
        "api_errors": errors_count if delivery in {"api", "both"} else None,
    })


@bp.route("/stores/<store_id>/provision", methods=["POST"])
def provision_reps(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404

    if not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY not configured in Settings"}), 400

    credentials = provision_store_reps(store)
    store["credentials"] = credentials
    _save_stores(_stores)
    return jsonify({"credentials": credentials})


@bp.route("/stores/<store_id>/deprovision", methods=["POST"])
def deprovision_reps(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404

    if not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY not configured in Settings"}), 400

    result = deprovision_store_reps(store_id)
    store["credentials"] = []
    _save_stores(_stores)
    return jsonify(result)
