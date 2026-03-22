from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib import error, request


STATUS_VALUES = ["lead", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
ACTIVITY_TYPES = ["call", "email", "meeting", "demo", "note"]
ACTIVITY_OUTCOMES = [
    "connected",
    "no_answer",
    "left_vm",
    "appt_set",
    "showed",
    "no_show",
    "sold",
    "lost",
    "negotiating",
    "follow_up",
]
DEAL_SOURCES = ["internet", "phone", "walk_in", "referral", "third_party"]


def load_env_file(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1]

        if key not in os.environ:
            os.environ[key] = value


def fetch_profiles_from_supabase(
    project_url: str,
    auth_token: str,
    supabase_apikey: str = "",
) -> list[dict[str, Any]]:
    """Fetch all profiles from Supabase REST to build the rep pool for round-robin assignment."""
    base = project_url.rstrip("/").split("/functions/")[0].split("/rest/")[0]
    if ".supabase.co" not in base:
        return []
    url = f"{base}/rest/v1/profiles?select=id,first_name,last_name,role&role=eq.sales_rep&order=created_at.asc"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
    }
    if supabase_apikey:
        headers["apikey"] = supabase_apikey
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return []


@dataclass
class TeamMember:
    member_id: str
    role: str
    name: str


@dataclass
class Event:
    sales_rep_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sales_rep_id": self.sales_rep_id,
            "type": self.type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


def random_business_time(day: datetime, rng: random.Random) -> datetime:
    start = day.replace(hour=8, minute=0, second=0, microsecond=0)
    minutes = rng.randint(0, 11 * 60 + 59)
    return start + timedelta(minutes=minutes)


def build_team(salespeople: int, managers: int, bdc_agents: int) -> list[TeamMember]:
    team: list[TeamMember] = []
    for i in range(1, salespeople + 1):
        team.append(TeamMember(member_id=f"S-{i:03d}", role="sales", name=f"Sales Rep {i}"))
    for i in range(1, managers + 1):
        team.append(TeamMember(member_id=f"M-{i:03d}", role="manager", name=f"Manager {i}"))
    for i in range(1, bdc_agents + 1):
        team.append(TeamMember(member_id=f"B-{i:03d}", role="bdc", name=f"BDC Agent {i}"))
    return team


def pick_member(team: list[TeamMember], role: str, rng: random.Random) -> TeamMember:
    eligible = [member for member in team if member.role == role]
    if not eligible:
        eligible = team
    return rng.choice(eligible)


def sales_rep_uuid(dealership_id: str, member: TeamMember) -> str:
    return stable_uuid("sales_rep", dealership_id, member.member_id)


def extract_user_id_from_jwt(auth_token: str) -> str | None:
    token = auth_token.strip()
    if token.count(".") != 2:
        return None

    try:
        payload_segment = token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_segment + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None

    try:
        parsed = uuid.UUID(sub)
    except ValueError:
        return None
    return str(parsed)


def make_event(
    ts: datetime,
    dealership_id: str,
    member: TeamMember,
    event_type: str,
    payload: dict[str, Any],
    sales_rep_id_override: str | None = None,
) -> Event:
    sales_rep_id = sales_rep_id_override or sales_rep_uuid(dealership_id, member)
    return Event(
        sales_rep_id=sales_rep_id,
        type=event_type,
        payload=payload,
        created_at=to_iso(ts),
    )


def generate_deal_workflow(
    day: datetime,
    deal_number: int,
    team: list[TeamMember],
    dealership_id: str,
    rng: random.Random,
    sales_rep_id_override: str | None = None,
) -> list[Event]:
    events: list[Event] = []

    created_ts = random_business_time(day, rng)
    sales_member = pick_member(team, "sales", rng)
    manager_member = pick_member(team, "manager", rng)

    deal_id = stable_uuid("deal", dealership_id, day.strftime("%Y%m%d"), str(deal_number))
    customer_name = f"Customer {deal_number:05d}"
    deal_amount = rng.randint(12000, 68000)
    gross_profit = rng.randint(700, 6000)

    events.append(
        make_event(
            ts=created_ts,
            dealership_id=dealership_id,
            member=sales_member,
            event_type="deal.created",
            sales_rep_id_override=sales_rep_id_override,
            payload={
                "deal_id": deal_id,
                "customer_name": customer_name,
                "deal_amount": deal_amount,
                "gross_profit": gross_profit,
                "source": rng.choice(DEAL_SOURCES),
            },
        )
    )

    current_status = "lead"
    status_ts = created_ts + timedelta(minutes=rng.randint(10, 45))
    status_path = ["qualified", "proposal", "negotiation"]

    for next_status in status_path:
        if rng.random() < 0.88:
            events.append(
                make_event(
                    ts=status_ts,
                    dealership_id=dealership_id,
                    member=sales_member,
                    event_type="deal.status_changed",
                    sales_rep_id_override=sales_rep_id_override,
                    payload={
                        "deal_id": deal_id,
                        "old_status": current_status,
                        "new_status": next_status,
                    },
                )
            )
            current_status = next_status
            status_ts += timedelta(minutes=rng.randint(15, 90))

    activity_count = rng.randint(2, 6)
    for activity_index in range(1, activity_count + 1):
        activity_type = rng.choice(ACTIVITY_TYPES)
        activity_id = stable_uuid("activity", deal_id, str(activity_index))

        scheduled_ts = created_ts + timedelta(minutes=rng.randint(20, 360))
        events.append(
            make_event(
                ts=scheduled_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="activity.scheduled",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "activity_id": activity_id,
                    "deal_id": deal_id,
                    "activity_type": activity_type,
                    "scheduled_for": to_iso(scheduled_ts + timedelta(minutes=rng.randint(5, 180))),
                },
            )
        )

        completed_ts = scheduled_ts + timedelta(minutes=rng.randint(5, 220))
        outcome = rng.choice(ACTIVITY_OUTCOMES)
        if current_status == "negotiation" and rng.random() < 0.45:
            outcome = rng.choice(["sold", "negotiating", "follow_up", "lost"])

        events.append(
            make_event(
                ts=completed_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="activity.completed",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "activity_id": activity_id,
                    "deal_id": deal_id,
                    "activity_type": activity_type,
                    "outcome": outcome,
                },
            )
        )

    close_won = rng.random() < 0.36
    close_ts = status_ts + timedelta(minutes=rng.randint(30, 240))
    if close_won:
        events.append(
            make_event(
                ts=close_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="deal.status_changed",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "deal_id": deal_id,
                    "old_status": current_status,
                    "new_status": "closed_won",
                    "reason": "sold",
                },
            )
        )
    else:
        reason = rng.choice(["price", "timing", "credit", "vehicle_unavailable", "no_response"])
        events.append(
            make_event(
                ts=close_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="deal.status_changed",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "deal_id": deal_id,
                    "old_status": current_status,
                    "new_status": "closed_lost",
                    "reason": reason,
                },
            )
        )

    if rng.random() < 0.06:
        month = day.strftime("%Y-%m")
        old_quota = rng.randint(30, 80)
        new_quota = max(1, old_quota + rng.randint(-10, 15))
        events.append(
            make_event(
                ts=created_ts + timedelta(minutes=rng.randint(1, 30)),
                dealership_id=dealership_id,
                member=manager_member,
                event_type="rep_quota_updated",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "month": month,
                    "old_quota": old_quota,
                    "new_quota": new_quota,
                    "reason": rng.choice(["seasonality", "management_adjustment", "performance_retarget"]),
                },
            )
        )

    return events


def generate_events(
    start_date: datetime,
    days: int,
    daily_leads: int,
    team: list[TeamMember],
    dealership_id: str,
    seed: int,
    sales_rep_id_override: str | None = None,
    sales_rep_ids: list[str] | None = None,
) -> list[Event]:
    rng = random.Random(seed)
    events: list[Event] = []
    deal_counter = 1

    # Build rep rotation pool: explicit list > single override > generated UUIDs
    rep_pool: list[str] | None = None
    if sales_rep_ids:
        rep_pool = sales_rep_ids
    elif sales_rep_id_override:
        rep_pool = [sales_rep_id_override]

    for day_offset in range(days):
        day = start_date + timedelta(days=day_offset)
        leads_today = max(1, int(rng.gauss(daily_leads, max(2.0, daily_leads * 0.25))))
        for _ in range(leads_today):
            # Round-robin: each new deal goes to the next rep in the pool
            assigned = rep_pool[(deal_counter - 1) % len(rep_pool)] if rep_pool else None
            events.extend(
                generate_deal_workflow(
                    day=day,
                    deal_number=deal_counter,
                    team=team,
                    dealership_id=dealership_id,
                    rng=rng,
                    sales_rep_id_override=assigned,
                )
            )
            deal_counter += 1

    events.sort(key=lambda event: event.created_at)
    return events


def write_jsonl(events: list[Event], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")


def write_csv(events: list[Event], output_path: Path) -> None:
    rows = [event.to_dict() for event in events]
    fields = ["sales_rep_id", "type", "payload", "created_at"]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            row["payload"] = json.dumps(row["payload"], separators=(",", ":"))
            writer.writerow(row)


def post_event_to_api(
    event: Event,
    api_url: str,
    auth_token: str,
    supabase_apikey: str = "",
    timeout_seconds: int = 15,
) -> tuple[bool, str]:
    payload = json.dumps(event.to_dict(), separators=(",", ":")).encode("utf-8")

    is_supabase_rest = "/rest/v1/" in api_url
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
    }
    if is_supabase_rest:
        headers["apikey"] = supabase_apikey
        headers["Prefer"] = "return=minimal"

    req = request.Request(api_url, data=payload, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            status_ok = HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES
            return status_ok, f"status={response.status}"
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"http_error={exc.code} body={detail}"
    except error.URLError as exc:
        return False, f"url_error={exc.reason}"


_EVENT_TYPE_TO_ACTIVITY_TYPE: dict[str, str] = {
    "deal.created": "note",
    "deal.status_changed": "note",
    "activity.scheduled": "",   # use payload.activity_type
    "activity.completed": "",   # use payload.activity_type
    "rep_quota_updated": "note",
}


def event_to_action(event: Event) -> dict[str, Any]:
    payload = event.payload

    # Use payload's activity_type for activity events; fall back to mapped value
    mapped = _EVENT_TYPE_TO_ACTIVITY_TYPE.get(event.type, "note")
    activity_type = payload.get("activity_type") or mapped or "note"

    # Build a human-readable description from available context
    outcome = payload.get("outcome")
    new_status = payload.get("new_status")
    source = payload.get("source")
    if outcome:
        description = outcome
    elif new_status:
        description = f"Status changed to {new_status}"
    elif source:
        description = f"Lead from {source}"
    else:
        description = event.type

    row: dict[str, Any] = {
        # Edge function validation expects these names
        "rep_id": event.sales_rep_id,
        "action_type": activity_type,
        # DB activities table column names
        "sales_rep_id": event.sales_rep_id,
        "activity_type": activity_type,
        "deal_id": payload.get("deal_id"),
        "description": description,
        "created_at": event.created_at,
    }

    # Include scheduled_at / completed_at only when present
    if payload.get("scheduled_at"):
        row["scheduled_at"] = payload["scheduled_at"]
    if payload.get("completed_at"):
        row["completed_at"] = payload["completed_at"]

    return row


def events_to_deals(events: list[Event]) -> list[dict[str, Any]]:
    """Build deal upsert rows from deal.created and deal.status_changed events."""
    deals: dict[str, dict[str, Any]] = {}

    for event in events:
        p = event.payload
        deal_id = p.get("deal_id")
        if not deal_id:
            continue

        if event.type == "deal.created":
            deals[deal_id] = {
                "id": deal_id,
                "sales_rep_id": event.sales_rep_id,
                "customer_name": p.get("customer_name", ""),
                "deal_amount": p.get("deal_amount"),
                "gross_profit": p.get("gross_profit"),
                "status": "lead",
                "source": p.get("source"),
                "created_at": event.created_at,
                "updated_at": event.created_at,
            }
        elif event.type == "deal.status_changed" and deal_id in deals:
            deals[deal_id]["status"] = p.get("new_status", deals[deal_id]["status"])
            deals[deal_id]["updated_at"] = event.created_at
            if p.get("new_status") in ("closed_won", "closed_lost"):
                deals[deal_id]["close_date"] = event.created_at[:10]

    return list(deals.values())


def post_actions_batch_to_edge(
    events: list[Event],
    api_url: str,
    auth_token: str,
    supabase_apikey: str = "",
    timeout_seconds: int = 15,
) -> tuple[bool, str, int]:
    body = {
        "deals": events_to_deals(events),
        "actions": [event_to_action(event) for event in events],
    }
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
    }
    if supabase_apikey:
        headers["apikey"] = supabase_apikey

    req = request.Request(api_url, data=payload, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            inserted = len(events)
            if response_text:
                try:
                    parsed = json.loads(response_text)
                    if isinstance(parsed, dict) and isinstance(parsed.get("inserted"), int):
                        inserted = parsed["inserted"]
                except json.JSONDecodeError:
                    pass
            status_ok = HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES
            return status_ok, f"status={response.status}", inserted
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"http_error={exc.code} body={detail}", 0
    except error.URLError as exc:
        return False, f"url_error={exc.reason}", 0


def send_events_to_api(
    events: list[Event],
    api_url: str,
    auth_token: str,
    supabase_apikey: str = "",
    timeout_seconds: int = 15,
    max_retries: int = 3,
) -> dict[str, Any]:
    if "/functions/v1/" in api_url:
        delivered = False
        last_error = ""
        inserted = 0
        for attempt in range(max_retries + 1):
            ok, detail, inserted_count = post_actions_batch_to_edge(
                events=events,
                api_url=api_url,
                auth_token=auth_token,
                supabase_apikey=supabase_apikey,
                timeout_seconds=timeout_seconds,
            )
            if ok:
                delivered = True
                inserted = inserted_count
                break
            last_error = detail
            if attempt < max_retries:
                time.sleep(0.5 * (2**attempt))

        if delivered:
            return {"sent": inserted, "failed": max(0, len(events) - inserted), "errors": []}
        return {"sent": 0, "failed": len(events), "errors": [last_error]}

    sent = 0
    failed = 0
    errors: list[str] = []

    for event in events:
        delivered = False
        last_error = ""
        for attempt in range(max_retries + 1):
            ok, detail = post_event_to_api(
                event=event,
                api_url=api_url,
                auth_token=auth_token,
                supabase_apikey=supabase_apikey,
                timeout_seconds=timeout_seconds,
            )
            if ok:
                sent += 1
                delivered = True
                break
            last_error = detail
            if attempt < max_retries:
                time.sleep(0.5 * (2**attempt))

        if not delivered:
            failed += 1
            if len(errors) < 10:
                errors.append(last_error)

    return {"sent": sent, "failed": failed, "errors": errors}


def validate_api_settings(api_url: str, auth_token: str, supabase_apikey: str = "") -> None:
    if not api_url:
        raise ValueError("API URL is required for API delivery")
    if not (api_url.startswith("http://") or api_url.startswith("https://")):
        raise ValueError("API URL must start with http:// or https://")
    if "/api/events" not in api_url and "/rest/v1/" not in api_url and "/functions/v1/" not in api_url and ".supabase.co" not in api_url:
        raise ValueError("API URL must be TOP REP /api/events, Supabase /rest/v1/*, or Supabase /functions/v1/*")

    if not auth_token:
        raise ValueError("Auth token is required for API delivery")
    # For REST endpoints, block publishable keys as bearer (user JWT required)
    # Edge Functions accept sb_publishable_* directly as the anon bearer token (new Supabase key format)
    if "/rest/v1/" in api_url and (
        auth_token.startswith("sb_publishable_") or auth_token.startswith("sb_secret_")
    ):
        raise ValueError("Direct Supabase REST writes require a user JWT as auth token (not sb_publishable/sb_secret)")
    if ("/rest/v1/" in api_url or "/functions/v1/" in api_url) and not supabase_apikey and not auth_token.startswith("sb_publishable_"):
        raise ValueError("SUPABASE_ANON_KEY or --supabase-apikey is required for Supabase writes")


def normalize_delivery_url(api_url: str) -> str:
    trimmed = api_url.strip().rstrip("/")
    if not trimmed:
        return trimmed
    if "/api/events" in trimmed or "/rest/v1/" in trimmed or "/functions/v1/" in trimmed:
        return trimmed
    if ".supabase.co" in trimmed:
        return f"{trimmed}/rest/v1/events"
    return trimmed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic TOP REP event traffic for dealership sales testing."
    )
    parser.add_argument("--start-date", default=datetime.now().strftime("%Y-%m-%d"), help="Start date in YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=14, help="Number of days to simulate")
    parser.add_argument("--daily-leads", type=int, default=20, help="Average number of deals/leads per day")
    parser.add_argument("--salespeople", type=int, default=8, help="Number of sales reps")
    parser.add_argument("--managers", type=int, default=2, help="Number of managers")
    parser.add_argument("--bdc", type=int, default=3, help="Number of BDC agents")
    parser.add_argument("--dealership-id", default="DLR-001", help="Dealership identifier")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible output")
    parser.add_argument(
        "--delivery",
        choices=["file", "api", "both"],
        default="file",
        help="Where to send events: local file, API, or both",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("TOPREP_API_URL", ""),
        help="TOP REP ingest endpoint, e.g. https://<domain>/api/events",
    )
    parser.add_argument(
        "--auth-token",
        default="",
        help="Bearer token for API writes (or set TOPREP_AUTH_TOKEN env var)",
    )
    parser.add_argument(
        "--supabase-apikey",
        default=os.getenv("SUPABASE_ANON_KEY", ""),
        help="Supabase anon/publishable key (required for direct Supabase REST writes)",
    )
    parser.add_argument(
        "--sales-rep-id",
        default=os.getenv("TOPREP_SALES_REP_ID", ""),
        help="Single sales_rep_id UUID override (use --sales-rep-ids for round-robin)",
    )
    parser.add_argument(
        "--sales-rep-ids",
        default=os.getenv("TOPREP_SALES_REP_IDS", ""),
        help="Comma-separated UUIDs for round-robin deal assignment across reps (auto-fetched from Supabase profiles if omitted)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Max retries per event for API delivery",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=15,
        help="HTTP timeout per API request in seconds",
    )
    parser.add_argument("--format", choices=["jsonl", "csv"], default="jsonl", help="Output format")
    parser.add_argument("--output", default="output/events.jsonl", help="Output file path")
    return parser.parse_args()


def main() -> None:
    load_env_file()
    args = parse_args()
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    team = build_team(salespeople=args.salespeople, managers=args.managers, bdc_agents=args.bdc)

    auth_token_for_identity = args.auth_token.strip() or os.getenv("TOPREP_AUTH_TOKEN", "")
    supabase_apikey = args.supabase_apikey.strip() or os.getenv("SUPABASE_ANON_KEY", "")
    sales_rep_id_override = args.sales_rep_id.strip() or extract_user_id_from_jwt(auth_token_for_identity)

    # Build round-robin rep pool
    sales_rep_ids: list[str] = []
    raw_ids = getattr(args, "sales_rep_ids", "").strip()
    if raw_ids:
        sales_rep_ids = [r.strip() for r in raw_ids.split(",") if r.strip()]
    elif args.delivery in {"api", "both"}:
        api_url_base = (args.api_url or "").rstrip("/").split("/functions/")[0].split("/rest/")[0]
        profiles = fetch_profiles_from_supabase(api_url_base, auth_token_for_identity, supabase_apikey)
        sales_rep_ids = [p["id"] for p in profiles if isinstance(p, dict) and p.get("id")]
        if sales_rep_ids:
            print(f"[DealMaker] Auto-fetched {len(sales_rep_ids)} rep(s) from profiles for round-robin assignment", flush=True)
    if not sales_rep_ids and sales_rep_id_override:
        sales_rep_ids = [sales_rep_id_override]

    output_path = Path(args.output)
    if args.delivery in {"file", "both"}:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    events = generate_events(
        start_date=start_date,
        days=args.days,
        daily_leads=args.daily_leads,
        team=team,
        dealership_id=args.dealership_id,
        seed=args.seed,
        sales_rep_id_override=sales_rep_id_override,
        sales_rep_ids=sales_rep_ids,
    )

    file_written = False
    if args.delivery in {"file", "both"}:
        if args.format == "jsonl":
            write_jsonl(events, output_path)
        else:
            write_csv(events, output_path)
        file_written = True

    api_result: dict[str, Any] | None = None
    if args.delivery in {"api", "both"}:
        api_url = normalize_delivery_url(args.api_url)
        auth_token = auth_token_for_identity
        supabase_apikey = args.supabase_apikey.strip() or os.getenv("SUPABASE_ANON_KEY", "")
        try:
            validate_api_settings(api_url=api_url, auth_token=auth_token, supabase_apikey=supabase_apikey)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        api_result = send_events_to_api(
            events=events,
            api_url=api_url,
            auth_token=auth_token,
            supabase_apikey=supabase_apikey,
            timeout_seconds=args.request_timeout_seconds,
            max_retries=max(0, args.max_retries),
        )

    print(
        json.dumps(
            {
                "events_written": len(events),
                "output": str(output_path) if file_written else None,
                "format": args.format,
                "delivery": args.delivery,
                "start_date": args.start_date,
                "days": args.days,
                "daily_leads": args.daily_leads,
                "seed": args.seed,
                "event_types": [
                    "deal.created",
                    "deal.status_changed",
                    "activity.scheduled",
                    "activity.completed",
                    "rep_quota_updated",
                ],
                "api_result": api_result,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
