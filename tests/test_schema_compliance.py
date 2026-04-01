"""Schema-compliance tests: prove DealMaker events match the TopRep API contract.

Source of truth: REALTIME_DATA_INGEST_REFERENCE.md
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from dealmaker_generator import (
    ACTIVITY_OUTCOMES,
    ACTIVITY_TYPES,
    ALLOWED_EVENT_TYPES,
    REQUIRED_PAYLOAD_KEYS,
    STATUS_VALUES,
    Event,
    build_team,
    generate_events,
    to_iso,
    validate_event,
    validate_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEALERSHIP = "DLR-TEST"
_SALES_REP_ID = str(uuid.uuid4())


def _make_event(event_type: str, payload: dict, *, rep_id: str = _SALES_REP_ID) -> Event:
    ts = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
    return Event(
        sales_rep_id=rep_id,
        type=event_type,
        payload=payload,
        created_at=to_iso(ts),
    )


def _deal_id() -> str:
    return str(uuid.uuid4())


def _activity_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Envelope-level contract
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_valid_envelope_passes(self):
        event = _make_event("deal.created", {
            "deal_id": _deal_id(),
            "customer_name": "Alice",
            "deal_amount": 25000,
            "source": "internet",
        })
        assert validate_event(event) == []

    def test_missing_sales_rep_id(self):
        event = _make_event("deal.created", {
            "deal_id": _deal_id(),
            "customer_name": "Alice",
            "deal_amount": 25000,
            "source": "internet",
        }, rep_id="")
        errs = validate_event(event)
        assert any("sales_rep_id" in e for e in errs)

    def test_invalid_sales_rep_id_uuid(self):
        event = _make_event("deal.created", {
            "deal_id": _deal_id(),
            "customer_name": "Alice",
            "deal_amount": 25000,
            "source": "internet",
        }, rep_id="not-a-uuid")
        errs = validate_event(event)
        assert any("sales_rep_id" in e for e in errs)

    def test_unknown_event_type(self):
        event = _make_event("unknown.type", {})
        errs = validate_event(event)
        assert any("unknown event type" in e for e in errs)

    def test_missing_created_at(self):
        event = Event(
            sales_rep_id=_SALES_REP_ID,
            type="deal.created",
            payload={"deal_id": _deal_id(), "customer_name": "Alice", "deal_amount": 25000, "source": "internet"},
            created_at="",
        )
        errs = validate_event(event)
        assert any("created_at" in e for e in errs)

    def test_created_at_wrong_format_no_millis(self):
        event = Event(
            sales_rep_id=_SALES_REP_ID,
            type="deal.created",
            payload={"deal_id": _deal_id(), "customer_name": "Alice", "deal_amount": 25000, "source": "internet"},
            created_at="2026-03-01T09:00:00Z",
        )
        errs = validate_event(event)
        assert any("created_at" in e for e in errs)

    def test_created_at_with_offset_instead_of_z(self):
        event = Event(
            sales_rep_id=_SALES_REP_ID,
            type="deal.created",
            payload={"deal_id": _deal_id(), "customer_name": "Alice", "deal_amount": 25000, "source": "internet"},
            created_at="2026-03-01T09:00:00.000+00:00",
        )
        errs = validate_event(event)
        assert any("created_at" in e for e in errs)

    def test_created_at_correct_format(self):
        event = Event(
            sales_rep_id=_SALES_REP_ID,
            type="deal.created",
            payload={"deal_id": _deal_id(), "customer_name": "Alice", "deal_amount": 25000, "source": "internet"},
            created_at="2026-03-01T09:00:00.000Z",
        )
        errs = validate_event(event)
        assert not any("created_at" in e for e in errs)


# ---------------------------------------------------------------------------
# Per-type payload validation
# ---------------------------------------------------------------------------


class TestDealCreated:
    def test_valid(self):
        did = _deal_id()
        event = _make_event("deal.created", {
            "deal_id": did,
            "customer_name": "Bob",
            "deal_amount": 30000,
            "gross_profit": 2500,
            "source": "referral",
        })
        assert validate_event(event) == []

    def test_valid_without_optional_gross_profit(self):
        did = _deal_id()
        event = _make_event("deal.created", {
            "deal_id": did,
            "customer_name": "Bob",
            "deal_amount": 30000,
            "source": "referral",
        })
        assert validate_event(event) == []

    @pytest.mark.parametrize("missing_key", REQUIRED_PAYLOAD_KEYS["deal.created"])
    def test_missing_required_key(self, missing_key):
        payload = {
            "deal_id": _deal_id(),
            "customer_name": "Bob",
            "deal_amount": 30000,
            "source": "referral",
        }
        del payload[missing_key]
        event = _make_event("deal.created", payload)
        errs = validate_event(event)
        assert any(missing_key in e for e in errs)


class TestDealStatusChanged:
    def test_valid_no_reason(self):
        event = _make_event("deal.status_changed", {
            "deal_id": _deal_id(),
            "old_status": "lead",
            "new_status": "qualified",
        })
        assert validate_event(event) == []

    def test_valid_with_reason(self):
        event = _make_event("deal.status_changed", {
            "deal_id": _deal_id(),
            "old_status": "negotiation",
            "new_status": "closed_won",
            "reason": "sold",
        })
        assert validate_event(event) == []

    @pytest.mark.parametrize("status", STATUS_VALUES)
    def test_all_status_values_accepted(self, status):
        event = _make_event("deal.status_changed", {
            "deal_id": _deal_id(),
            "old_status": "lead",
            "new_status": status,
        })
        errs = validate_event(event)
        assert not any("new_status" in e for e in errs)

    def test_invalid_status_value(self):
        event = _make_event("deal.status_changed", {
            "deal_id": _deal_id(),
            "old_status": "lead",
            "new_status": "unknown_stage",
        })
        errs = validate_event(event)
        assert any("new_status" in e for e in errs)


class TestActivityScheduled:
    def test_valid(self):
        event = _make_event("activity.scheduled", {
            "activity_id": _activity_id(),
            "deal_id": _deal_id(),
            "activity_type": "call",
            "scheduled_for": "2026-03-01T10:00:00.000Z",
        })
        assert validate_event(event) == []

    def test_valid_without_optional_deal_id(self):
        event = _make_event("activity.scheduled", {
            "activity_id": _activity_id(),
            "activity_type": "email",
            "scheduled_for": "2026-03-01T10:00:00.000Z",
        })
        assert validate_event(event) == []

    @pytest.mark.parametrize("activity_type", ACTIVITY_TYPES)
    def test_all_activity_types_accepted(self, activity_type):
        event = _make_event("activity.scheduled", {
            "activity_id": _activity_id(),
            "deal_id": _deal_id(),
            "activity_type": activity_type,
            "scheduled_for": "2026-03-01T10:00:00.000Z",
        })
        errs = validate_event(event)
        assert not any("activity_type" in e for e in errs)

    def test_invalid_activity_type(self):
        event = _make_event("activity.scheduled", {
            "activity_id": _activity_id(),
            "deal_id": _deal_id(),
            "activity_type": "smoke_signal",
            "scheduled_for": "2026-03-01T10:00:00.000Z",
        })
        errs = validate_event(event)
        assert any("activity_type" in e for e in errs)


class TestActivityCompleted:
    def test_valid(self):
        event = _make_event("activity.completed", {
            "activity_id": _activity_id(),
            "deal_id": _deal_id(),
            "activity_type": "call",
            "outcome": "connected",
        })
        assert validate_event(event) == []

    @pytest.mark.parametrize("outcome", ACTIVITY_OUTCOMES)
    def test_all_outcomes_accepted(self, outcome):
        event = _make_event("activity.completed", {
            "activity_id": _activity_id(),
            "deal_id": _deal_id(),
            "activity_type": "call",
            "outcome": outcome,
        })
        errs = validate_event(event)
        assert not any("outcome" in e for e in errs)

    def test_invalid_outcome(self):
        event = _make_event("activity.completed", {
            "activity_id": _activity_id(),
            "deal_id": _deal_id(),
            "activity_type": "call",
            "outcome": "bad_value",
        })
        errs = validate_event(event)
        assert any("outcome" in e for e in errs)


class TestRepQuotaUpdated:
    def test_valid_no_reason(self):
        event = _make_event("rep_quota_updated", {
            "month": "2026-03",
            "old_quota": 40,
            "new_quota": 45,
        })
        assert validate_event(event) == []

    def test_valid_with_reason(self):
        event = _make_event("rep_quota_updated", {
            "month": "2026-03",
            "old_quota": 40,
            "new_quota": 45,
            "reason": "seasonality",
        })
        assert validate_event(event) == []

    @pytest.mark.parametrize("missing_key", REQUIRED_PAYLOAD_KEYS["rep_quota_updated"])
    def test_missing_required_key(self, missing_key):
        payload = {"month": "2026-03", "old_quota": 40, "new_quota": 45}
        del payload[missing_key]
        event = _make_event("rep_quota_updated", payload)
        errs = validate_event(event)
        assert any(missing_key in e for e in errs)


# ---------------------------------------------------------------------------
# validate_events summary
# ---------------------------------------------------------------------------


class TestValidateEvents:
    def test_all_valid_events(self):
        events = [
            _make_event("deal.created", {
                "deal_id": _deal_id(), "customer_name": "Alice",
                "deal_amount": 20000, "source": "internet",
            }),
            _make_event("activity.completed", {
                "activity_id": _activity_id(), "deal_id": _deal_id(),
                "activity_type": "call", "outcome": "connected",
            }),
        ]
        report = validate_events(events)
        assert report["passed"] is True
        assert report["total"] == 2
        assert report["valid"] == 2
        assert report["invalid"] == 0
        assert report["errors"] == []

    def test_one_invalid_event(self):
        events = [
            _make_event("deal.created", {
                "deal_id": _deal_id(), "customer_name": "Alice",
                "deal_amount": 20000, "source": "internet",
            }),
            _make_event("bad.type", {}),
        ]
        report = validate_events(events)
        assert report["passed"] is False
        assert report["invalid"] == 1
        assert len(report["errors"]) == 1


# ---------------------------------------------------------------------------
# End-to-end: full generate_events output passes validation
# ---------------------------------------------------------------------------


class TestGeneratedEventsCompliance:
    """Prove that every event emitted by generate_events satisfies the contract."""

    @pytest.mark.parametrize("seed", [0, 42, 99, 1337])
    def test_all_events_valid_for_seed(self, seed):
        team = build_team(salespeople=4, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start,
            days=3,
            daily_leads=5,
            team=team,
            dealership_id=_DEALERSHIP,
            seed=seed,
        )
        assert len(events) > 0, "expected at least one event"
        report = validate_events(events)
        assert report["passed"] is True, (
            f"seed={seed}: {report['invalid']} invalid events – "
            f"first errors: {report['errors'][:3]}"
        )

    def test_event_type_coverage(self):
        """All allowed event types must appear in a normal run."""
        team = build_team(salespeople=4, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        # Provide multiple rep IDs so deal.reassigned events can be generated
        rep_ids = [str(uuid.uuid4()) for _ in range(4)]
        events = generate_events(
            start_date=start,
            days=30,
            daily_leads=10,
            team=team,
            dealership_id=_DEALERSHIP,
            seed=42,
            sales_rep_ids=rep_ids,
        )
        found_types = {e.type for e in events}
        for et in ALLOWED_EVENT_TYPES:
            assert et in found_types, f"event type {et!r} never generated"

    def test_created_at_format(self):
        """Every created_at must be UTC ISO-8601 with ms and Z."""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        team = build_team(salespeople=2, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start, days=2, daily_leads=3, team=team,
            dealership_id=_DEALERSHIP, seed=7,
        )
        for event in events:
            assert pattern.match(event.created_at), (
                f"bad created_at format: {event.created_at!r}"
            )
