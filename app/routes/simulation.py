"""
Simulation routes — start / stop / status for background store runners.

POST /simulation/<store_id>/start
POST /simulation/<store_id>/stop
GET  /simulation/<store_id>/status  → JSON status snapshot
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

from app.routes.stores import _stores, _parse_hire_dates
from app.supabase_client import post_event

# Import core generation helpers from v1 generator (still valid in v2)
import sys, os
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dealmaker_generator import build_team, generate_events

bp = Blueprint("simulation", __name__, url_prefix="/simulation")

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

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        s = self._store
        team = build_team(
            salespeople=s["salespeople"],
            managers=s["managers"],
            bdc_agents=s["bdc_agents"],
            archetype_dist=s.get("archetype_dist"),
            new_hire_dates=_parse_hire_dates(s),
        )
        batch = 0

        output_dir = Path("output/stores")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{s['dealership_id']}.jsonl"

        while not self._stop_event.is_set():
            events = generate_events(
                start_date=datetime.now(timezone.utc),
                days=s["batch_days"],
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
                for ev in events:
                    result = post_event(ev.to_dict())
                    if "error" in result:
                        self.last_error = str(result["error"])[:140]
                    else:
                        self.last_error = None

            self.events_sent += len(events)
            self.last_batch_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            s["events_sent"] = self.events_sent
            batch += 1

            if self._stop_event.wait(s["every_seconds"]):
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
    return jsonify({
        "store_id": store_id,
        "status": store.get("status", "stopped"),
        "events_sent": store.get("events_sent", 0),
        "last_batch_at": thread.last_batch_at if thread else None,
        "last_error": thread.last_error if thread else None,
    })
