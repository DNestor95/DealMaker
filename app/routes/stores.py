"""
Store / Dealership routes.

GET  /              → list all configured stores
GET  /stores/new    → new store form
POST /stores/new    → create store + seed initial data into TopRep DB
GET  /stores/<id>   → store detail / live stats
POST /stores/<id>/start  → start background simulation for store
POST /stores/<id>/stop   → stop simulation for store
DELETE /stores/<id>      → remove store from session
"""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, session, url_for

from app.supabase_client import get_profiles, rest_get

bp = Blueprint("stores", __name__)

# ---------------------------------------------------------------------------
# In-memory store registry (replace with DB persistence in a later iteration)
# ---------------------------------------------------------------------------
_stores: dict[str, dict] = {}


@bp.route("/")
def index():
    return render_template("stores/list.html", stores=list(_stores.values()))


@bp.route("/stores/new", methods=["GET"])
def new_store():
    return render_template(
        "stores/new.html",
        lead_sources=["internet", "phone", "showroom", "referral", "service", "walkin"],
        deal_statuses=["lead", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"],
        activity_types=["call", "email", "meeting", "demo", "note"],
        activity_outcomes=[
            "connected", "no_answer", "left_vm", "appt_set",
            "showed", "no_show", "sold", "lost", "negotiating", "follow_up",
        ],
        rep_roles=["sales_rep", "manager", "bdc"],
    )


@bp.route("/stores/new", methods=["POST"])
def create_store():
    data = request.form

    store_id = data.get("dealership_id", "").strip()
    if not store_id:
        return render_template("stores/new.html", error="Dealership ID is required.")

    store = {
        "dealership_id": store_id,
        "salespeople": int(data.get("salespeople", 8)),
        "managers": int(data.get("managers", 2)),
        "bdc_agents": int(data.get("bdc_agents", 3)),
        "daily_leads": int(data.get("daily_leads", 20)),
        "lead_sources": data.getlist("lead_sources") or ["internet", "phone", "showroom"],
        "deal_statuses": data.getlist("deal_statuses") or ["lead", "qualified", "closed_won", "closed_lost"],
        "activity_types": data.getlist("activity_types") or ["call", "email", "meeting"],
        "activity_outcomes": data.getlist("activity_outcomes") or ["connected", "appt_set", "showed", "sold"],
        # deal amount / gross profit range controls
        "deal_amount_min": int(data.get("deal_amount_min", 12000)),
        "deal_amount_max": int(data.get("deal_amount_max", 68000)),
        "gross_profit_min": int(data.get("gross_profit_min", 700)),
        "gross_profit_max": int(data.get("gross_profit_max", 6000)),
        # rep behaviour weights (0-100 sliders, used to bias simulation)
        "close_rate_pct": int(data.get("close_rate_pct", 36)),
        "status_advance_pct": int(data.get("status_advance_pct", 88)),
        "activities_per_deal_min": int(data.get("activities_per_deal_min", 2)),
        "activities_per_deal_max": int(data.get("activities_per_deal_max", 6)),
        # delivery
        "delivery": data.get("delivery", "file"),
        "batch_days": int(data.get("batch_days", 1)),
        "every_seconds": int(data.get("every_seconds", 10)),
        "seed": int(data.get("seed", 42)),
        # status
        "status": "stopped",
        "events_sent": 0,
    }

    _stores[store_id] = store
    return redirect(url_for("stores.store_detail", store_id=store_id))


@bp.route("/stores/<store_id>")
def store_detail(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return render_template("404.html"), 404

    # Try to pull live rep profiles tied to this store from TopRep
    profiles = get_profiles()
    store_profiles = [p for p in profiles if p.get("store_id") == store_id]

    return render_template("stores/detail.html", store=store, profiles=store_profiles)
