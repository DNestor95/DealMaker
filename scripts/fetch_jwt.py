#!/usr/bin/env python3
"""Fetch a Supabase user JWT and write it to .env as TOPREP_AUTH_TOKEN.

Cross-platform alternative to ``fetch_supabase_jwt.ps1`` (Windows PowerShell).

Usage::

    python scripts/fetch_jwt.py
    python scripts/fetch_jwt.py --email you@example.com
    python scripts/fetch_jwt.py --env path/to/.env

The script reads TOPREP_API_URL and SUPABASE_ANON_KEY from the target .env
file, prompts for credentials, exchanges them for a JWT via the Supabase
Password grant, and writes the resulting token back to TOPREP_AUTH_TOKEN in
the same .env file.

Security notes
--------------
* The password is collected with ``getpass`` so it is never echoed to the
  terminal.
* Only the first 20 characters of the returned token are printed as a
  confirmation prefix; the full token is never logged or displayed.
* The token is written to .env which is already listed in .gitignore.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path
from urllib import error, request

# ---------------------------------------------------------------------------
# Defaults — safe to hard-code (publishable key; not a secret)
# ---------------------------------------------------------------------------

_DEFAULT_SUPABASE_URL = "https://ahimfdfuuefesgbbnccr.supabase.co"
_DEFAULT_PUBLISHABLE_KEY = "sb_publishable_SABMCFFXgDOvyvTvJWH0_w_qREoAIpS"

def _read_env(path: Path) -> dict[str, str]:
    """Parse a .env file and return a key→value mapping."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _write_env_key(path: Path, key: str, value: str) -> None:
    """Set *key* = *value* in a .env file (update existing line or append)."""
    if not path.exists():
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    content = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^({re.escape(key)}\s*=.*)$", re.MULTILINE)

    if pattern.search(content):
        updated = pattern.sub(f"{key}={value}", content)
    else:
        updated = content.rstrip("\n") + f"\n{key}={value}\n"

    path.write_text(updated, encoding="utf-8")


# ---------------------------------------------------------------------------
# Auth token fetch
# ---------------------------------------------------------------------------

def _supabase_root(api_url: str) -> str:
    """Derive the Supabase project root URL from any configured TOPREP_API_URL."""
    # Strip known path prefixes to get the bare project host.
    for suffix in ("/functions/v1/", "/rest/v1/", "/api/events", "/auth/v1/"):
        idx = api_url.find(suffix)
        if idx != -1:
            api_url = api_url[:idx]
    # Edge Function hosts use *.functions.supabase.co → map back to *.supabase.co
    api_url = re.sub(r"(https://[^.]+)\.functions\.(supabase\.co.*)", r"\1.\2", api_url)
    return api_url.rstrip("/")


def fetch_token(supabase_root: str, anon_key: str, email: str, password: str) -> str:
    """POST to /auth/v1/token?grant_type=password and return the access_token."""
    url = f"{supabase_root}/auth/v1/token?grant_type=password"
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "apikey": anon_key,
    }
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("error_description") or json.loads(raw).get("message") or raw
        except Exception:
            msg = raw
        print(f"[fetch_jwt] ✗ HTTP {exc.code}: {msg}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[fetch_jwt] ✗ Request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    token = data.get("access_token")
    if not token:
        print(f"[fetch_jwt] ✗ No access_token in response: {list(data.keys())}", file=sys.stderr)
        sys.exit(1)
    return token


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a Supabase user JWT and save it to .env as TOPREP_AUTH_TOKEN.",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the .env file (default: .env in the current directory).",
    )
    parser.add_argument("--email", default="", help="Supabase user email address.")
    args = parser.parse_args(argv)

    env_path = Path(args.env)
    if not env_path.exists():
        example = env_path.parent / ".env.example"
        if example.exists():
            print(f"[fetch_jwt] .env not found — copy {example} to {env_path} first.")
        else:
            print(f"[fetch_jwt] .env not found at {env_path}.")
        sys.exit(1)

    env_map = _read_env(env_path)

    # Resolve Supabase root URL
    api_url = env_map.get("TOPREP_API_URL", "").strip()
    if not api_url:
        # Fall back to the hard-coded TopRep Supabase URL
        api_url = _DEFAULT_SUPABASE_URL
        print(f"[fetch_jwt] TOPREP_API_URL not set — using default TopRep Supabase URL.")

    supabase_root = _supabase_root(api_url)

    anon_key = (
        env_map.get("SUPABASE_ANON_KEY", "").strip()
        or env_map.get("VITE_SUPABASE_PUBLISHABLE_DEFAULT_KEY", "").strip()
        or _DEFAULT_PUBLISHABLE_KEY
    )

    print(f"[fetch_jwt] Supabase endpoint: {supabase_root}")

    email = args.email.strip() or input("Email: ").strip()
    password = getpass.getpass("Password: ")

    print("[fetch_jwt] Fetching token…")
    token = fetch_token(supabase_root, anon_key, email, password)

    _write_env_key(env_path, "TOPREP_AUTH_TOKEN", token)

    prefix = token[:20] if len(token) >= 20 else token
    print(f"[fetch_jwt] ✓ TOPREP_AUTH_TOKEN written to {env_path}")
    print(f"[fetch_jwt]   Token prefix: {prefix}…")


if __name__ == "__main__":
    main()
