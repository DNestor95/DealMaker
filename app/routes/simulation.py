"""
Simulation routes — start / stop / status / report for background store runners.

POST /simulation/<store_id>/start
POST /simulation/<store_id>/stop
GET  /simulation/<store_id>/status  → JSON status snapshot
GET  /simulation/<store_id>/report  → HTML simulation report
"""
from __future__ import annotations

import json
import os
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template

from dealmaker_postgres import database_url_from_env, is_postgres_dsn
from app.routes.stores import SPEED_PRESETS, _OUTPUT_DIR, _stores, _parse_hire_dates

# Absolute path to the project root so output/ is always found regardless of CWD.
_APP_ROOT = Path(__file__).parent.parent.parent

# Import core generation helpers from v1 generator (still valid in v2)
sys.path.insert(0, str(_APP_ROOT))
from dealmaker_generator import AUTH_ERROR_401, build_team, generate_events, normalize_delivery_url, send_events_to_api
from app.supabase_client import _api_url as _supabase_api_url, _anon_key as _supabase_anon_key

bp = Blueprint("simulation", __name__, url_prefix="/simulation")

# Absolute path to project root — avoids CWD-relative bugs in production.
_APP_ROOT = Path(__file__).parent.parent.parent


def _resolve_api_url() -> str:
    """Return the delivery URL, falling back to the hardcoded TopRep Supabase URL."""
    url = os.getenv("TOPREP_API_URL", "").strip() or database_url_from_env().strip()
    if not url:
        url = _supabase_api_url()  # hardcoded fallback in supabase_client
    return normalize_delivery_url(url)


def _resolve_anon_key() -> str:
    return os.getenv("SUPABASE_ANON_KEY", "") or _supabase_anon_key()

# ---------------------------------------------------------------------------
# Running thread registry
# ---------------------------------------------------------------------------
_runners: dict[str, "_StoreThread"] = {}


class _StoreThread(threading.Thread):
    def __init__(self, store: dict) -> None:
        super().__init__(daemon=True)
        self._store = store
        self._stop_event = threading.Event()
        self.events_sent = 0
        self.last_batch_at: str | None = None
        self.last_error: str | None = None
        # Accelerated-time tracking (populated during run)
        self.simulated_date: str | None = None
        self.sim_days_elapsed: float = 0.0
        self.speed_preset: str = store.get("sim_speed_preset", "realtime")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        s = self._store

        # ── Resolve speed multiplier ───────────────────────────────────────
        preset = s.get("sim_speed_preset", "realtime")
        if preset == "custom":
            try:
                speed_mult = max(1.0, float(s.get("sim_speed_multiplier", 1.0)))
            except (ValueError, TypeError):
                speed_mult = 1.0
        else:
            speed_mult = SPEED_PRESETS.get(preset, SPEED_PRESETS["realtime"])["multiplier"] or 1.0

        batch_days: int = s.get("batch_days", 1)

        # Real seconds to sleep between batches.
        # For realtime mode keep the original every_seconds behaviour; for
        # accelerated modes derive the interval from the speed multiplier so
        # that batch_days of simulated time passes in the correct real time.
        if speed_mult <= 1.0:
            sleep_seconds: float = float(s.get("every_seconds", 10))
        else:
            sleep_seconds = (batch_days * 86400.0) / speed_mult

        # ── Determine simulation start date ───────────────────────────────
        raw_start = s.get("sim_start_date", "")
        if raw_start:
            try:
                sim_current = datetime.fromisoformat(raw_start).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                sim_current = datetime.now(timezone.utc)
        else:
            sim_current = datetime.now(timezone.utc)

        self.simulated_date = sim_current.isoformat().replace("+00:00", "Z")

        # ── Total days cap (0 = run indefinitely) ─────────────────────────
        sim_days_total: int = int(s.get("sim_days_total", 0))

        # ── Build team once ───────────────────────────────────────────────
        team = build_team(
            salespeople=s["salespeople"],
            managers=s["managers"],
            bdc_agents=s["bdc_agents"],
            archetype_dist=s.get("archetype_dist"),
            new_hire_dates=_parse_hire_dates(s),
        )
        # Prefer provisioned profile IDs so events.sales_rep_id always points to
        # existing profiles rows (avoids FK 23503 errors on /rest/v1/events).
        explicit_rep_ids = [
            c.get("user_id")
            for c in s.get("credentials", [])
            if isinstance(c, dict) and c.get("user_id") and not c.get("error")
        ]
        batch = 0

        output_dir = _OUTPUT_DIR / "stores"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{s['dealership_id']}.jsonl"

        while not self._stop_event.is_set():
            # Auto-stop when the requested total days have been simulated
            if sim_days_total > 0 and self.sim_days_elapsed >= sim_days_total:
                break

            events = generate_events(
                start_date=sim_current,
                days=batch_days,
                daily_leads=s["daily_leads"],
                team=team,
                dealership_id=s["dealership_id"],
                seed=s["seed"] + batch,
                sales_rep_ids=explicit_rep_ids or None,
                base_close_rate=s.get("close_rate_pct", 36) / 100.0,
                deal_amount_min=s.get("deal_amount_min", 12000),
                deal_amount_max=s.get("deal_amount_max", 68000),
                gross_profit_min=s.get("gross_profit_min", 700),
                gross_profit_max=s.get("gross_profit_max", 6000),
                activities_min=s.get("activities_per_deal_min", 2),
                activities_max=s.get("activities_per_deal_max", 6),
                month_shape=s.get("month_shape", "flat"),
                scenarios=s.get("default_scenarios", []),
            )

            if s["delivery"] in {"file", "both"}:
                with output_file.open("a", encoding="utf-8") as fh:
                    for ev in events:
                        fh.write(json.dumps(ev.to_dict(), separators=(",", ":")) + "\n")

            if s["delivery"] in {"api", "both"}:
                api_url = _resolve_api_url()
                # For direct Supabase REST writes (/rest/v1/events) we must use
                # the service role key as the bearer token — it bypasses RLS so
                # synthetic events for any rep UUID can be inserted without the
                # caller's user-JWT restricting writes to only their own sub.
                # For the TopRep Next.js /api/events route (contains /api/events
                # or a non-Supabase domain), prefer the user JWT instead.
                service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
                user_jwt = os.getenv("TOPREP_AUTH_TOKEN", "").strip()
                is_rest_write = "/rest/v1/" in api_url
                if is_rest_write and service_key:
                    auth_token = service_key
                else:
                    auth_token = user_jwt or service_key
                supabase_apikey = _resolve_anon_key()
                if api_url:
                    result = send_events_to_api(
                        events,
                        api_url,
                        auth_token,
                        supabase_apikey,
                        timeout_seconds=8,
                        max_retries=0,
                    )
                    if result["failed"] > 0 and result["errors"]:
                        self.last_error = str(result["errors"][0])[:140]
                        # Stop the thread on authentication failure to avoid
                        # burning through retries with a token that won't work.
                        if self.last_error.startswith(AUTH_ERROR_401) or "23503" in self.last_error:
                            self._stop_event.set()
                    else:
                        self.last_error = None
                else:
                    self.last_error = "Could not resolve API URL — check Settings"

            self.events_sent += len(events)
            self.last_batch_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            s["events_sent"] = self.events_sent
            batch += 1

            # Advance the simulated clock
            sim_current += timedelta(days=batch_days)
            self.sim_days_elapsed += batch_days
            self.simulated_date = sim_current.isoformat().replace("+00:00", "Z")

            if self._stop_event.wait(sleep_seconds):
                break

        s["status"] = "stopped"


@bp.route("/<store_id>/start", methods=["POST"])
def start(store_id: str):
    # Vercel (and other serverless runtimes) do not support persistent background
    # threads — each request runs in an isolated, short-lived function instance.
    # Return a clear error instead of silently starting a thread that will die
    # as soon as the HTTP response is sent.
    if os.environ.get("VERCEL"):
        return jsonify({
            "error": "Live simulation is not available on Vercel.",
            "hint": (
                "Vercel is a serverless platform: background threads cannot persist "
                "between requests.  Use the Backfill feature to generate historical "
                "data, or run DealMaker locally / on Railway for live simulation."
            ),
        }), 503

    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404

    if store_id in _runners and _runners[store_id].is_alive():
        return jsonify({"status": "already_running"})

    # Pre-flight auth check when events are delivered to the API.
    if store.get("delivery") in {"api", "both"}:
        api_url = _resolve_api_url()
        has_token = (
            os.getenv("TOPREP_AUTH_TOKEN", "").strip()
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        if not is_postgres_dsn(api_url) and not has_token:
            return jsonify({
                "error": "Authentication failed (HTTP 401) — check TOPREP_AUTH_TOKEN.",
                "hint": "Set TOPREP_AUTH_TOKEN or SUPABASE_SERVICE_ROLE_KEY in Settings before starting an API-delivery simulation.",
            }), 401

    thread = _StoreThread(store)
    _runners[store_id] = thread
    store["status"] = "running"
    thread.start()
    return jsonify({"status": "started"})


@bp.route("/<store_id>/stop", methods=["POST"])
def stop(store_id: str):
    thread = _runners.get(store_id)
    if thread:
        thread.stop()
    store = _stores.get(store_id)
    if store:
        store["status"] = "stopping"
    return jsonify({"status": "stopping"})


@bp.route("/<store_id>/status")
def status(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404
    thread = _runners.get(store_id)
    sim_days_total = int(store.get("sim_days_total", 0))
    sim_days_elapsed = thread.sim_days_elapsed if thread else 0.0
    progress_pct = (
        round(sim_days_elapsed / sim_days_total * 100, 1)
        if sim_days_total > 0
        else None
    )
    return jsonify({
        "store_id": store_id,
        "status": store.get("status", "stopped"),
        "events_sent": store.get("events_sent", 0),
        "last_batch_at": thread.last_batch_at if thread else None,
        "last_error": thread.last_error if thread else None,
        "simulated_date": thread.simulated_date if thread else None,
        "sim_days_elapsed": sim_days_elapsed,
        "sim_days_total": sim_days_total if sim_days_total > 0 else None,
        "progress_pct": progress_pct,
        "speed_preset": store.get("sim_speed_preset", "realtime"),
    })


# ---------------------------------------------------------------------------
# Report endpoint
# ---------------------------------------------------------------------------

def _build_report(store_id: str) -> dict | None:
    """Parse the store's JSONL output file and return aggregated stats."""
    output_file = _OUTPUT_DIR / "stores" / f"{store_id}.jsonl"
    if not output_file.exists():
        return None

    type_counts: dict[str, int] = defaultdict(int)
    deal_amounts: list[float] = []
    gross_profits: list[float] = []
    close_won = 0
    close_lost = 0
    rep_event_counts: dict[str, int] = defaultdict(int)
    activity_type_counts: dict[str, int] = defaultdict(int)
    source_counts: dict[str, int] = defaultdict(int)
    daily_deals: dict[str, int] = defaultdict(int)

    with output_file.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            ev_type: str = ev.get("type", "unknown")
            type_counts[ev_type] += 1

            rep_id: str | None = ev.get("sales_rep_id")
            if rep_id:
                rep_event_counts[rep_id] += 1

            payload: dict = ev.get("payload", {})

            if ev_type == "deal.created":
                try:
                    if payload.get("deal_amount"):
                        deal_amounts.append(float(payload["deal_amount"]))
                    if payload.get("gross_profit"):
                        gross_profits.append(float(payload["gross_profit"]))
                except (ValueError, TypeError):
                    pass
                if payload.get("source"):
                    source_counts[payload["source"]] += 1
                # Track daily deal volume
                created_at: str = ev.get("created_at", "")
                if created_at:
                    day_key = created_at[:10]
                    daily_deals[day_key] += 1

            elif ev_type == "deal.status_changed":
                # Generator uses "new_status" (per REALTIME_DATA_INGEST_REFERENCE.md contract).
                new_status = payload.get("new_status", "")
                if new_status == "closed_won":
                    close_won += 1
                elif new_status == "closed_lost":
                    close_lost += 1

            elif ev_type == "activity.completed":
                act_type = payload.get("activity_type", "unknown")
                activity_type_counts[act_type] += 1

    total_deals = type_counts.get("deal.created", 0)
    close_rate = round(close_won / total_deals * 100, 1) if total_deals else 0.0
    avg_deal_amount = round(sum(deal_amounts) / len(deal_amounts)) if deal_amounts else 0
    avg_gross_profit = round(sum(gross_profits) / len(gross_profits)) if gross_profits else 0

    # Sort top reps by event count
    top_reps = sorted(rep_event_counts.items(), key=lambda x: x[1], reverse=True)[:20]

    # Sort daily deals for a time-series view
    daily_series = sorted(daily_deals.items())

    return {
        "store_id": store_id,
        "total_events": sum(type_counts.values()),
        "event_type_counts": dict(type_counts),
        "total_deals": total_deals,
        "closed_won": close_won,
        "closed_lost": close_lost,
        "close_rate_pct": close_rate,
        "avg_deal_amount": avg_deal_amount,
        "avg_gross_profit": avg_gross_profit,
        "unique_reps": len(rep_event_counts),
        "top_reps": top_reps,
        "activity_type_counts": dict(activity_type_counts),
        "source_counts": dict(source_counts),
        "daily_deals": daily_series,
        "file_path": str(output_file),
    }


@bp.route("/<store_id>/report")
def report(store_id: str):
    store = _stores.get(store_id)
    if not store:
        return render_template("404.html"), 404

    stats = _build_report(store_id)
    speed_label = SPEED_PRESETS.get(
        store.get("sim_speed_preset", "realtime"),
        SPEED_PRESETS["realtime"],
    )["label"]

    return render_template(
        "simulation/report.html",
        store=store,
        stats=stats,
        speed_label=speed_label,
    )
