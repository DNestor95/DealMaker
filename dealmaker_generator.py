from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import re
import ssl
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib import error, request

from dealmaker_postgres import database_url_from_env, insert_events, is_postgres_dsn


def _ssl_ctx() -> ssl.SSLContext:
    """SSL context that trusts certifi's CA bundle when available (fixes macOS urllib)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


AUTH_ERROR_401 = "Authentication failed (HTTP 401) — check TOPREP_AUTH_TOKEN."

STATUS_VALUES = ["lead", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
ACTIVITY_TYPES = [
    "call", "email", "text", "voicemail", "meeting", "appointment",
    "test_drive", "demo", "note", "follow_up",
    # §4.3 recommended types for full dealership coverage
    "trade_appraisal", "credit_app", "pencil_presented", "manager_to",
    "delivery", "walk_in", "lost_reason",
]
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
DEAL_SOURCES = ["internet", "phone", "showroom"]

# Richer raw-source strings mapped to canonical sources (for audit trail)
RAW_SOURCES: dict[str, list[str]] = {
    "internet": [
        "CarsDotCom_TradeIn", "TrueCar", "OEM_Referral", "Website_Form",
        "AutoTrader", "Facebook_Ad", "Google_PPC", "Carfax",
    ],
    "phone": [
        "inbound_call", "outbound_prospecting", "service_to_sales", "referral_call",
    ],
    "showroom": [
        "walk_in", "referral", "service_drive", "repeat_customer", "be_back",
    ],
}

# Constrains which outcomes each activity type can realistically produce
ACTIVITY_OUTCOME_MAP: dict[str, list[str]] = {
    "call": ["connected", "no_answer", "left_vm"],
    "email": ["connected", "follow_up"],
    "text": ["connected", "follow_up"],
    "voicemail": ["left_vm"],
    "meeting": ["showed", "connected"],
    "appointment": ["appt_set", "showed", "no_show"],
    "test_drive": ["showed"],
    "demo": ["connected", "showed"],
    "note": ["follow_up"],
    "follow_up": ["follow_up", "connected"],
    "trade_appraisal": ["connected", "follow_up"],
    "credit_app": ["connected", "follow_up"],
    "pencil_presented": ["negotiating", "follow_up"],
    "manager_to": ["negotiating", "connected"],
    "delivery": ["sold"],
    "walk_in": ["connected", "showed"],
    "lost_reason": ["lost"],
}

# Activity types appropriate for each deal-lifecycle stage
_STAGE_ACTIVITY_TYPES: dict[str, list[str]] = {
    "contact": ["call", "email", "text", "voicemail"],
    "appointment_set": ["call", "email", "text", "appointment"],
    "appointment_show": ["meeting", "test_drive", "demo", "walk_in"],
    "negotiation": [
        "meeting", "call", "note", "pencil_presented",
        "manager_to", "trade_appraisal", "credit_app",
    ],
    "follow_up": ["call", "email", "text", "follow_up", "note"],
}

# Archetype-aware speed-to-lead response time ranges (minutes)
_RESPONSE_TIME_RANGES: dict[str, tuple[int, int]] = {
    "rockstar": (5, 15),
    "solid_mid": (10, 30),
    "underperformer": (30, 120),
    "new_hire": (15, 45),
}

# ---------------------------------------------------------------------------
# TopRep API contract — source of truth: REALTIME_DATA_INGEST_REFERENCE.md
# ---------------------------------------------------------------------------

ALLOWED_EVENT_TYPES: list[str] = [
    "deal.created",
    "deal.status_changed",
    "deal.reassigned",
    "activity.scheduled",
    "activity.completed",
    "rep_quota_updated",
]

# Required payload keys per event type (optional keys are not listed)
REQUIRED_PAYLOAD_KEYS: dict[str, list[str]] = {
    "deal.created":        ["deal_id", "customer_name", "deal_amount", "source"],
    "deal.status_changed": ["deal_id", "old_status", "new_status"],
    "deal.reassigned":     ["deal_id", "from_rep_id", "to_rep_id"],
    "activity.scheduled":  ["activity_id", "activity_type", "scheduled_for"],
    "activity.completed":  ["activity_id", "activity_type", "outcome"],
    "rep_quota_updated":   ["month", "old_quota", "new_quota"],
}

_ISO_WITH_MS_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# ---------------------------------------------------------------------------
# Rep Archetypes
# ---------------------------------------------------------------------------

@dataclass
class ArchetypeConfig:
    name: str
    close_rate_mult: float
    activity_mult: float
    pipeline_advance_rate: float


ARCHETYPES: dict[str, ArchetypeConfig] = {
    "rockstar": ArchetypeConfig(
        name="Rockstar",
        close_rate_mult=2.5,
        activity_mult=1.2,
        pipeline_advance_rate=0.97,
    ),
    "solid_mid": ArchetypeConfig(
        name="Solid Mid",
        close_rate_mult=1.0,
        activity_mult=1.0,
        pipeline_advance_rate=0.88,
    ),
    "underperformer": ArchetypeConfig(
        name="Underperformer",
        close_rate_mult=0.3,
        activity_mult=0.8,
        pipeline_advance_rate=0.65,
    ),
    "new_hire": ArchetypeConfig(
        name="New Hire (Ramping)",
        close_rate_mult=0.2,   # base; ramped at simulation time via hire_date
        activity_mult=0.7,     # base; ramped at simulation time
        pipeline_advance_rate=0.55,
    ),
}

_ARCHETYPE_ABBREV: dict[str, str] = {
    "rockstar": "rock",
    "solid_mid": "mid",
    "underperformer": "under",
    "new_hire": "new",
}


def _new_hire_mult(hire_date: date | None, sim_date: date) -> float:
    """Compute ramp multiplier for a new hire: 0.2 → 1.0 over 6 months."""
    if hire_date is None:
        return 1.0
    months = max(0, (sim_date.year - hire_date.year) * 12 + sim_date.month - hire_date.month)
    return min(1.0, 0.2 + (months / 6.0) * 0.8)


# ---------------------------------------------------------------------------
# Month-Shape Weights
# ---------------------------------------------------------------------------

_MONTH_SHAPE_WEIGHTS: dict[str, list[tuple[int, int, float]]] = {
    # (day_from, day_to_inclusive, weight)
    "realistic": [
        (1,  5,  0.6),
        (6,  15, 0.8),
        (16, 22, 1.0),
        (23, 26, 1.5),
        (27, 28, 2.2),
        (29, 31, 3.5),
    ],
    "flat": [
        (1, 31, 1.0),
    ],
    "front_loaded": [
        (1,  5,  3.0),
        (6,  10, 2.0),
        (11, 20, 1.0),
        (21, 31, 0.5),
    ],
}


def daily_weight(day_of_month: int, month_shape: str = "flat") -> float:
    """Return the relative weight for a given day-of-month under the chosen shape."""
    buckets = _MONTH_SHAPE_WEIGHTS.get(month_shape, _MONTH_SHAPE_WEIGHTS["flat"])
    for start, end, weight in buckets:
        if start <= day_of_month <= end:
            return weight
    return 1.0


# ---------------------------------------------------------------------------
# Stress Scenarios
# ---------------------------------------------------------------------------

@dataclass
class ScenarioConfig:
    """Multipliers applied on top of base store parameters for a named scenario."""
    lead_volume_mult: float = 1.0
    close_rate_mult: float = 1.0
    pipeline_advance_mult: float = 1.0
    bdc_appt_set_prob_mult: float = 1.0
    bdc_activity_volume_mult: float = 1.0
    inventory_loss_prob: float | None = None   # if set, overrides vehicle_unavailable prob
    deal_amount_floor_bump: int = 0            # added to deal_amount_min
    manager_events_enabled: bool = True
    # When a scenario stacks a simple multiplier on high_heat days
    high_heat_day_lead_mult: float = 1.0
    high_heat_day_count: int = 0              # first N days of sim get the mult


SCENARIO_REGISTRY: dict[str, ScenarioConfig] = {
    "slow_industry_month": ScenarioConfig(
        lead_volume_mult=0.65,
        close_rate_mult=0.85,
    ),
    "manager_on_vacation": ScenarioConfig(
        pipeline_advance_mult=0.80,
        manager_events_enabled=False,
    ),
    "bdc_underperforming": ScenarioConfig(
        bdc_appt_set_prob_mult=0.25,
        bdc_activity_volume_mult=0.5,
    ),
    "inventory_shortage": ScenarioConfig(
        inventory_loss_prob=0.40,
    ),
    "strong_incentive_month": ScenarioConfig(
        close_rate_mult=1.30,
        deal_amount_floor_bump=2000,
    ),
    "high_heat_weekend": ScenarioConfig(
        high_heat_day_lead_mult=2.5,
        high_heat_day_count=2,
    ),
}


def apply_scenarios(
    base: ScenarioConfig,
    scenario_keys: list[str],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> ScenarioConfig:
    """Merge scenario configs left-to-right onto base, then apply per-scenario field overrides."""
    result = ScenarioConfig(
        lead_volume_mult=base.lead_volume_mult,
        close_rate_mult=base.close_rate_mult,
        pipeline_advance_mult=base.pipeline_advance_mult,
        bdc_appt_set_prob_mult=base.bdc_appt_set_prob_mult,
        bdc_activity_volume_mult=base.bdc_activity_volume_mult,
        inventory_loss_prob=base.inventory_loss_prob,
        deal_amount_floor_bump=base.deal_amount_floor_bump,
        manager_events_enabled=base.manager_events_enabled,
        high_heat_day_lead_mult=base.high_heat_day_lead_mult,
        high_heat_day_count=base.high_heat_day_count,
    )

    for key in scenario_keys:
        sc = SCENARIO_REGISTRY.get(key)
        if sc is None:
            continue
        # Apply user-level field overrides before merging
        merged = ScenarioConfig(
            lead_volume_mult=sc.lead_volume_mult,
            close_rate_mult=sc.close_rate_mult,
            pipeline_advance_mult=sc.pipeline_advance_mult,
            bdc_appt_set_prob_mult=sc.bdc_appt_set_prob_mult,
            bdc_activity_volume_mult=sc.bdc_activity_volume_mult,
            inventory_loss_prob=sc.inventory_loss_prob,
            deal_amount_floor_bump=sc.deal_amount_floor_bump,
            manager_events_enabled=sc.manager_events_enabled,
            high_heat_day_lead_mult=sc.high_heat_day_lead_mult,
            high_heat_day_count=sc.high_heat_day_count,
        )
        if overrides and key in overrides:
            for field_name, val in overrides[key].items():
                if hasattr(merged, field_name):
                    setattr(merged, field_name, val)

        # Stack multipliers
        result.lead_volume_mult *= merged.lead_volume_mult
        result.close_rate_mult *= merged.close_rate_mult
        result.pipeline_advance_mult *= merged.pipeline_advance_mult
        result.bdc_appt_set_prob_mult *= merged.bdc_appt_set_prob_mult
        result.bdc_activity_volume_mult *= merged.bdc_activity_volume_mult
        result.deal_amount_floor_bump += merged.deal_amount_floor_bump
        if not merged.manager_events_enabled:
            result.manager_events_enabled = False
        if merged.inventory_loss_prob is not None:
            result.inventory_loss_prob = merged.inventory_loss_prob
        if merged.high_heat_day_lead_mult != 1.0:
            result.high_heat_day_lead_mult = merged.high_heat_day_lead_mult
            result.high_heat_day_count = max(result.high_heat_day_count, merged.high_heat_day_count)

    return result


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
        with request.urlopen(req, timeout=10, context=_ssl_ctx()) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return []


@dataclass
class TeamMember:
    member_id: str
    role: str
    name: str
    archetype: str = "solid_mid"       # key into ARCHETYPES
    hire_date: date | None = None       # used for New Hire ramp curve


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


def build_team(
    salespeople: int,
    managers: int,
    bdc_agents: int,
    archetype_dist: dict[str, int] | None = None,
    new_hire_dates: list[date | None] | None = None,
) -> list[TeamMember]:
    """Build a team roster.

    ``archetype_dist`` maps archetype key → count for sales reps.  When
    provided, the first N sales reps are tagged with that archetype (in
    rockstar → solid_mid → underperformer → new_hire order).  Any
    remainder receive "solid_mid".  Managers and BDC agents always receive
    "solid_mid" (they don't have a meaningful close-rate archetype).

    ``new_hire_dates`` is a list of ``date | None`` values, one per New Hire
    rep (0-indexed).  When supplied, each New Hire TeamMember gets the
    corresponding hire_date so the ramp curve is computed at simulation time.
    """
    team: list[TeamMember] = []

    # Build ordered list of archetypes for sales reps
    archetype_slots: list[str] = []
    if archetype_dist:
        for arch_key in ["rockstar", "solid_mid", "underperformer", "new_hire"]:
            archetype_slots.extend([arch_key] * archetype_dist.get(arch_key, 0))

    new_hire_idx = 0
    for i in range(1, salespeople + 1):
        arch = archetype_slots[i - 1] if i - 1 < len(archetype_slots) else "solid_mid"
        hire_dt: date | None = None
        if arch == "new_hire" and new_hire_dates:
            hire_dt = new_hire_dates[new_hire_idx] if new_hire_idx < len(new_hire_dates) else None
            new_hire_idx += 1
        team.append(TeamMember(member_id=f"S-{i:03d}", role="sales", name=f"Sales Rep {i}", archetype=arch, hire_date=hire_dt))
    for i in range(1, managers + 1):
        team.append(TeamMember(member_id=f"M-{i:03d}", role="manager", name=f"Manager {i}", archetype="solid_mid"))
    for i in range(1, bdc_agents + 1):
        team.append(TeamMember(member_id=f"B-{i:03d}", role="bdc", name=f"BDC Agent {i}", archetype="solid_mid"))
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


def _bounded_rate(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _activity_score(
    activity_type: str,
    outcome: str,
    archetype: str,
    stage: str,
    rng: random.Random,
) -> float:
    outcome_score = {
        "sold": 1.00,
        "showed": 0.85,
        "appt_set": 0.78,
        "connected": 0.62,
        "negotiating": 0.66,
        "follow_up": 0.45,
        "left_vm": 0.22,
        "no_answer": 0.12,
        "no_show": 0.10,
        "lost": 0.05,
    }.get(outcome, 0.40)
    type_adj = {
        "call": 0.02,
        "email": -0.01,
        "text": 0.01,
        "voicemail": -0.02,
        "meeting": 0.06,
        "appointment": 0.05,
        "test_drive": 0.10,
        "demo": 0.08,
        "note": 0.00,
        "follow_up": 0.00,
        "trade_appraisal": 0.06,
        "credit_app": 0.07,
        "pencil_presented": 0.08,
        "manager_to": 0.06,
        "delivery": 0.10,
        "walk_in": 0.04,
        "lost_reason": 0.00,
    }.get(activity_type, 0.0)
    archetype_adj = {
        "rockstar": 0.08,
        "solid_mid": 0.00,
        "underperformer": -0.08,
        "new_hire": -0.04,
    }.get(archetype, 0.0)
    stage_adj = {
        "contact": 0.00,
        "appointment_set": 0.03,
        "appointment_show": 0.05,
        "negotiation": 0.08,
        "follow_up": 0.01,
    }.get(stage, 0.0)
    jitter = rng.uniform(-0.05, 0.05)
    return round(_bounded_rate(outcome_score + type_adj + archetype_adj + stage_adj + jitter), 3)


def _generate_description(
    activity_type: str,
    outcome: str,
    customer_name: str,
    rng: random.Random,
) -> str:
    """Generate a realistic human-readable activity description."""
    _TYPE_VERBS: dict[str, str] = {
        "call": "Called",
        "email": "Emailed",
        "text": "Texted",
        "voicemail": "Left voicemail for",
        "meeting": "Met with",
        "appointment": "Appointment with",
        "test_drive": "Test drive with",
        "demo": "Vehicle demo for",
        "note": "Internal note on",
        "follow_up": "Follow-up with",
        "trade_appraisal": "Trade appraisal for",
        "credit_app": "Credit application for",
        "pencil_presented": "Payment worksheet presented to",
        "manager_to": "Manager T.O. with",
        "delivery": "Vehicle delivery to",
        "walk_in": "Walk-in visit from",
        "lost_reason": "Lost reason documented for",
    }
    _OUTCOME_NOTES: dict[str, list[str]] = {
        "connected": ["Discussed vehicle options.", "Customer interested, will follow up."],
        "no_answer": ["No answer, will retry.", "No pickup."],
        "left_vm": ["Left message requesting callback.", "Voicemail left."],
        "appt_set": ["Appointment scheduled.", "Confirmed visit date."],
        "showed": ["Customer arrived.", "Showed up on time."],
        "no_show": ["Customer did not show.", "No-show, rescheduling."],
        "sold": ["Deal closed.", "Completed sale."],
        "lost": ["Deal lost.", "Customer chose another option."],
        "negotiating": ["Numbers presented.", "Working the deal."],
        "follow_up": ["Needs more time.", "Will follow up next week."],
    }
    verb = _TYPE_VERBS.get(activity_type, activity_type.replace("_", " ").title())
    note = rng.choice(_OUTCOME_NOTES.get(outcome, [outcome.replace("_", " ")]))
    return f"{verb} {customer_name}. {note}"


def generate_deal_workflow(
    day: datetime,
    deal_number: int,
    team: list[TeamMember],
    dealership_id: str,
    rng: random.Random,
    sales_rep_id_override: str | None = None,
    base_close_rate: float = 0.36,
    deal_amount_min: int = 12000,
    deal_amount_max: int = 68000,
    gross_profit_min: int = 700,
    gross_profit_max: int = 6000,
    activities_min: int = 2,
    activities_max: int = 6,
    contact_rate: float | None = None,
    appointment_rate: float | None = None,
    showroom_rate: float | None = None,
    negotiation_rate: float | None = None,
    scenario: ScenarioConfig | None = None,
) -> list[Event]:
    events: list[Event] = []
    sc = scenario or ScenarioConfig()

    created_ts = random_business_time(day, rng)
    sales_member = pick_member(team, "sales", rng)
    manager_member = pick_member(team, "manager", rng)

    # --- Archetype multipliers ---
    arch = ARCHETYPES.get(sales_member.archetype, ARCHETYPES["solid_mid"])
    if sales_member.archetype == "new_hire":
        ramp = _new_hire_mult(sales_member.hire_date, day.date())
        close_rate_mult = arch.close_rate_mult + (1.0 - arch.close_rate_mult) * ramp
        activity_mult = arch.activity_mult + (1.0 - arch.activity_mult) * ramp
        pipeline_advance_rate = arch.pipeline_advance_rate + (ARCHETYPES["solid_mid"].pipeline_advance_rate - arch.pipeline_advance_rate) * ramp
    else:
        close_rate_mult = arch.close_rate_mult
        activity_mult = arch.activity_mult
        pipeline_advance_rate = arch.pipeline_advance_rate

    # Apply scenario multipliers
    effective_close_rate = _bounded_rate(base_close_rate * close_rate_mult * sc.close_rate_mult)
    effective_pipeline_rate = _bounded_rate(pipeline_advance_rate * sc.pipeline_advance_mult)

    # Stage-specific realism rates. These model business milestones while keeping
    # DB-compatible canonical statuses.
    base_contact_rate = 0.72 if contact_rate is None else contact_rate
    base_appointment_rate = 0.55 if appointment_rate is None else appointment_rate
    base_showroom_rate = 0.65 if showroom_rate is None else showroom_rate
    base_negotiation_rate = 0.80 if negotiation_rate is None else negotiation_rate

    effective_contact_rate = _bounded_rate(base_contact_rate * (0.85 + 0.30 * activity_mult) * sc.pipeline_advance_mult)
    effective_appointment_rate = _bounded_rate(base_appointment_rate * effective_pipeline_rate * sc.bdc_appt_set_prob_mult)
    effective_showroom_rate = _bounded_rate(base_showroom_rate * effective_pipeline_rate)
    effective_negotiation_rate = _bounded_rate(base_negotiation_rate * effective_pipeline_rate)

    # Activities per deal
    raw_activity_count = rng.randint(activities_min, activities_max)
    bdc_mult = sc.bdc_activity_volume_mult if sales_member.role == "bdc" else 1.0
    target_activity_count = max(1, round(raw_activity_count * activity_mult * bdc_mult))

    deal_id = stable_uuid("deal", dealership_id, day.strftime("%Y%m%d"), str(deal_number))
    customer_name = f"Customer {deal_number:05d}"

    effective_amount_min = deal_amount_min + sc.deal_amount_floor_bump
    # Ensure min < max to avoid randint errors when scenario bumps floor above configured max
    safe_amount_max = max(effective_amount_min + 1, deal_amount_max)

    source = rng.choice(DEAL_SOURCES)
    raw_source = rng.choice(RAW_SOURCES[source])

    # Allow $0 deals for early-stage internet leads (~15%)
    if source == "internet" and rng.random() < 0.15:
        deal_amount = 0
    else:
        deal_amount = rng.randint(max(1, effective_amount_min), safe_amount_max)
    gross_profit = rng.randint(gross_profit_min, max(gross_profit_min + 1, gross_profit_max))

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
                "source": source,
                "raw_source": raw_source,
                "stage": "lead",
            },
        )
    )

    current_status = "lead"
    activity_index = 1
    activity_count = 0
    # Archetype-aware speed-to-lead for first activity gap
    response_range = _RESPONSE_TIME_RANGES.get(sales_member.archetype, (10, 30))
    cursor_ts = created_ts + timedelta(minutes=rng.randint(*response_range))

    def _log_activity(stage: str, activity_type: str, outcome: str, ts: datetime) -> datetime:
        nonlocal activity_index, activity_count
        activity_id = stable_uuid("activity", deal_id, str(activity_index))
        scheduled_ts = ts + timedelta(minutes=rng.randint(5, 45))
        completed_ts = scheduled_ts + timedelta(minutes=rng.randint(5, 120))

        # Response time: minutes since deal creation
        response_minutes = int((completed_ts - created_ts).total_seconds() / 60)

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
                    "scheduled_for": to_iso(scheduled_ts + timedelta(minutes=rng.randint(5, 120))),
                },
            )
        )
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
                    "contact_quality_score": _activity_score(activity_type, outcome, sales_member.archetype, stage, rng),
                    "stage_milestone": stage,
                    "response_time_minutes": response_minutes,
                    "follow_up_sequence": activity_index,
                    "completed_at": to_iso(completed_ts),
                    "description": _generate_description(activity_type, outcome, customer_name, rng),
                },
            )
        )
        activity_index += 1
        activity_count += 1
        return completed_ts

    # Lead -> Contact milestone (mapped to status lead -> qualified)
    contact_success = False
    for _ in range(1 + (1 if rng.random() < 0.35 else 0)):
        contact_type = rng.choice(_STAGE_ACTIVITY_TYPES["contact"])
        if rng.random() < effective_contact_rate:
            contact_outcome = "connected"
        else:
            # Pick a non-success outcome valid for the chosen activity type
            valid_outcomes = [o for o in ACTIVITY_OUTCOME_MAP.get(contact_type, ["no_answer"]) if o not in ("connected",)]
            contact_outcome = rng.choice(valid_outcomes) if valid_outcomes else "no_answer"
        cursor_ts = _log_activity("contact", contact_type, contact_outcome, cursor_ts)
        if contact_outcome in {"connected", "appt_set"}:
            contact_success = True
            break

    if contact_success:
        next_status_ts = cursor_ts + timedelta(minutes=rng.randint(5, 45))
        events.append(
            make_event(
                ts=next_status_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="deal.status_changed",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "deal_id": deal_id,
                    "old_status": current_status,
                    "new_status": "qualified",
                    "reason": "contact_established",
                },
            )
        )
        current_status = "qualified"
        cursor_ts = next_status_ts

    # Contact -> Appointment milestone (mapped to status qualified -> proposal)
    appointment_set = False
    if contact_success:
        appt_type = rng.choice(_STAGE_ACTIVITY_TYPES["appointment_set"])
        if rng.random() < effective_appointment_rate * 0.45:
            appt_outcome = "appt_set"
        else:
            appt_outcome = "follow_up"
        cursor_ts = _log_activity("appointment_set", appt_type, appt_outcome, cursor_ts)
        appointment_set = appt_outcome == "appt_set"

        if not appointment_set and rng.random() < effective_appointment_rate:
            appt_type = rng.choice(_STAGE_ACTIVITY_TYPES["appointment_set"])
            cursor_ts = _log_activity("appointment_set", appt_type, "appt_set", cursor_ts)
            appointment_set = True

    if appointment_set and current_status == "qualified":
        next_status_ts = cursor_ts + timedelta(minutes=rng.randint(5, 45))
        events.append(
            make_event(
                ts=next_status_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="deal.status_changed",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "deal_id": deal_id,
                    "old_status": current_status,
                    "new_status": "proposal",
                    "reason": "appointment_set",
                },
            )
        )
        current_status = "proposal"
        cursor_ts = next_status_ts

    # Appointment -> Showroom milestone (mapped to status proposal -> negotiation)
    showroom_visit = False
    if appointment_set:
        showed = rng.random() < effective_showroom_rate
        showroom_outcome = "showed" if showed else "no_show"
        showroom_type = rng.choice(_STAGE_ACTIVITY_TYPES["appointment_show"])
        cursor_ts = _log_activity("appointment_show", showroom_type, showroom_outcome, cursor_ts)
        showroom_visit = showed

    if showroom_visit and current_status == "proposal":
        next_status_ts = cursor_ts + timedelta(minutes=rng.randint(10, 60))
        events.append(
            make_event(
                ts=next_status_ts,
                dealership_id=dealership_id,
                member=sales_member,
                event_type="deal.status_changed",
                sales_rep_id_override=sales_rep_id_override,
                payload={
                    "deal_id": deal_id,
                    "old_status": current_status,
                    "new_status": "negotiation",
                    "reason": "showroom_visit",
                },
            )
        )
        current_status = "negotiation"
        cursor_ts = next_status_ts

    # Negotiation actions before closing
    if current_status == "negotiation" and rng.random() < effective_negotiation_rate:
        neg_type = rng.choice(_STAGE_ACTIVITY_TYPES["negotiation"])
        neg_outcome = rng.choice(ACTIVITY_OUTCOME_MAP.get(neg_type, ["negotiating"]))
        cursor_ts = _log_activity("negotiation", neg_type, neg_outcome, cursor_ts)

    # Pad to target activity volume so rep activity cadence remains realistic.
    while activity_count < target_activity_count:
        pad_type = rng.choice(_STAGE_ACTIVITY_TYPES["follow_up"])
        pad_outcome = rng.choice(ACTIVITY_OUTCOME_MAP.get(pad_type, ["follow_up"]))
        cursor_ts = _log_activity(
            "follow_up",
            pad_type,
            pad_outcome,
            cursor_ts,
        )

    if current_status == "negotiation":
        win_prob = effective_close_rate
    elif appointment_set:
        win_prob = effective_close_rate * 0.55
    elif contact_success:
        win_prob = effective_close_rate * 0.25
    else:
        win_prob = effective_close_rate * 0.08

    close_won = rng.random() < _bounded_rate(win_prob)
    close_ts = cursor_ts + timedelta(minutes=rng.randint(30, 240))
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
                    "deal_amount": deal_amount,
                    "gross_profit": gross_profit,
                    "close_date": close_ts.strftime("%Y-%m-%d"),
                },
            )
        )
        # Post-sale: delivery activity
        delivery_ts = close_ts + timedelta(minutes=rng.randint(60, 480))
        _log_activity("follow_up", "delivery", "sold", delivery_ts)
    else:
        base_reasons = ["price", "timing", "credit", "vehicle_unavailable", "no_response"]
        if not contact_success:
            base_reasons = ["no_response", "timing", "credit"]
        elif contact_success and not appointment_set:
            base_reasons = ["no_response", "timing", "price"]
        elif appointment_set and not showroom_visit:
            base_reasons = ["no_show", "timing", "price"]
        if sc.inventory_loss_prob is not None and rng.random() < sc.inventory_loss_prob:
            reason = "vehicle_unavailable"
        else:
            reason = rng.choice(base_reasons)
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
        # Post-loss: document lost reason
        lost_ts = close_ts + timedelta(minutes=rng.randint(5, 60))
        _log_activity("follow_up", "lost_reason", "lost", lost_ts)

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
    base_close_rate: float = 0.36,
    deal_amount_min: int = 12000,
    deal_amount_max: int = 68000,
    gross_profit_min: int = 700,
    gross_profit_max: int = 6000,
    activities_min: int = 2,
    activities_max: int = 6,
    contact_rate: float | None = None,
    appointment_rate: float | None = None,
    showroom_rate: float | None = None,
    negotiation_rate: float | None = None,
    month_shape: str = "flat",
    scenarios: list[str] | None = None,
    scenario_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[Event]:
    rng = random.Random(seed)
    events: list[Event] = []
    deal_counter = 1

    # Resolve scenario config
    base_sc = ScenarioConfig()
    sc = apply_scenarios(base_sc, scenarios or [], scenario_overrides)

    # Effective daily_leads after slow/fast scenario
    effective_daily_leads = daily_leads * sc.lead_volume_mult

    # Build rep rotation pool: explicit list > single override > generated UUIDs
    rep_pool: list[str] | None = None
    if sales_rep_ids:
        rep_pool = sales_rep_ids
    elif sales_rep_id_override:
        rep_pool = [sales_rep_id_override]

    # Determine all rep UUIDs for per-rep quota emission
    quota_rep_ids: list[str] = []
    if rep_pool:
        quota_rep_ids = list(rep_pool)
    else:
        quota_rep_ids = [sales_rep_uuid(dealership_id, m) for m in team if m.role == "sales"]

    # Pre-compute per-day weights for the month-shape distribution
    # We normalise so total_weighted_days * normalised_weight == days
    day_weights: list[float] = []
    for day_offset in range(days):
        day = start_date + timedelta(days=day_offset)
        w = daily_weight(day.day, month_shape)
        # High-heat weekend: first N days get extra leads
        if sc.high_heat_day_count > 0 and day_offset < sc.high_heat_day_count:
            w *= sc.high_heat_day_lead_mult
        day_weights.append(w)

    total_weight = sum(day_weights) or 1.0
    total_leads_target = effective_daily_leads * days

    # Track which months have had quota events emitted
    months_with_quota: set[str] = set()

    for day_offset in range(days):
        day = start_date + timedelta(days=day_offset)
        month_key = day.strftime("%Y-%m")

        # Emit per-rep quota events at the start of each new month
        if month_key not in months_with_quota:
            months_with_quota.add(month_key)
            for rep_id in quota_rep_ids:
                quota = rng.randint(8, 20)
                old_quota = max(0, quota + rng.randint(-3, 0))
                events.append(
                    Event(
                        sales_rep_id=rep_id,
                        type="rep_quota_updated",
                        payload={
                            "month": month_key,
                            "old_quota": old_quota,
                            "new_quota": quota,
                            "reason": rng.choice(["seasonality", "management_adjustment", "performance_retarget"]),
                        },
                        created_at=to_iso(day.replace(hour=8, minute=0, second=0, microsecond=0)),
                    )
                )

        # Scale daily leads by shape weight
        scaled_mean = total_leads_target * day_weights[day_offset] / total_weight
        leads_today = max(1, int(rng.gauss(scaled_mean, max(2.0, scaled_mean * 0.25))))
        for _ in range(leads_today):
            # Round-robin: each new deal goes to the next rep in the pool
            assigned = rep_pool[(deal_counter - 1) % len(rep_pool)] if rep_pool else None
            deal_events = generate_deal_workflow(
                day=day,
                deal_number=deal_counter,
                team=team,
                dealership_id=dealership_id,
                rng=rng,
                sales_rep_id_override=assigned,
                base_close_rate=base_close_rate,
                deal_amount_min=deal_amount_min,
                deal_amount_max=deal_amount_max,
                gross_profit_min=gross_profit_min,
                gross_profit_max=gross_profit_max,
                activities_min=activities_min,
                activities_max=activities_max,
                contact_rate=contact_rate,
                appointment_rate=appointment_rate,
                showroom_rate=showroom_rate,
                negotiation_rate=negotiation_rate,
                scenario=sc,
            )
            events.extend(deal_events)

            # Occasional deal reassignment (~5%) when multiple reps available
            if rep_pool and len(rep_pool) > 1 and rng.random() < 0.05:
                deal_id = deal_events[0].payload.get("deal_id")
                if deal_id and assigned:
                    to_rep = rng.choice([r for r in rep_pool if r != assigned])
                    reassign_ts = day.replace(
                        hour=rng.randint(8, 18),
                        minute=rng.randint(0, 59),
                        second=0,
                        microsecond=0,
                    )
                    events.append(
                        Event(
                            sales_rep_id=to_rep,
                            type="deal.reassigned",
                            payload={
                                "deal_id": deal_id,
                                "from_rep_id": assigned,
                                "to_rep_id": to_rep,
                                "reason": rng.choice(["round_robin", "manual_reassignment", "coverage"]),
                            },
                            created_at=to_iso(reassign_ts),
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
        with request.urlopen(req, timeout=timeout_seconds, context=_ssl_ctx()) as response:
            status_ok = HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES
            return status_ok, f"status={response.status}"
    except error.HTTPError as exc:
        if exc.code == 401:
            return False, AUTH_ERROR_401
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"http_error={exc.code} body={detail}"
    except error.URLError as exc:
        return False, f"url_error={exc.reason}"


_EVENT_TYPE_TO_ACTIVITY_TYPE: dict[str, str] = {
    "deal.created": "note",
    "deal.status_changed": "note",
    "deal.reassigned": "note",
    "activity.scheduled": "",   # use payload.activity_type
    "activity.completed": "",   # use payload.activity_type
    "rep_quota_updated": "note",
}


def event_to_action(event: Event) -> dict[str, Any]:
    payload = event.payload

    # Use payload's activity_type for activity events; fall back to mapped value
    mapped = _EVENT_TYPE_TO_ACTIVITY_TYPE.get(event.type, "note")
    activity_type = payload.get("activity_type") or mapped or "note"

    # Prefer payload description (enriched); fall back to generated description
    description = payload.get("description")
    if not description:
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

    # Include analytics fields when present
    if payload.get("scheduled_at"):
        row["scheduled_at"] = payload["scheduled_at"]
    if payload.get("completed_at"):
        row["completed_at"] = payload["completed_at"]
    if payload.get("outcome"):
        row["outcome"] = payload["outcome"]
    if payload.get("contact_quality_score") is not None:
        row["contact_quality_score"] = payload["contact_quality_score"]
    if payload.get("response_time_minutes") is not None:
        row["response_time_minutes"] = payload["response_time_minutes"]
    if payload.get("follow_up_sequence") is not None:
        row["follow_up_sequence"] = payload["follow_up_sequence"]

    return row


def events_to_deals(events: list[Event]) -> list[dict[str, Any]]:
    """Build deal upsert rows from deal.created, deal.status_changed, and deal.reassigned events."""
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
            new_status = p.get("new_status", deals[deal_id]["status"])
            deals[deal_id]["status"] = new_status
            deals[deal_id]["updated_at"] = event.created_at
            if new_status == "closed_won":
                # Pick up final financials from close payload
                if p.get("deal_amount") is not None:
                    deals[deal_id]["deal_amount"] = p["deal_amount"]
                if p.get("gross_profit") is not None:
                    deals[deal_id]["gross_profit"] = p["gross_profit"]
                deals[deal_id]["close_date"] = p.get("close_date") or event.created_at[:10]
            elif new_status == "closed_lost":
                deals[deal_id]["close_date"] = event.created_at[:10]
        elif event.type == "deal.reassigned" and deal_id in deals:
            deals[deal_id]["sales_rep_id"] = event.sales_rep_id
            deals[deal_id]["updated_at"] = event.created_at

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
        with request.urlopen(req, timeout=timeout_seconds, context=_ssl_ctx()) as response:
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
        if exc.code == 401:
            return False, AUTH_ERROR_401, 0
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"http_error={exc.code} body={detail}", 0
    except error.URLError as exc:
        return False, f"url_error={exc.reason}", 0


def post_events_batch_to_rest(
    events: list[Event],
    api_url: str,
    auth_token: str,
    supabase_apikey: str = "",
    timeout_seconds: int = 15,
) -> tuple[bool, str, int]:
    payload = json.dumps([event.to_dict() for event in events], separators=(",", ":")).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "Prefer": "return=minimal",
    }
    if supabase_apikey:
        headers["apikey"] = supabase_apikey

    req = request.Request(api_url, data=payload, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout_seconds, context=_ssl_ctx()) as response:
            status_ok = HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES
            return status_ok, f"status={response.status}", len(events)
    except error.HTTPError as exc:
        if exc.code == 401:
            return False, AUTH_ERROR_401, 0
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
    if is_postgres_dsn(api_url):
        inserted, errors = insert_events(api_url, [event.to_dict() for event in events])
        return {"sent": inserted, "failed": max(0, len(events) - inserted), "errors": errors[:10]}

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

    if "/rest/v1/" in api_url:
        delivered = False
        last_error = ""
        inserted = 0
        for attempt in range(max_retries + 1):
            ok, detail, inserted_count = post_events_batch_to_rest(
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
            if last_error == AUTH_ERROR_401:
                break
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
            # 401 means credentials are wrong/expired; don't burn retries for
            # this event or continue with the rest of the batch.
            if last_error == AUTH_ERROR_401:
                break
            if attempt < max_retries:
                time.sleep(0.5 * (2**attempt))

        if not delivered:
            failed += 1
            if len(errors) < 10:
                errors.append(last_error)
            # Stop the batch immediately on auth failures so callers can surface
            # actionable errors without waiting through every event retry cycle.
            if last_error == AUTH_ERROR_401:
                failed += max(0, len(events) - sent - failed)
                break

    return {"sent": sent, "failed": failed, "errors": errors}


def validate_event(event: Event) -> list[str]:
    """Validate a single event against the TopRep API contract.

    Returns a (possibly empty) list of human-readable error strings.
    An empty list means the event is contract-compliant.
    """
    errors: list[str] = []

    # --- Envelope ---
    if not event.sales_rep_id:
        errors.append("missing sales_rep_id")
    else:
        try:
            uuid.UUID(event.sales_rep_id)
        except ValueError:
            errors.append(f"sales_rep_id is not a valid UUID: {event.sales_rep_id!r}")

    if event.type not in ALLOWED_EVENT_TYPES:
        errors.append(f"unknown event type: {event.type!r}; allowed={ALLOWED_EVENT_TYPES}")

    if not event.created_at:
        errors.append("missing created_at")
    elif not _ISO_WITH_MS_Z_RE.match(event.created_at):
        errors.append(
            f"created_at must be UTC ISO-8601 with milliseconds and Z suffix "
            f"(e.g. 2026-03-03T15:04:05.000Z); got {event.created_at!r}"
        )

    # --- Payload keys ---
    required_keys = REQUIRED_PAYLOAD_KEYS.get(event.type, [])
    for key in required_keys:
        if key not in event.payload:
            errors.append(f"payload missing required key {key!r} for type {event.type!r}")

    # --- Enum values ---
    if event.type in ("activity.scheduled", "activity.completed"):
        at = event.payload.get("activity_type")
        if at and at not in ACTIVITY_TYPES:
            errors.append(
                f"activity_type {at!r} not in allowed values {ACTIVITY_TYPES}"
            )

    if event.type == "activity.completed":
        outcome = event.payload.get("outcome")
        if outcome and outcome not in ACTIVITY_OUTCOMES:
            errors.append(
                f"outcome {outcome!r} not in allowed values {ACTIVITY_OUTCOMES}"
            )

    if event.type == "deal.status_changed":
        for field_name in ("old_status", "new_status"):
            val = event.payload.get(field_name)
            if val and val not in STATUS_VALUES:
                errors.append(
                    f"{field_name} {val!r} not in allowed values {STATUS_VALUES}"
                )

    return errors


def validate_events(events: list[Event]) -> dict[str, Any]:
    """Validate all events and return a summary dict.

    Keys:
        total      – total event count
        valid      – count of events with zero errors
        invalid    – count of events with at least one error
        errors     – list of dicts {index, type, errors} for invalid events (capped at 20)
        passed     – True when invalid == 0
    """
    total = len(events)
    valid = 0
    invalid = 0
    error_samples: list[dict[str, Any]] = []

    for idx, event in enumerate(events):
        errs = validate_event(event)
        if errs:
            invalid += 1
            if len(error_samples) < 20:
                error_samples.append({"index": idx, "type": event.type, "errors": errs})
        else:
            valid += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "errors": error_samples,
        "passed": invalid == 0,
    }


def validate_api_settings(api_url: str, auth_token: str, supabase_apikey: str = "") -> None:
    if not api_url:
        raise ValueError("API URL is required for API delivery")
    if is_postgres_dsn(api_url):
        return
    if not (api_url.startswith("http://") or api_url.startswith("https://")):
        raise ValueError("API URL must start with http://, https://, or use a postgres:// DSN")
    if "/api/events" not in api_url and "/rest/v1/" not in api_url and "/functions/v1/" not in api_url and ".supabase.co" not in api_url:
        raise ValueError("API URL must be TOP REP /api/events, Supabase /rest/v1/*, Supabase /functions/v1/*, or a postgres:// DSN")

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
    if is_postgres_dsn(trimmed):
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
        default=os.getenv("TOPREP_API_URL", "") or database_url_from_env(),
        help="TOP REP ingest endpoint or postgres:// connection string",
    )
    parser.add_argument(
        "--auth-token",
        default="",
        help="Bearer token for API writes (or set TOPREP_AUTH_TOKEN env var)",
    )
    parser.add_argument(
        "--supabase-apikey",
        default=os.getenv("SUPABASE_ANON_KEY", ""),
        help="Supabase anon/publishable key (required for direct Supabase REST writes only)",
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
    parser.add_argument(
        "--validate",
        action="store_true",
        default=False,
        help="After generating events, validate each event against the TopRep API contract and print a compliance report",
    )
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
    elif args.delivery in {"api", "both"} and not is_postgres_dsn(args.api_url or ""):
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

    schema_report: dict[str, Any] | None = None
    if args.validate:
        schema_report = validate_events(events)

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
                "schema_validation": schema_report,
            },
            indent=2,
        )
    )

    if schema_report and not schema_report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
