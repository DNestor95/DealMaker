from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any


def database_url_from_env() -> str:
    return os.getenv("TOPREP_DATABASE_URL") or os.getenv("DATABASE_URL", "")


def is_postgres_dsn(value: str) -> bool:
    trimmed = value.strip().lower()
    return trimmed.startswith("postgresql://") or trimmed.startswith("postgres://")


def check_database_connection(database_url: str | None = None) -> dict[str, Any]:
    dsn = (database_url or database_url_from_env()).strip()
    if not dsn:
        return {"ok": False, "error": "DATABASE_URL not configured."}

    try:
        import psycopg
    except ImportError:
        return {"ok": False, "error": "psycopg is not installed."}

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("select current_database(), version()")
                database_name, version = cur.fetchone()
        return {
            "ok": True,
            "message": f"Connected to Postgres database '{database_name}'.",
            "server": str(version).split(",", 1)[0],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def insert_events(database_url: str, rows: Iterable[Mapping[str, Any]]) -> tuple[int, list[str]]:
    dsn = database_url.strip()
    if not dsn:
        return 0, ["DATABASE_URL not configured."]

    materialized_rows = list(rows)
    if not materialized_rows:
        return 0, []

    try:
        import psycopg
    except ImportError:
        return 0, ["psycopg is not installed."]

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO events (sales_rep_id, type, payload, created_at)
                    VALUES (%s::uuid, %s, %s::jsonb, %s::timestamptz)
                    """,
                    [
                        (
                            str(row["sales_rep_id"]),
                            str(row["type"]),
                            json.dumps(row.get("payload", {}), separators=(",", ":")),
                            str(row["created_at"]),
                        )
                        for row in materialized_rows
                    ],
                )
            conn.commit()
        return len(materialized_rows), []
    except Exception as exc:
        return 0, [str(exc)]