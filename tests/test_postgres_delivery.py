from __future__ import annotations

import uuid
from datetime import datetime, timezone

from dealmaker_generator import Event, normalize_delivery_url, send_events_to_api, validate_api_settings
from dealmaker_postgres import clear_events_for_reps
from app.supabase_client import clear_rep_data_for_reps, materialize_events_for_toprep


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


def test_clear_events_for_reps_clears_materialized_rep_tables(monkeypatch) -> None:
    executed: list[tuple[str, object]] = []

    class _Cursor:
        rowcount = 3

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql: str, params=None):
            executed.append((sql, params))

        def fetchall(self):
            return [
                ("events", "sales_rep_id"),
                ("deals", "sales_rep_id"),
                ("activities", "sales_rep_id"),
                ("rep_month_stats", "rep_id"),
                ("rep_month_forecast", "rep_id"),
            ]

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

        def commit(self):
            executed.append(("commit", None))

    class _Psycopg:
        @staticmethod
        def connect(_dsn: str, connect_timeout: int):
            return _Connection()

    monkeypatch.setitem(__import__("sys").modules, "psycopg", _Psycopg)

    rep_id = str(uuid.uuid4())
    result = clear_events_for_reps("postgresql://example", [rep_id])

    assert result["ok"] is True
    delete_sql = "\n".join(sql for sql, _params in executed)
    assert 'DELETE FROM "deals"' in delete_sql
    assert 'DELETE FROM "activities"' in delete_sql
    assert 'DELETE FROM "rep_month_stats"' in delete_sql
    assert 'DELETE FROM "rep_month_forecast"' in delete_sql


def test_clear_rep_data_for_reps_uses_supabase_rest_service_role(monkeypatch) -> None:
    requested: list[str] = []

    class _Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _fake_urlopen(req, timeout: int, context):
        requested.append(req.full_url)
        assert req.get_method() == "DELETE"
        assert req.headers["Authorization"].startswith("Bearer service-role")
        return _Response()

    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-token")
    monkeypatch.setenv("TOPREP_API_URL", "https://example.supabase.co")
    monkeypatch.setattr("app.supabase_client.request.urlopen", _fake_urlopen)

    result = clear_rep_data_for_reps([str(uuid.uuid4())])

    assert result["ok"] is True
    assert any("/rest/v1/rep_month_stats?" in url for url in requested)
    assert not any("/rest/v1/events?" in url for url in requested)


def test_materialize_events_for_toprep_upserts_deals_and_activities(monkeypatch) -> None:
    captured: dict[str, list[dict]] = {}

    def _fake_upsert(path: str, rows: list[dict], on_conflict: str = "id") -> dict:
        captured[path] = rows
        return {"ok": True, "inserted": len(rows)}

    monkeypatch.setattr("app.supabase_client._rest_upsert_rows", _fake_upsert)

    rep_id = str(uuid.uuid4())
    deal_id = str(uuid.uuid4())
    activity_id = str(uuid.uuid4())
    events = [
        {
            "sales_rep_id": rep_id,
            "type": "deal.created",
            "payload": {
                "deal_id": deal_id,
                "customer_name": "A Customer",
                "deal_amount": 35000,
                "gross_profit": 2400,
                "source": "internet",
            },
            "created_at": "2026-05-01T10:00:00.000Z",
        },
        {
            "sales_rep_id": rep_id,
            "type": "activity.completed",
            "payload": {
                "activity_id": activity_id,
                "deal_id": deal_id,
                "activity_type": "call",
                "outcome": "connected",
                "completed_at": "2026-05-01T10:10:00.000Z",
            },
            "created_at": "2026-05-01T10:10:00.000Z",
        },
        {
            "sales_rep_id": rep_id,
            "type": "deal.status_changed",
            "payload": {
                "deal_id": deal_id,
                "old_status": "lead",
                "new_status": "closed_won",
                "close_date": "2026-05-01",
            },
            "created_at": "2026-05-01T11:00:00.000Z",
        },
        {
            "sales_rep_id": rep_id,
            "type": "rep_quota_updated",
            "payload": {
                "month": "2026-05",
                "old_quota": 9,
                "new_quota": 10,
            },
            "created_at": "2026-05-01T08:00:00.000Z",
        },
    ]

    result = materialize_events_for_toprep(events)

    assert result["ok"] is True
    assert captured["deals"][0]["id"] == deal_id
    assert captured["deals"][0]["status"] == "closed_won"
    assert captured["activities"][0]["id"] == activity_id
    assert captured["activities"][0]["deal_id"] == deal_id
    assert captured["quotas"][0]["rep_id"] == rep_id
    assert captured["quotas"][0]["quota_units"] == 10
    assert captured["quotas"][0]["period_start"] == "2026-05-01"


def test_materialized_quota_uses_rep_history_instead_of_random_payload(monkeypatch) -> None:
    captured: dict[str, list[dict]] = {}

    def _fake_upsert(path: str, rows: list[dict], on_conflict: str = "id") -> dict:
        captured[path] = rows
        return {"ok": True, "inserted": len(rows)}

    monkeypatch.setattr("app.supabase_client._rest_upsert_rows", _fake_upsert)

    rep_id = str(uuid.uuid4())
    other_rep_id = str(uuid.uuid4())

    def _deal_events(rep: str, month: str, sold_count: int, lead_count: int = 10) -> list[dict]:
        rows: list[dict] = []
        for idx in range(lead_count):
            deal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{rep}|{month}|{idx}"))
            rows.append({
                "sales_rep_id": rep,
                "type": "deal.created",
                "payload": {
                    "deal_id": deal_id,
                    "customer_name": f"Customer {idx}",
                    "deal_amount": 30000,
                    "source": "internet",
                },
                "created_at": f"{month}-05T10:00:00.000Z",
            })
            rows.append({
                "sales_rep_id": rep,
                "type": "deal.status_changed",
                "payload": {
                    "deal_id": deal_id,
                    "old_status": "lead",
                    "new_status": "closed_won" if idx < sold_count else "closed_lost",
                    "close_date": f"{month}-20",
                },
                "created_at": f"{month}-20T10:00:00.000Z",
            })
        return rows

    events = (
        _deal_events(rep_id, "2026-02", 5)
        + _deal_events(rep_id, "2026-03", 8)
        + _deal_events(rep_id, "2026-04", 10)
        + _deal_events(rep_id, "2026-05", 1, lead_count=2)
        + _deal_events(other_rep_id, "2026-03", 4)
        + _deal_events(other_rep_id, "2026-04", 6)
        + [{
            "sales_rep_id": rep_id,
            "type": "rep_quota_updated",
            "payload": {"month": "2026-05", "old_quota": 60, "new_quota": 60},
            "created_at": "2026-05-01T08:00:00.000Z",
        }]
    )

    result = materialize_events_for_toprep(events)

    assert result["ok"] is True
    may_quota = next(
        row for row in captured["quotas"]
        if row["rep_id"] == rep_id and row["period_start"] == "2026-05-01"
    )
    assert may_quota["quota_units"] == 9
