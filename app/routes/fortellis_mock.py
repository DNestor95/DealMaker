"""
Mock Fortellis / CDK Elead API server.

Mounts Elead-compatible endpoints so TopRep's real Fortellis HTTP client
(lib/ingestion/providers/fortellis/client.ts) can be tested end-to-end
without touching the real Fortellis API.

Setup:
  1. Create a store in DealMaker (e.g. dealership_id = "test-store")
  2. In TopRep's integration_configs, set:
       subscription_id = "test-store"   (matches the dealership_id here)
       base_url        = "http://localhost:5050"
  3. Run TopRep's sync — it will call this server instead of api.fortellis.io

Endpoints mirrored:
  POST /oauth2/aus1p1ixy7YL8cMq02p7/v1/token         → fake bearer token
  GET  /sales/v1/elead/activities/activityTypes        → activity type catalog
  GET  /sales/v2/elead/opportunities/search            → leads / deals
  GET  /sales/v1/elead/activities/history/byOpportunityId/<id>  → activities
  GET  /sales/v1/elead/reference/employees             → rep list

The Subscription-Id request header maps to the DealMaker dealership_id.
salesPersonId in responses is the member_id (e.g. "S-001") — set
reps.employee_external_id to these values in TopRep to resolve rep links.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

# Make the project root importable so dealmaker_generator can be found
_APP_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_APP_ROOT))

from dealmaker_generator import (  # noqa: E402
    build_team,
    generate_events,
    sales_rep_uuid,
)

bp = Blueprint("fortellis_mock", __name__)

# Dataset cache — keyed by dealership_id.  Clear via POST /fortellis-mock/clear-cache.
_cache: dict[str, dict] = {}

# How many days of synthetic history to generate per store.
_DATASET_DAYS = 90


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_stores() -> dict[str, dict]:
    """Import _stores lazily to avoid circular imports."""
    from app.routes.stores import _stores  # noqa: PLC0415
    return _stores


def _resolve_store(req) -> tuple[dict | None, str]:
    """Return (store_dict, dealership_id) from Subscription-Id header."""
    sub_id = req.headers.get("Subscription-Id", "").strip()
    stores = _get_stores()
    store = stores.get(sub_id)
    return store, sub_id


def _build_dataset(dealership_id: str, store: dict) -> dict:
    """
    Generate 90 days of events and index them as Elead-format objects.

    Returns:
        {
          "leads":      list[dict],       # FortellisLead shape
          "deals":      list[dict],       # FortellisDeal shape (sold only)
          "activities": dict[str, list],  # opportunityId → list[FortellisActivity]
          "employees":  list[dict],       # FortellisEmployee shape
        }
    """
    from app.routes.stores import _parse_hire_dates  # noqa: PLC0415

    team = build_team(
        salespeople=store.get("salespeople", 8),
        managers=store.get("managers", 2),
        bdc_agents=0,
        archetype_dist=store.get("archetype_dist"),
        new_hire_dates=_parse_hire_dates(store),
    )

    # Build rep UUID → member_id reverse map so we can set salesPersonId
    uuid_to_member: dict[str, str] = {}
    for member in team:
        uuid_to_member[sales_rep_uuid(dealership_id, member)] = member.member_id

    end_date = datetime.now(tz=timezone.utc)
    start_date = end_date - timedelta(days=_DATASET_DAYS)

    events = generate_events(
        start_date=start_date,
        days=_DATASET_DAYS,
        daily_leads=store.get("daily_leads", 20),
        team=team,
        dealership_id=dealership_id,
        seed=store.get("seed", 42),
        base_close_rate=store.get("close_rate_pct", 36) / 100.0,
        deal_amount_min=store.get("deal_amount_min", 12000),
        deal_amount_max=store.get("deal_amount_max", 68000),
        gross_profit_min=store.get("gross_profit_min", 700),
        gross_profit_max=store.get("gross_profit_max", 6000),
        activities_min=store.get("activities_per_deal_min", 2),
        activities_max=store.get("activities_per_deal_max", 6),
        month_shape=store.get("month_shape", "flat"),
        scenarios=store.get("default_scenarios", []),
    )

    # Index events into Elead shapes
    leads: dict[str, dict] = {}       # deal_id → FortellisLead
    deal_statuses: dict[str, str] = {}  # deal_id → latest status
    deal_amounts: dict[str, Any] = {}   # deal_id → saleAmount
    deal_close_dates: dict[str, str] = {}  # deal_id → closeDate
    activities: dict[str, list] = {}   # deal_id → [FortellisActivity]

    for ev in events:
        p = ev.payload
        rep_member_id = uuid_to_member.get(ev.sales_rep_id, ev.sales_rep_id)

        if ev.type == "deal.created":
            deal_id = p.get("deal_id", "")
            leads[deal_id] = {
                "opportunityId": deal_id,
                "salesPersonId": rep_member_id,
                "leadSource": p.get("source"),
                "customerName": p.get("customer_name"),
                "status": "open",
                "createdDate": ev.created_at,
                "updatedDate": ev.created_at,
            }
            deal_statuses[deal_id] = "open"
            deal_amounts[deal_id] = p.get("deal_amount", 0)

        elif ev.type == "deal.status_changed":
            deal_id = p.get("deal_id", "")
            new_status = p.get("new_status", "")
            deal_statuses[deal_id] = new_status
            if new_status == "closed_won":
                deal_close_dates[deal_id] = ev.created_at
            # Keep updatedDate fresh
            if deal_id in leads:
                leads[deal_id]["updatedDate"] = ev.created_at
                leads[deal_id]["status"] = "sold" if new_status == "closed_won" else "open"

        elif ev.type in ("activity.completed", "activity.scheduled"):
            deal_id = p.get("deal_id", "")
            activity: dict[str, Any] = {
                "activityId": p.get("activity_id"),
                "activityName": p.get("activity_type"),
                "assignedTo": rep_member_id,
            }
            if ev.type == "activity.completed":
                activity["completedDate"] = ev.created_at
                activity["completedBy"] = rep_member_id
                activity["outcome"] = p.get("outcome")
            else:
                activity["dueDate"] = p.get("scheduled_for", ev.created_at)

            activities.setdefault(deal_id, []).append(activity)

    # Build FortellisDeal list (closed_won opportunities only)
    deals: list[dict] = []
    for deal_id, status in deal_statuses.items():
        if status == "closed_won" and deal_id in leads:
            lead = leads[deal_id]
            deals.append({
                "opportunityId": deal_id,
                "salesPersonId": lead["salesPersonId"],
                "status": "sold",
                "saleAmount": deal_amounts.get(deal_id, 0),
                "customerName": lead.get("customerName"),
                "closeDate": deal_close_dates.get(deal_id),
            })

    # Build FortellisEmployee list from sales reps only
    employees: list[dict] = [
        {
            "employeeId": m.member_id,
            "firstName": m.name.split()[0] if m.name else m.member_id,
            "lastName": m.name.split()[-1] if m.name else "",
        }
        for m in team
        if m.role == "sales"
    ]

    return {
        "leads": list(leads.values()),
        "deals": deals,
        "activities": activities,
        "employees": employees,
    }


def _dataset(dealership_id: str, store: dict) -> dict:
    if dealership_id not in _cache:
        _cache[dealership_id] = _build_dataset(dealership_id, store)
    return _cache[dealership_id]


# ---------------------------------------------------------------------------
# Token endpoint (mirrors Fortellis identity server path)
# ---------------------------------------------------------------------------

@bp.post("/oauth2/aus1p1ixy7YL8cMq02p7/v1/token")
def mock_token():
    return jsonify({"access_token": "mock-fortellis-token", "expires_in": 3600, "token_type": "Bearer"})


# ---------------------------------------------------------------------------
# Activity Types
# ---------------------------------------------------------------------------

_ACTIVITY_TYPES = [
    {"activityTypeId": 1,  "activityTypeName": "call"},
    {"activityTypeId": 2,  "activityTypeName": "email"},
    {"activityTypeId": 3,  "activityTypeName": "text"},
    {"activityTypeId": 4,  "activityTypeName": "voicemail"},
    {"activityTypeId": 5,  "activityTypeName": "meeting"},
    {"activityTypeId": 6,  "activityTypeName": "appointment"},
    {"activityTypeId": 7,  "activityTypeName": "test_drive"},
    {"activityTypeId": 8,  "activityTypeName": "demo"},
    {"activityTypeId": 9,  "activityTypeName": "note"},
    {"activityTypeId": 10, "activityTypeName": "follow_up"},
]


@bp.get("/sales/v1/elead/activities/activityTypes")
def activity_types():
    return jsonify(_ACTIVITY_TYPES)


# ---------------------------------------------------------------------------
# Opportunities (leads + deals)
# ---------------------------------------------------------------------------

@bp.get("/sales/v2/elead/opportunities/search")
def opportunities_search():
    store, dealership_id = _resolve_store(request)
    if not store:
        return jsonify({"error": f"No DealMaker store found for Subscription-Id '{dealership_id}'"}), 404

    ds = _dataset(dealership_id, store)

    # Filter by status param (fetchDeals passes status=sold)
    status_filter = request.args.get("status", "").lower()
    if status_filter == "sold":
        items = ds["deals"]
    else:
        items = ds["leads"]

    # Pagination — page-number style (matches Elead's API)
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, int(request.args.get("pageSize", 100)))
    except ValueError:
        page, page_size = 1, 100

    total = len(items)
    total_pages = max(1, -(-total // page_size))   # ceiling division
    start = (page - 1) * page_size
    page_items = items[start: start + page_size]

    return jsonify({
        "opportunities": page_items,
        "totalItems": total,
        "totalPages": total_pages,
        "currentPage": page,
    })


# ---------------------------------------------------------------------------
# Activity history by opportunity ID
# ---------------------------------------------------------------------------

@bp.get("/sales/v1/elead/activities/history/byOpportunityId/<path:opportunity_id>")
def activity_history(opportunity_id: str):
    store, dealership_id = _resolve_store(request)
    if not store:
        return jsonify({"error": f"No DealMaker store found for Subscription-Id '{dealership_id}'"}), 404

    ds = _dataset(dealership_id, store)
    acts = ds["activities"].get(opportunity_id, [])
    return jsonify({"activities": acts})


# ---------------------------------------------------------------------------
# Employees reference data
# ---------------------------------------------------------------------------

@bp.get("/sales/v1/elead/reference/employees")
def employees():
    store, dealership_id = _resolve_store(request)
    if not store:
        return jsonify({"error": f"No DealMaker store found for Subscription-Id '{dealership_id}'"}), 404

    ds = _dataset(dealership_id, store)
    return jsonify({"employees": ds["employees"]})


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

@bp.post("/fortellis-mock/clear-cache")
def clear_cache():
    """Force dataset regeneration (call after editing a store's config)."""
    store_id = request.json.get("store_id") if request.is_json else None
    if store_id:
        _cache.pop(store_id, None)
        return jsonify({"cleared": store_id})
    _cache.clear()
    return jsonify({"cleared": "all"})


@bp.get("/fortellis-mock/status")
def mock_status():
    """Show which stores have a cached dataset and their sizes."""
    stores = _get_stores()
    info = {}
    for store_id in stores:
        if store_id in _cache:
            ds = _cache[store_id]
            info[store_id] = {
                "cached": True,
                "leads": len(ds["leads"]),
                "deals": len(ds["deals"]),
                "activities": sum(len(v) for v in ds["activities"].values()),
                "employees": len(ds["employees"]),
            }
        else:
            info[store_id] = {"cached": False}
    return jsonify(info)
