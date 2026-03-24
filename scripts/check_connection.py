#!/usr/bin/env python3
"""Validate DealMaker ↔ Supabase connectivity and optionally send a test event.

Usage::

    python scripts/check_connection.py            # connectivity check only
    python scripts/check_connection.py --send     # check + send one test event
    python scripts/check_connection.py --env path/to/.env

Exit codes
----------
0 — all checks passed
1 — one or more checks failed

The script loads .env automatically so you do not need to export env vars
manually before running.

Security notes
--------------
* TOPREP_AUTH_TOKEN is read from .env / environment variables and never
  printed in full.  Only the first 12 characters are shown as a confirmation
  prefix so you can verify the correct token is loaded without exposing it.
* SUPABASE_SERVICE_ROLE_KEY is never read or used by this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so app and generator modules are importable
# when the script is run from any working directory.
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_env(path: Path) -> None:
    """Load a .env file into os.environ (only for keys not already set)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _mask(token: str, visible: int = 12) -> str:
    """Return a masked representation of a secret token."""
    if not token:
        return "(not set)"
    prefix = token[:visible]
    return f"{prefix}…"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_env(env_path: Path) -> bool:
    """Verify that TOPREP_AUTH_TOKEN is configured."""
    token = os.getenv("TOPREP_AUTH_TOKEN", "").strip()
    if not token or token == "your-user-jwt":
        print("✗ TOPREP_AUTH_TOKEN is not set.")
        print(f"  → Run:  python scripts/fetch_jwt.py --env {env_path}")
        print("    or set it manually in .env")
        return False

    api_url = os.getenv("TOPREP_API_URL", "(default TopRep Supabase URL)")
    print(f"✓ TOPREP_AUTH_TOKEN is configured  ({_mask(token)})")
    print(f"  Target endpoint: {api_url}")
    return True


def check_connectivity() -> bool:
    """Call check_connection() from supabase_client and report result."""
    from app.supabase_client import check_connection  # noqa: PLC0415

    result = check_connection()
    if result.get("ok"):
        print(f"✓ Connection OK — {result.get('message', '')}")
        return True

    print(f"✗ Connection failed — {result.get('error', 'unknown error')}")

    # Provide targeted hints for common errors
    error_msg = result.get("error", "")
    if "401" in error_msg or "Authentication" in error_msg:
        print("  → Your TOPREP_AUTH_TOKEN may be expired or invalid.")
        print("    Run:  python scripts/fetch_jwt.py  to refresh it.")
    elif "Name or service not known" in error_msg or "getaddrinfo" in error_msg:
        print("  → Cannot reach Supabase — check your internet connection.")
    return False


def send_test_event() -> bool:
    """Send a single minimal test event to confirm end-to-end write works."""
    from dealmaker_generator import Event, post_event_to_api, to_iso  # noqa: PLC0415
    from app.supabase_client import (  # noqa: PLC0415
        _TOPREP_SUPABASE_URL,
        _anon_key,
        _api_url,
    )

    api_url_base = _api_url().rstrip("/")
    anon_key = _anon_key()
    auth_token = os.getenv("TOPREP_AUTH_TOKEN", "")

    # Build the target URL for the test event
    from urllib.parse import urlparse  # noqa: PLC0415
    parsed = urlparse(api_url_base)
    path = parsed.path

    if (
        path.startswith("/functions/v1/")
        or path.startswith("/api/events")
        or path.startswith("/rest/v1/")
    ):
        post_url = api_url_base
    else:
        post_url = f"{parsed.scheme}://{parsed.netloc}/api/events"

    now = datetime.now(timezone.utc)
    event = Event(
        sales_rep_id=str(uuid.uuid4()),
        type="activity.completed",
        payload={
            "activity_id": str(uuid.uuid4()),
            "deal_id": str(uuid.uuid4()),
            "activity_type": "call",
            "outcome": "connected",
        },
        created_at=to_iso(now),
    )

    print(f"  Sending test event to: {post_url}")
    ok, detail = post_event_to_api(
        event=event,
        api_url=post_url,
        auth_token=auth_token,
        supabase_apikey=anon_key,
    )

    if ok:
        print(f"✓ Test event delivered successfully  ({detail})")
        return True

    print(f"✗ Test event failed — {detail}")
    if "401" in detail or "Authentication" in detail:
        print("  → Token rejected.  Run: python scripts/fetch_jwt.py  to refresh.")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate DealMaker → Supabase connectivity.\n"
            "Exits 0 on success, 1 on failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the .env file (default: .env in the current directory).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Also send one minimal test event to verify end-to-end write.",
    )
    args = parser.parse_args(argv)

    env_path = Path(args.env)
    _load_env(env_path)

    print("=== DealMaker connectivity check ===\n")

    passed = True

    passed = check_env(env_path) and passed
    print()

    if not passed:
        print("⚠  Fix the above issues, then re-run this script.")
        sys.exit(1)

    passed = check_connectivity() and passed
    print()

    if args.send:
        passed = send_test_event() and passed
        print()

    if passed:
        print("=== All checks passed ✓ ===")
        sys.exit(0)
    else:
        print("=== One or more checks failed ✗ ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
