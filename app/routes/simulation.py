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

from app.routes.stores import _OUTPUT_DIR, _stores, _parse_hire_dates

# Absolute path to the project root so output/ is always found regardless of CWD.
_APP_ROOT = Path(__file__).parent.parent.parent

# Import core generation helpers from v1 generator (still valid in v2)
sys.path.insert(0, str(_APP_ROOT))
from dealmaker_generator import build_team, generate_events, normalize_delivery_url, send_events_to_api

bp = Blueprint("simulation", __name__, url_prefix="/simulation")

# Absolute path to project root — avoids CWD-relative bugs in production.
_APP_ROOT = Path(__file__).parent.parent.parent

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
                api_url = normalize_delivery_url(os.getenv("TOPREP_API_URL", ""))
                auth_token = os.getenv("TOPREP_AUTH_TOKEN", "")
                supabase_apikey = os.getenv("SUPABASE_ANON_KEY", "")
                if api_url:
                    result = send_events_to_api(events, api_url, auth_token, supabase_apikey)
                    if result["failed"] > 0 and result["errors"]:
                        self.last_error = str(result["errors"][0])[:140]
                    else:
                        self.last_error = None
                else:
                    self.last_error = "TOPREP_API_URL not configured"

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
    store = _stores.get(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404

    if store_id in _runners and _runners[store_id].is_alive():
        return jsonify({"status": "already_running"})

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
