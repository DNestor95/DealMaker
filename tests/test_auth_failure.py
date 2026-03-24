"""Tests for HTTP 401 authentication-failure handling.

Verifies that post_event_to_api, post_actions_batch_to_edge, and
send_events_to_api all surface the canonical
"Authentication failed (HTTP 401) — check TOPREP_AUTH_TOKEN." message
when the API responds with 401 Unauthorized, and that send_events_to_api
propagates it in the errors list.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from http.client import HTTPMessage
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

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

_AUTH_ERROR_MSG = AUTH_ERROR_401

_FAKE_URL = "https://example.supabase.co/functions/v1/ingest"
_FAKE_REST_URL = "https://example.supabase.co/rest/v1/events"


def _make_401_error() -> HTTPError:
    """Build a urllib HTTPError that mimics a 401 response."""
    return HTTPError(
        url=_FAKE_URL,
        code=401,
        msg="Unauthorized",
        hdrs=HTTPMessage(),
        fp=BytesIO(b'{"message":"Invalid JWT"}'),
    )


def _make_event() -> Event:
    ts = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
    return Event(
        sales_rep_id=str(uuid.uuid4()),
        type="deal.created",
        payload={
            "deal_id": str(uuid.uuid4()),
            "customer_name": "Test Customer",
            "deal_amount": 25000,
            "source": "internet",
        },
        created_at=to_iso(ts),
    )


# ---------------------------------------------------------------------------
# post_event_to_api — 401 handling
# ---------------------------------------------------------------------------


class TestPostEventToApi:
    def test_returns_auth_error_on_401(self):
        with patch("dealmaker_generator.request.urlopen", side_effect=_make_401_error()):
            ok, detail = post_event_to_api(
                event=_make_event(),
                api_url=_FAKE_REST_URL,
                auth_token="bad-token",
            )
        assert ok is False
        assert detail == _AUTH_ERROR_MSG

    def test_does_not_swallow_other_http_errors(self):
        other_err = HTTPError(
            url=_FAKE_REST_URL,
            code=500,
            msg="Internal Server Error",
            hdrs=HTTPMessage(),
            fp=BytesIO(b"server error"),
        )
        with patch("dealmaker_generator.request.urlopen", side_effect=other_err):
            ok, detail = post_event_to_api(
                event=_make_event(),
                api_url=_FAKE_REST_URL,
                auth_token="any-token",
            )
        assert ok is False
        assert "500" in detail
        assert _AUTH_ERROR_MSG not in detail


# ---------------------------------------------------------------------------
# post_actions_batch_to_edge — 401 handling
# ---------------------------------------------------------------------------


class TestPostActionsBatchToEdge:
    def test_returns_auth_error_on_401(self):
        events = [_make_event()]
        with patch("dealmaker_generator.request.urlopen", side_effect=_make_401_error()):
            ok, detail, inserted = post_actions_batch_to_edge(
                events=events,
                api_url=_FAKE_URL,
                auth_token="bad-token",
            )
        assert ok is False
        assert detail == _AUTH_ERROR_MSG
        assert inserted == 0

    def test_does_not_swallow_other_http_errors(self):
        other_err = HTTPError(
            url=_FAKE_URL,
            code=403,
            msg="Forbidden",
            hdrs=HTTPMessage(),
            fp=BytesIO(b"forbidden"),
        )
        events = [_make_event()]
        with patch("dealmaker_generator.request.urlopen", side_effect=other_err):
            ok, detail, inserted = post_actions_batch_to_edge(
                events=events,
                api_url=_FAKE_URL,
                auth_token="any-token",
            )
        assert ok is False
        assert "403" in detail
        assert _AUTH_ERROR_MSG not in detail


# ---------------------------------------------------------------------------
# send_events_to_api — propagates 401 error string
# ---------------------------------------------------------------------------


class TestSendEventsToApi:
    def test_propagates_auth_error_for_rest_url(self):
        events = [_make_event()]
        with patch("dealmaker_generator.request.urlopen", side_effect=_make_401_error()):
            result = send_events_to_api(
                events=events,
                api_url=_FAKE_REST_URL,
                auth_token="bad-token",
                max_retries=0,
            )
        assert result["failed"] == 1
        assert result["sent"] == 0
        assert any(_AUTH_ERROR_MSG in e for e in result["errors"])

    def test_propagates_auth_error_for_edge_function_url(self):
        events = [_make_event()]
        with patch("dealmaker_generator.request.urlopen", side_effect=_make_401_error()):
            result = send_events_to_api(
                events=events,
                api_url=_FAKE_URL,
                auth_token="bad-token",
                max_retries=0,
            )
        assert result["failed"] == len(events)
        assert result["sent"] == 0
        assert any(_AUTH_ERROR_MSG in e for e in result["errors"])
