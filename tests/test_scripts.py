"""Tests for the scripts/fetch_jwt.py and scripts/check_connection.py helper scripts.

Strategy
--------
* fetch_jwt.py — tests verify .env read/write helpers, URL parsing, and that
  the token is masked (not printed in full) and not logged.  The actual HTTP
  POST is not made; we patch urllib.request.urlopen.

* check_connection.py — tests verify exit-code behaviour, token masking, and
  that the --send flag calls the right delivery path.  HTTP calls are patched
  so no real network traffic is made.
"""
from __future__ import annotations

import json
import sys
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to import the scripts as modules
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).parent.parent / "scripts"


def _import_script(name: str):
    """Import a script from the scripts/ directory."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Lazy module-level cache so we only import once per test session
@pytest.fixture(scope="module")
def fetch_jwt():
    return _import_script("fetch_jwt")


@pytest.fixture(scope="module")
def check_connection_script():
    return _import_script("check_connection")


# ---------------------------------------------------------------------------
# fetch_jwt — .env helpers
# ---------------------------------------------------------------------------


class TestFetchJwtEnvHelpers:
    def test_read_env_parses_key_value(self, tmp_path, fetch_jwt):
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=testtoken\nSUPABASE_ANON_KEY=mykey\n")
        result = fetch_jwt._read_env(env)
        assert result["TOPREP_AUTH_TOKEN"] == "testtoken"
        assert result["SUPABASE_ANON_KEY"] == "mykey"

    def test_read_env_ignores_comments_and_blank_lines(self, tmp_path, fetch_jwt):
        env = tmp_path / ".env"
        env.write_text("# comment\n\nFOO=bar\n")
        result = fetch_jwt._read_env(env)
        assert "FOO" in result
        assert len(result) == 1

    def test_read_env_strips_quotes(self, tmp_path, fetch_jwt):
        env = tmp_path / ".env"
        env.write_text('KEY="quoted_value"\n')
        result = fetch_jwt._read_env(env)
        assert result["KEY"] == "quoted_value"

    def test_write_env_key_updates_existing(self, tmp_path, fetch_jwt):
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=old\nOTHER=unchanged\n")
        fetch_jwt._write_env_key(env, "TOPREP_AUTH_TOKEN", "newtoken")
        text = env.read_text()
        assert "TOPREP_AUTH_TOKEN=newtoken" in text
        assert "TOPREP_AUTH_TOKEN=old" not in text
        assert "OTHER=unchanged" in text

    def test_write_env_key_appends_new_key(self, tmp_path, fetch_jwt):
        env = tmp_path / ".env"
        env.write_text("OTHER=value\n")
        fetch_jwt._write_env_key(env, "TOPREP_AUTH_TOKEN", "abc123")
        text = env.read_text()
        assert "TOPREP_AUTH_TOKEN=abc123" in text

    def test_write_env_key_creates_file_if_missing(self, tmp_path, fetch_jwt):
        env = tmp_path / "new.env"
        assert not env.exists()
        fetch_jwt._write_env_key(env, "FOO", "bar")
        assert env.exists()
        assert "FOO=bar" in env.read_text()


class TestFetchJwtUrlParsing:
    def test_supabase_root_bare_url(self, fetch_jwt):
        url = "https://abc123.supabase.co"
        assert fetch_jwt._supabase_root(url) == "https://abc123.supabase.co"

    def test_supabase_root_strips_api_events(self, fetch_jwt):
        url = "https://abc123.supabase.co/api/events"
        assert fetch_jwt._supabase_root(url) == "https://abc123.supabase.co"

    def test_supabase_root_strips_functions_path(self, fetch_jwt):
        url = "https://abc123.supabase.co/functions/v1/my-fn"
        assert fetch_jwt._supabase_root(url) == "https://abc123.supabase.co"

    def test_supabase_root_maps_edge_function_host(self, fetch_jwt):
        url = "https://abc123.functions.supabase.co/my-fn"
        result = fetch_jwt._supabase_root(url)
        assert "functions" not in result
        assert "supabase.co" in result


class TestFetchJwtTokenFetch:
    def test_fetch_token_returns_access_token(self, fetch_jwt):
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"access_token": "tok123"}).encode()
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response):
            token = fetch_jwt.fetch_token(
                "https://proj.supabase.co", "anon-key", "user@test.com", "pass"
            )
        assert token == "tok123"

    def test_fetch_token_exits_on_missing_token(self, fetch_jwt):
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"error": "invalid_grant"}).encode()
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response):
            with pytest.raises(SystemExit) as exc_info:
                fetch_jwt.fetch_token(
                    "https://proj.supabase.co", "anon-key", "user@test.com", "bad"
                )
        assert exc_info.value.code == 1

    def test_token_output_is_masked(self, fetch_jwt, tmp_path, capsys):
        """Token printed to stdout must be ≤ 20 chars + ellipsis, not the full token."""
        long_token = "A" * 200
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=placeholder\n")

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"access_token": long_token}).encode()
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=fake_response),
            patch("getpass.getpass", return_value="secret"),
        ):
            fetch_jwt.main(["--env", str(env), "--email", "u@test.com"])

        captured = capsys.readouterr()
        # The full token must not appear in stdout
        assert long_token not in captured.out
        # A prefix followed by "…" should appear
        assert "…" in captured.out or "..." in captured.out


# ---------------------------------------------------------------------------
# check_connection — env check and masking
# ---------------------------------------------------------------------------


class TestCheckConnectionScript:
    def test_exits_1_when_token_not_set(self, tmp_path, check_connection_script, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# no token\n")
        monkeypatch.delenv("TOPREP_AUTH_TOKEN", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            check_connection_script.main(["--env", str(env)])
        assert exc_info.value.code == 1

    def test_exits_1_when_token_is_placeholder(self, tmp_path, check_connection_script, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=your-user-jwt\n")
        monkeypatch.delenv("TOPREP_AUTH_TOKEN", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            check_connection_script.main(["--env", str(env)])
        assert exc_info.value.code == 1

    def test_mask_hides_most_of_token(self, check_connection_script):
        token = "supersecrettoken12345678"
        masked = check_connection_script._mask(token, visible=12)
        assert masked.startswith(token[:12])
        assert token[12:] not in masked
        assert "…" in masked

    def test_mask_returns_not_set_for_empty(self, check_connection_script):
        assert check_connection_script._mask("") == "(not set)"

    def test_exits_0_when_connection_ok(self, tmp_path, check_connection_script, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=real-looking-jwt-token\n")
        monkeypatch.setenv("TOPREP_AUTH_TOKEN", "real-looking-jwt-token")

        with patch(
            "app.supabase_client.check_connection",
            return_value={"ok": True, "message": "Connected (HTTP 200)."},
        ):
            with pytest.raises(SystemExit) as exc_info:
                check_connection_script.main(["--env", str(env)])
        assert exc_info.value.code == 0

    def test_exits_1_when_connection_fails(self, tmp_path, check_connection_script, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=real-looking-jwt-token\n")
        monkeypatch.setenv("TOPREP_AUTH_TOKEN", "real-looking-jwt-token")

        with patch(
            "app.supabase_client.check_connection",
            return_value={"ok": False, "error": "HTTP 401: Unauthorized"},
        ):
            with pytest.raises(SystemExit) as exc_info:
                check_connection_script.main(["--env", str(env)])
        assert exc_info.value.code == 1

    def test_send_flag_calls_post_event_to_api(self, tmp_path, check_connection_script, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("TOPREP_AUTH_TOKEN=real-looking-jwt-token\n")
        monkeypatch.setenv("TOPREP_AUTH_TOKEN", "real-looking-jwt-token")

        with (
            patch(
                "app.supabase_client.check_connection",
                return_value={"ok": True, "message": "Connected."},
            ),
            patch(
                "dealmaker_generator.post_event_to_api",
                return_value=(True, "status=200"),
            ) as mock_post,
        ):
            with pytest.raises(SystemExit) as exc_info:
                check_connection_script.main(["--env", str(env), "--send"])

        assert exc_info.value.code == 0
        mock_post.assert_called_once()

    def test_full_token_never_printed(self, tmp_path, check_connection_script, monkeypatch, capsys):
        long_token = "B" * 200
        env = tmp_path / ".env"
        env.write_text(f"TOPREP_AUTH_TOKEN={long_token}\n")
        monkeypatch.setenv("TOPREP_AUTH_TOKEN", long_token)

        with patch(
            "app.supabase_client.check_connection",
            return_value={"ok": True, "message": "Connected."},
        ):
            with pytest.raises(SystemExit):
                check_connection_script.main(["--env", str(env)])

        captured = capsys.readouterr()
        assert long_token not in captured.out
        assert long_token not in captured.err
