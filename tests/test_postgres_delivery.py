from __future__ import annotations

import uuid
from datetime import datetime, timezone

from dealmaker_generator import Event, normalize_delivery_url, send_events_to_api, validate_api_settings


def _event() -> Event:
    return Event(
        sales_rep_id=str(uuid.uuid4()),
        type="deal.created",
        payload={
            "deal_id": str(uuid.uuid4()),
            "customer_name": "DB Test Customer",
            "deal_amount": 32000,
            "source": "internet",
        },
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def test_normalize_delivery_url_keeps_postgres_dsn() -> None:
    dsn = "postgresql://postgres:secret@db.example.supabase.co:5432/postgres"
    assert normalize_delivery_url(dsn) == dsn


def test_validate_api_settings_allows_postgres_dsn() -> None:
    validate_api_settings(
        api_url="postgresql://postgres:secret@db.example.supabase.co:5432/postgres",
        auth_token="",
        supabase_apikey="",
    )


def test_send_events_to_api_uses_postgres_insert(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_insert_events(database_url: str, rows: list[dict]) -> tuple[int, list[str]]:
        captured["database_url"] = database_url
        captured["rows"] = rows
        return len(rows), []

    monkeypatch.setattr("dealmaker_generator.insert_events", _fake_insert_events)

    event = _event()
    dsn = "postgresql://postgres:secret@db.example.supabase.co:5432/postgres"
    result = send_events_to_api([event], dsn, auth_token="", max_retries=0)

    assert result == {"sent": 1, "failed": 0, "errors": []}
    assert captured["database_url"] == dsn
    assert captured["rows"] == [event.to_dict()]