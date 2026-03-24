"""End-to-end data-sending tests for DealMaker.

Strategy
--------
* A **real local HTTP server** (Python's TCPServer over a real TCP socket) is
  started on an ephemeral port.  ``post_event_to_api``, ``send_events_to_api``,
  and ``post_actions_batch_to_edge`` are called pointing to that server.
  This proves that the HTTP delivery mechanism fires an actual network request
  — no mocks, no patching.

* A **Flask test-client** integration exercises the backfill route with file
  delivery, confirming that the route generates and persists events end-to-end.

* A **live integration test** (``TestLiveDelivery``) sends one real event to
  the configured TopRep Supabase endpoint.  It is skipped automatically when
  ``TOPREP_AUTH_TOKEN`` is not set, so it only runs in CI or locally when
  credentials are available.
"""
from __future__ import annotations

import json
import os
import queue
import socketserver
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from dealmaker_generator import (
    AUTH_ERROR_401,
    Event,
    build_team,
    generate_events,
    post_actions_batch_to_edge,
    post_event_to_api,
    send_events_to_api,
    to_iso,
)


# ---------------------------------------------------------------------------
# Local TCP-based HTTP server fixture
# ---------------------------------------------------------------------------

class _RecordingHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that records incoming requests in a queue."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server._requests.put({   # type: ignore[attr-defined]
            "method": "POST",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"inserted":1}')

    def log_message(self, fmt, *args):  # suppress server log noise
        pass


@pytest.fixture()
def local_server():
    """Start a real local HTTP server on a random port; yield its base URL."""
    request_queue: queue.Queue = queue.Queue()

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._requests = request_queue

    server = _Server(("127.0.0.1", 0), _RecordingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}", request_queue

    server.shutdown()
    thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEALERSHIP = "DLR-TEST-SEND"
_FAKE_TOKEN = "fake-bearer-token-for-local-test"
_FAKE_APIKEY = "fake-apikey"


def _make_event() -> Event:
    ts = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
    return Event(
        sales_rep_id=str(uuid.uuid4()),
        type="deal.created",
        payload={
            "deal_id": str(uuid.uuid4()),
            "customer_name": "Local Test Customer",
            "deal_amount": 27500,
            "source": "internet",
        },
        created_at=to_iso(ts),
    )


def _drain(q: queue.Queue, timeout: float = 2.0) -> list[dict]:
    """Collect all items currently in the queue, waiting up to *timeout* s."""
    items = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            if items:
                break
            time.sleep(0.05)
    return items


# ---------------------------------------------------------------------------
# TestHttpDelivery — real TCP, post_event_to_api
# ---------------------------------------------------------------------------

class TestHttpDelivery:
    """post_event_to_api sends a real HTTP POST over TCP and the server receives it."""

    def test_post_is_made(self, local_server):
        base_url, req_queue = local_server
        event = _make_event()

        ok, detail = post_event_to_api(
            event=event,
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
        )

        assert ok is True, f"unexpected failure: {detail}"
        received = _drain(req_queue)
        assert len(received) == 1, "expected exactly one HTTP request"

    def test_correct_http_method_and_path(self, local_server):
        base_url, req_queue = local_server
        ok, _ = post_event_to_api(
            event=_make_event(),
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
        )
        assert ok is True
        req = _drain(req_queue)[0]
        assert req["method"] == "POST"
        assert req["path"] == "/api/events"

    def test_authorization_header_is_sent(self, local_server):
        base_url, req_queue = local_server
        post_event_to_api(
            event=_make_event(),
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
        )
        req = _drain(req_queue)[0]
        auth = req["headers"].get("Authorization", req["headers"].get("authorization", ""))
        assert auth == f"Bearer {_FAKE_TOKEN}"

    def test_content_type_is_json(self, local_server):
        base_url, req_queue = local_server
        post_event_to_api(
            event=_make_event(),
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
        )
        req = _drain(req_queue)[0]
        ct = req["headers"].get("Content-Type", req["headers"].get("content-type", ""))
        assert "application/json" in ct

    def test_body_is_valid_event_json(self, local_server):
        base_url, req_queue = local_server
        event = _make_event()
        post_event_to_api(
            event=event,
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
        )
        req = _drain(req_queue)[0]
        body = json.loads(req["body"])
        assert body["sales_rep_id"] == event.sales_rep_id
        assert body["type"] == event.type
        assert body["payload"]["deal_id"] == event.payload["deal_id"]
        assert "created_at" in body

    def test_supabase_rest_adds_apikey_header(self, local_server):
        base_url, req_queue = local_server
        post_event_to_api(
            event=_make_event(),
            api_url=f"{base_url}/rest/v1/events",
            auth_token=_FAKE_TOKEN,
            supabase_apikey=_FAKE_APIKEY,
        )
        req = _drain(req_queue)[0]
        apikey = req["headers"].get("apikey", req["headers"].get("Apikey", ""))
        assert apikey == _FAKE_APIKEY


# ---------------------------------------------------------------------------
# TestSendEventsToApiPipeline — generate → send → verify via TCP
# ---------------------------------------------------------------------------

class TestSendEventsToApiPipeline:
    """Full pipeline: generate_events → send_events_to_api → real HTTP server."""

    def test_all_events_sent(self, local_server):
        base_url, req_queue = local_server
        team = build_team(salespeople=2, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start,
            days=1,
            daily_leads=3,
            team=team,
            dealership_id=_DEALERSHIP,
            seed=42,
        )
        assert len(events) > 0

        result = send_events_to_api(
            events=events,
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
            max_retries=0,
        )

        assert result["failed"] == 0, f"some events failed: {result['errors']}"
        assert result["sent"] == len(events)

    def test_each_posted_body_has_required_fields(self, local_server):
        base_url, req_queue = local_server
        team = build_team(salespeople=2, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start,
            days=1,
            daily_leads=2,
            team=team,
            dealership_id=_DEALERSHIP,
            seed=7,
        )

        send_events_to_api(
            events=events,
            api_url=f"{base_url}/api/events",
            auth_token=_FAKE_TOKEN,
            max_retries=0,
        )

        received = _drain(req_queue, timeout=5.0)
        assert len(received) == len(events)
        for req in received:
            body = json.loads(req["body"])
            assert "sales_rep_id" in body
            assert "type" in body
            assert "payload" in body
            assert "created_at" in body


# ---------------------------------------------------------------------------
# TestEdgeFunctionBatchDelivery — post_actions_batch_to_edge → TCP server
# ---------------------------------------------------------------------------

class TestEdgeFunctionBatchDelivery:
    """post_actions_batch_to_edge sends one request with deals+actions arrays."""

    def test_batch_is_sent_as_single_request(self, local_server):
        base_url, req_queue = local_server
        team = build_team(salespeople=2, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start, days=1, daily_leads=2, team=team,
            dealership_id=_DEALERSHIP, seed=99,
        )

        ok, detail, inserted = post_actions_batch_to_edge(
            events=events,
            api_url=f"{base_url}/functions/v1/ingest",
            auth_token=_FAKE_TOKEN,
        )

        assert ok is True, f"batch delivery failed: {detail}"
        received = _drain(req_queue)
        assert len(received) == 1, "expected exactly one batched request"

    def test_batch_body_contains_deals_and_actions(self, local_server):
        base_url, req_queue = local_server
        team = build_team(salespeople=2, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start, days=1, daily_leads=2, team=team,
            dealership_id=_DEALERSHIP, seed=11,
        )

        post_actions_batch_to_edge(
            events=events,
            api_url=f"{base_url}/functions/v1/ingest",
            auth_token=_FAKE_TOKEN,
        )

        req = _drain(req_queue)[0]
        body = json.loads(req["body"])
        assert "deals" in body and isinstance(body["deals"], list)
        assert "actions" in body and isinstance(body["actions"], list)
        assert len(body["actions"]) == len(events)


# ---------------------------------------------------------------------------
# TestFlaskBackfillRoute — Flask test client, file delivery
# ---------------------------------------------------------------------------

class TestFlaskBackfillRoute:
    """Flask test-client exercises the backfill route and verifies event output."""

    @pytest.fixture(autouse=True)
    def _app(self, tmp_path, monkeypatch):
        """Create a minimal Flask test app with a temp output directory."""
        import app.routes.stores as stores_mod

        monkeypatch.setattr(stores_mod, "_OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(stores_mod, "_STORES_FILE", tmp_path / "stores_config.json")
        # Clear in-memory store registry so tests are isolated
        stores_mod._stores.clear()

        from app import create_app
        flask_app = create_app()
        flask_app.config["TESTING"] = True
        self._client = flask_app.test_client()
        self._tmp = tmp_path

    def _register_store(self) -> str:
        store_id = "DLR-BACKFILL-TEST"
        from app.routes.stores import _stores
        _stores[store_id] = {
            "dealership_id": store_id,
            "display_name": "Backfill Test Store",
            "salespeople": 2,
            "managers": 1,
            "bdc_agents": 1,
            "daily_leads": 3,
            "seed": 42,
            "delivery": "file",
            "close_rate_pct": 36,
            "deal_amount_min": 12000,
            "deal_amount_max": 68000,
            "gross_profit_min": 700,
            "gross_profit_max": 6000,
            "activities_per_deal_min": 2,
            "activities_per_deal_max": 6,
            "month_shape": "flat",
            "archetype_dist": {},
            "default_scenarios": [],
            "status": "stopped",
            "events_sent": 0,
        }
        return store_id

    def test_backfill_creates_output_file(self):
        store_id = self._register_store()
        resp = self._client.post(
            f"/stores/{store_id}/backfill",
            data={"start_date": "2026-03-01", "end_date": "2026-03-02", "delivery": "file"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["events"] > 0
        assert data["days"] == 2

    def test_backfill_file_contains_valid_events(self):
        store_id = self._register_store()
        resp = self._client.post(
            f"/stores/{store_id}/backfill",
            data={"start_date": "2026-03-01", "end_date": "2026-03-01", "delivery": "file"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        out_file = Path(data["file"])
        assert out_file.exists(), "output JSONL file was not created"
        lines = [l for l in out_file.read_text().splitlines() if l.strip()]
        assert len(lines) == data["events"]
        for line in lines:
            ev = json.loads(line)
            assert "sales_rep_id" in ev
            assert "type" in ev
            assert "payload" in ev
            assert "created_at" in ev

    def test_backfill_returns_401_without_token_for_api_delivery(self, monkeypatch):
        monkeypatch.delenv("TOPREP_AUTH_TOKEN", raising=False)
        store_id = self._register_store()
        resp = self._client.post(
            f"/stores/{store_id}/backfill",
            data={"start_date": "2026-03-01", "end_date": "2026-03-01", "delivery": "api"},
        )
        assert resp.status_code == 401
        body = resp.get_json()
        assert AUTH_ERROR_401 in body["error"]


# ---------------------------------------------------------------------------
# TestLiveDelivery — real Supabase, skipped when token absent
# ---------------------------------------------------------------------------

_LIVE_TOKEN = os.getenv("TOPREP_AUTH_TOKEN", "")
_LIVE_URL = "https://ahimfdfuuefesgbbnccr.supabase.co"

# Import the baked-in publishable key from supabase_client (safe to include in
# source per the comment in that module; importing keeps it DRY).
from app.supabase_client import _TOPREP_PUBLISHABLE_KEY as _LIVE_APIKEY


@pytest.mark.skipif(
    not _LIVE_TOKEN,
    reason="TOPREP_AUTH_TOKEN not set — skipping live Supabase delivery test",
)
class TestLiveDelivery:
    """Integration tests that send a real event to the TopRep Supabase project.

    These tests run automatically in CI when the TOPREP_AUTH_TOKEN secret is
    configured and are skipped otherwise.
    """

    def _event(self) -> Event:
        ts = datetime.now(timezone.utc)
        return Event(
            sales_rep_id=str(uuid.uuid4()),
            type="activity.completed",
            payload={
                "activity_id": str(uuid.uuid4()),
                "deal_id": str(uuid.uuid4()),
                "activity_type": "call",
                "outcome": "connected",
            },
            created_at=to_iso(ts),
        )

    def test_connection_is_reachable(self):
        from app.supabase_client import check_connection
        result = check_connection()
        assert result["ok"] is True, f"connection check failed: {result.get('error')}"

    def test_send_one_event_to_api_events(self):
        api_url = f"{_LIVE_URL}/api/events"
        ok, detail = post_event_to_api(
            event=self._event(),
            api_url=api_url,
            auth_token=_LIVE_TOKEN,
            supabase_apikey=_LIVE_APIKEY,
        )
        assert ok is True, f"live delivery failed: {detail}"

    def test_send_events_batch_to_api(self):
        team = build_team(salespeople=2, managers=1, bdc_agents=1)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = generate_events(
            start_date=start, days=1, daily_leads=2, team=team,
            dealership_id="DLR-LIVE-CI", seed=42,
        )
        result = send_events_to_api(
            events=events,
            api_url=f"{_LIVE_URL}/api/events",
            auth_token=_LIVE_TOKEN,
            supabase_apikey=_LIVE_APIKEY,
            max_retries=0,
        )
        assert result["failed"] == 0, (
            f"live batch failed: {result['errors']}"
        )
        assert result["sent"] == len(events)
