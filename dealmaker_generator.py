from __future__ import annotations

import argparse
import csv
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ACTIONS = {
    "lead_created": {"entity": "lead", "channel": "system"},
    "lead_assigned": {"entity": "lead", "channel": "system"},
    "lead_scored": {"entity": "lead", "channel": "system"},
    "call_logged": {"entity": "activity", "channel": "phone"},
    "email_sent": {"entity": "activity", "channel": "email"},
    "sms_sent": {"entity": "activity", "channel": "sms"},
    "inbound_response": {"entity": "activity", "channel": "phone"},
    "appointment_set": {"entity": "appointment", "channel": "phone"},
    "appointment_confirmed": {"entity": "appointment", "channel": "sms"},
    "appointment_completed": {"entity": "appointment", "channel": "in_person"},
    "test_drive_completed": {"entity": "opportunity", "channel": "in_person"},
    "trade_in_appraised": {"entity": "opportunity", "channel": "in_person"},
    "credit_app_submitted": {"entity": "finance", "channel": "in_person"},
    "quote_presented": {"entity": "deal", "channel": "in_person"},
    "manager_approval_requested": {"entity": "deal", "channel": "system"},
    "manager_approval_granted": {"entity": "deal", "channel": "system"},
    "follow_up_task_created": {"entity": "task", "channel": "system"},
    "note_added": {"entity": "note", "channel": "system"},
    "status_changed": {"entity": "lead", "channel": "system"},
    "deal_closed_won": {"entity": "deal", "channel": "in_person"},
    "deal_closed_lost": {"entity": "deal", "channel": "system"},
}


@dataclass
class TeamMember:
    member_id: str
    role: str
    name: str


@dataclass
class Event:
    event_id: str
    event_ts: str
    source_system: str
    dealership_id: str
    team_member_id: str
    team_member_role: str
    action: str
    entity: str
    entity_id: str
    lead_id: str
    opportunity_id: str
    customer_id: str
    channel: str
    result: str
    value: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_ts": self.event_ts,
            "source_system": self.source_system,
            "dealership_id": self.dealership_id,
            "team_member_id": self.team_member_id,
            "team_member_role": self.team_member_role,
            "action": self.action,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "lead_id": self.lead_id,
            "opportunity_id": self.opportunity_id,
            "customer_id": self.customer_id,
            "channel": self.channel,
            "result": self.result,
            "value": self.value,
            "metadata": self.metadata,
        }


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
    eligible = [m for m in team if m.role == role]
    if not eligible:
        eligible = team
    return rng.choice(eligible)


def new_event(
    ts: datetime,
    member: TeamMember,
    dealership_id: str,
    action: str,
    entity_id: str,
    lead_id: str,
    opportunity_id: str,
    customer_id: str,
    result: str,
    value: float,
    metadata: dict[str, Any] | None = None,
) -> Event:
    action_info = ACTIONS[action]
    return Event(
        event_id=str(uuid.uuid4()),
        event_ts=to_iso(ts),
        source_system="dealmaker",
        dealership_id=dealership_id,
        team_member_id=member.member_id,
        team_member_role=member.role,
        action=action,
        entity=action_info["entity"],
        entity_id=entity_id,
        lead_id=lead_id,
        opportunity_id=opportunity_id,
        customer_id=customer_id,
        channel=action_info["channel"],
        result=result,
        value=value,
        metadata=metadata or {},
    )


def generate_lead_workflow(
    day: datetime,
    lead_number: int,
    team: list[TeamMember],
    dealership_id: str,
    rng: random.Random,
) -> list[Event]:
    events: list[Event] = []

    created_at = random_business_time(day, rng)
    lead_id = f"L-{day:%Y%m%d}-{lead_number:05d}"
    customer_id = f"C-{day:%Y%m%d}-{lead_number:05d}"
    opportunity_id = f"O-{day:%Y%m%d}-{lead_number:05d}"
    deal_id = f"D-{day:%Y%m%d}-{lead_number:05d}"

    bdc_member = pick_member(team, "bdc", rng)
    sales_member = pick_member(team, "sales", rng)
    manager_member = pick_member(team, "manager", rng)

    events.append(
        new_event(
            ts=created_at,
            member=bdc_member,
            dealership_id=dealership_id,
            action="lead_created",
            entity_id=lead_id,
            lead_id=lead_id,
            opportunity_id=opportunity_id,
            customer_id=customer_id,
            result="success",
            value=0.0,
            metadata={"lead_source": rng.choice(["web", "third_party", "walk_in", "phone"]), "priority": rng.choice(["low", "medium", "high"]), "store": dealership_id},
        )
    )

    events.append(
        new_event(
            ts=created_at + timedelta(minutes=rng.randint(2, 25)),
            member=bdc_member,
            dealership_id=dealership_id,
            action="lead_scored",
            entity_id=lead_id,
            lead_id=lead_id,
            opportunity_id=opportunity_id,
            customer_id=customer_id,
            result="success",
            value=float(rng.randint(1, 100)),
            metadata={"score_model": "v1", "score": rng.randint(1, 100)},
        )
    )

    events.append(
        new_event(
            ts=created_at + timedelta(minutes=rng.randint(10, 45)),
            member=manager_member,
            dealership_id=dealership_id,
            action="lead_assigned",
            entity_id=lead_id,
            lead_id=lead_id,
            opportunity_id=opportunity_id,
            customer_id=customer_id,
            result="success",
            value=0.0,
            metadata={"assigned_to": sales_member.member_id},
        )
    )

    contact_time = created_at + timedelta(minutes=rng.randint(20, 180))
    attempts = rng.randint(1, 4)
    for attempt in range(1, attempts + 1):
        action = rng.choice(["call_logged", "email_sent", "sms_sent"])
        contact_time += timedelta(minutes=rng.randint(15, 120))
        events.append(
            new_event(
                ts=contact_time,
                member=sales_member,
                dealership_id=dealership_id,
                action=action,
                entity_id=lead_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result=rng.choice(["connected", "left_message", "no_answer"]),
                value=0.0,
                metadata={"attempt": attempt},
            )
        )

    responded = rng.random() < 0.62
    if responded:
        events.append(
            new_event(
                ts=contact_time + timedelta(minutes=rng.randint(5, 60)),
                member=sales_member,
                dealership_id=dealership_id,
                action="inbound_response",
                entity_id=lead_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="engaged",
                value=0.0,
                metadata={"response_time_minutes": rng.randint(1, 240)},
            )
        )
    else:
        events.append(
            new_event(
                ts=contact_time + timedelta(hours=2),
                member=sales_member,
                dealership_id=dealership_id,
                action="follow_up_task_created",
                entity_id=lead_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="open",
                value=0.0,
                metadata={"task": "retry_contact", "due_in_hours": 24},
            )
        )
        events.append(
            new_event(
                ts=contact_time + timedelta(days=2),
                member=sales_member,
                dealership_id=dealership_id,
                action="deal_closed_lost",
                entity_id=deal_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="lost_no_response",
                value=0.0,
                metadata={"reason": "unreachable"},
            )
        )
        return events

    appointment_set = rng.random() < 0.55
    if appointment_set:
        appt_time = contact_time + timedelta(hours=rng.randint(4, 48))
        events.append(
            new_event(
                ts=appt_time - timedelta(hours=rng.randint(1, 6)),
                member=sales_member,
                dealership_id=dealership_id,
                action="appointment_set",
                entity_id=opportunity_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="scheduled",
                value=0.0,
                metadata={"appointment_time": to_iso(appt_time)},
            )
        )

        if rng.random() < 0.8:
            events.append(
                new_event(
                    ts=appt_time - timedelta(hours=1),
                    member=bdc_member,
                    dealership_id=dealership_id,
                    action="appointment_confirmed",
                    entity_id=opportunity_id,
                    lead_id=lead_id,
                    opportunity_id=opportunity_id,
                    customer_id=customer_id,
                    result="confirmed",
                    value=0.0,
                    metadata={"confirmation_method": "sms"},
                )
            )

        if rng.random() < 0.72:
            events.append(
                new_event(
                    ts=appt_time,
                    member=sales_member,
                    dealership_id=dealership_id,
                    action="appointment_completed",
                    entity_id=opportunity_id,
                    lead_id=lead_id,
                    opportunity_id=opportunity_id,
                    customer_id=customer_id,
                    result="show",
                    value=0.0,
                    metadata={},
                )
            )
            if rng.random() < 0.6:
                events.append(
                    new_event(
                        ts=appt_time + timedelta(minutes=rng.randint(15, 60)),
                        member=sales_member,
                        dealership_id=dealership_id,
                        action="test_drive_completed",
                        entity_id=opportunity_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result="completed",
                        value=0.0,
                        metadata={"vehicle_class": rng.choice(["new", "used", "cpo"])},
                    )
                )
            if rng.random() < 0.35:
                events.append(
                    new_event(
                        ts=appt_time + timedelta(minutes=rng.randint(45, 120)),
                        member=sales_member,
                        dealership_id=dealership_id,
                        action="trade_in_appraised",
                        entity_id=opportunity_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result="appraised",
                        value=float(rng.randint(3000, 25000)),
                        metadata={"condition": rng.choice(["fair", "good", "excellent"])},
                    )
                )
            if rng.random() < 0.58:
                events.append(
                    new_event(
                        ts=appt_time + timedelta(minutes=rng.randint(60, 180)),
                        member=sales_member,
                        dealership_id=dealership_id,
                        action="credit_app_submitted",
                        entity_id=opportunity_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result="submitted",
                        value=0.0,
                        metadata={"lender_count": rng.randint(1, 4)},
                    )
                )

            if rng.random() < 0.75:
                gross = float(rng.randint(800, 5000))
                events.append(
                    new_event(
                        ts=appt_time + timedelta(minutes=rng.randint(90, 210)),
                        member=sales_member,
                        dealership_id=dealership_id,
                        action="quote_presented",
                        entity_id=deal_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result="presented",
                        value=gross,
                        metadata={"front_gross": gross, "payment_term": rng.choice([48, 60, 72, 84])},
                    )
                )
                events.append(
                    new_event(
                        ts=appt_time + timedelta(minutes=rng.randint(100, 240)),
                        member=sales_member,
                        dealership_id=dealership_id,
                        action="manager_approval_requested",
                        entity_id=deal_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result="requested",
                        value=gross,
                        metadata={"discount_request": rng.randint(0, 1500)},
                    )
                )
                events.append(
                    new_event(
                        ts=appt_time + timedelta(minutes=rng.randint(101, 241)),
                        member=manager_member,
                        dealership_id=dealership_id,
                        action="manager_approval_granted",
                        entity_id=deal_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result=rng.choice(["approved", "approved_with_changes"]),
                        value=gross,
                        metadata={},
                    )
                )

                if rng.random() < 0.38:
                    sale_price = float(rng.randint(18000, 65000))
                    events.append(
                        new_event(
                            ts=appt_time + timedelta(minutes=rng.randint(150, 300)),
                            member=sales_member,
                            dealership_id=dealership_id,
                            action="deal_closed_won",
                            entity_id=deal_id,
                            lead_id=lead_id,
                            opportunity_id=opportunity_id,
                            customer_id=customer_id,
                            result="sold",
                            value=sale_price,
                            metadata={"sale_price": sale_price, "f_and_i_products": rng.randint(0, 4)},
                        )
                    )
                else:
                    events.append(
                        new_event(
                            ts=appt_time + timedelta(days=rng.randint(1, 4)),
                            member=sales_member,
                            dealership_id=dealership_id,
                            action="deal_closed_lost",
                            entity_id=deal_id,
                            lead_id=lead_id,
                            opportunity_id=opportunity_id,
                            customer_id=customer_id,
                            result=rng.choice(["lost_price", "lost_vehicle", "lost_finance"]),
                            value=0.0,
                            metadata={},
                        )
                    )
            else:
                events.append(
                    new_event(
                        ts=appt_time + timedelta(days=1),
                        member=sales_member,
                        dealership_id=dealership_id,
                        action="deal_closed_lost",
                        entity_id=deal_id,
                        lead_id=lead_id,
                        opportunity_id=opportunity_id,
                        customer_id=customer_id,
                        result="lost_after_visit",
                        value=0.0,
                        metadata={},
                    )
                )
        else:
            events.append(
                new_event(
                    ts=appt_time + timedelta(hours=1),
                    member=sales_member,
                    dealership_id=dealership_id,
                    action="status_changed",
                    entity_id=lead_id,
                    lead_id=lead_id,
                    opportunity_id=opportunity_id,
                    customer_id=customer_id,
                    result="no_show",
                    value=0.0,
                    metadata={"status": "appointment_no_show"},
                )
            )
    else:
        events.append(
            new_event(
                ts=contact_time + timedelta(days=1),
                member=sales_member,
                dealership_id=dealership_id,
                action="follow_up_task_created",
                entity_id=lead_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="open",
                value=0.0,
                metadata={"task": "send_offer"},
            )
        )
        events.append(
            new_event(
                ts=contact_time + timedelta(days=4),
                member=sales_member,
                dealership_id=dealership_id,
                action="deal_closed_lost",
                entity_id=deal_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="lost_no_appointment",
                value=0.0,
                metadata={},
            )
        )

    if rng.random() < 0.5:
        note_ts = created_at + timedelta(minutes=rng.randint(15, 600))
        note_member = rng.choice([sales_member, bdc_member])
        events.append(
            new_event(
                ts=note_ts,
                member=note_member,
                dealership_id=dealership_id,
                action="note_added",
                entity_id=lead_id,
                lead_id=lead_id,
                opportunity_id=opportunity_id,
                customer_id=customer_id,
                result="saved",
                value=0.0,
                metadata={"note_type": rng.choice(["customer_pref", "objection", "vehicle_interest"])},
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
) -> list[Event]:
    rng = random.Random(seed)
    events: list[Event] = []
    lead_counter = 1

    for day_offset in range(days):
        day = start_date + timedelta(days=day_offset)
        leads_today = max(1, int(rng.gauss(daily_leads, max(2.0, daily_leads * 0.25))))
        for _ in range(leads_today):
            events.extend(
                generate_lead_workflow(
                    day=day,
                    lead_number=lead_counter,
                    team=team,
                    dealership_id=dealership_id,
                    rng=rng,
                )
            )
            lead_counter += 1

    events.sort(key=lambda e: e.event_ts)
    return events


def write_jsonl(events: list[Event], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")


def write_csv(events: list[Event], output_path: Path) -> None:
    rows = [event.to_dict() for event in events]
    fields = [
        "event_id",
        "event_ts",
        "source_system",
        "dealership_id",
        "team_member_id",
        "team_member_role",
        "action",
        "entity",
        "entity_id",
        "lead_id",
        "opportunity_id",
        "customer_id",
        "channel",
        "result",
        "value",
        "metadata",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            row["metadata"] = json.dumps(row["metadata"], separators=(",", ":"))
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic dealership CRM traffic events for testing."
    )
    parser.add_argument("--start-date", default=datetime.now().strftime("%Y-%m-%d"), help="Start date in YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=14, help="Number of days to simulate")
    parser.add_argument("--daily-leads", type=int, default=20, help="Average number of new leads per day")
    parser.add_argument("--salespeople", type=int, default=8, help="Number of sales reps")
    parser.add_argument("--managers", type=int, default=2, help="Number of sales managers")
    parser.add_argument("--bdc", type=int, default=3, help="Number of BDC agents")
    parser.add_argument("--dealership-id", default="DLR-001", help="Dealership identifier")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible output")
    parser.add_argument("--format", choices=["jsonl", "csv"], default="jsonl", help="Output format")
    parser.add_argument("--output", default="output/events.jsonl", help="Output file path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    team = build_team(salespeople=args.salespeople, managers=args.managers, bdc_agents=args.bdc)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    events = generate_events(
        start_date=start_date,
        days=args.days,
        daily_leads=args.daily_leads,
        team=team,
        dealership_id=args.dealership_id,
        seed=args.seed,
    )

    if args.format == "jsonl":
        write_jsonl(events, output_path)
    else:
        write_csv(events, output_path)

    print(
        json.dumps(
            {
                "events_written": len(events),
                "output": str(output_path),
                "format": args.format,
                "start_date": args.start_date,
                "days": args.days,
                "daily_leads": args.daily_leads,
                "seed": args.seed,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
