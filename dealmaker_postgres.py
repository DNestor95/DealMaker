from __future__ import annotations

import json
import os
import uuid
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
                # Keep events FK-safe on freshly cleared databases. Profiles.id
                # also references auth.users(id), so generated UUIDs cannot be
                # inserted directly into profiles.
                rep_ids: list[str] = []
                for row in materialized_rows:
                    rep_id = str(row["sales_rep_id"])
                    try:
                        rep_ids.append(str(uuid.UUID(rep_id)))
                    except ValueError:
                        # Let the event insert surface invalid UUID format.
                        continue

                rep_id_map: dict[str, str] = {}
                if rep_ids:
                    unique_rep_ids = sorted(set(rep_ids))
                    # Resolve DealMaker employee UUIDs → internal profile UUIDs
                    # via reps.employee_external_id (set during onboarding).
                    cur.execute(
                        """
                        SELECT employee_external_id::text, id::text
                        FROM reps
                        WHERE employee_external_id = ANY(%s)
                        """,
                        (unique_rep_ids,),
                    )
                    rep_id_map = {str(ext_id): str(profile_id) for ext_id, profile_id in cur.fetchall()}

                    unmapped = [r for r in unique_rep_ids if r not in rep_id_map]
                    if unmapped:
                        return 0, [
                            f"DealMaker rep ID(s) not mapped via employee_external_id: {unmapped}. "
                            "Set employee_external_id on the corresponding reps rows."
                        ]

                cur.executemany(
                    """
                    INSERT INTO events (sales_rep_id, type, payload, created_at)
                    VALUES (%s::uuid, %s, %s::jsonb, %s::timestamptz)
                    """,
                    [
                        (
                            rep_id_map.get(str(row["sales_rep_id"]), str(row["sales_rep_id"])),
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


def clear_events_for_reps(database_url: str, rep_ids: list[str]) -> dict[str, Any]:
    """Delete all events rows whose sales_rep_id is in `rep_ids`.

    Used by the store-reset flow to wipe only that store's data without
    touching other stores' events or non-events tables.
    """
    dsn = (database_url or database_url_from_env()).strip()
    if not dsn:
        return {"ok": False, "error": "DATABASE_URL not configured."}
    if not rep_ids:
        return {"ok": True, "deleted": 0}

    try:
        import psycopg
    except ImportError:
        return {"ok": False, "error": "psycopg is not installed."}

    try:
        with psycopg.connect(dsn, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM events WHERE sales_rep_id = ANY(%s::uuid[])",
                    ([str(r) for r in rep_ids],),
                )
                deleted = cur.rowcount
            conn.commit()
        return {"ok": True, "deleted": deleted}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def clear_public_tables(database_url: str | None = None) -> dict[str, Any]:
    """Delete all rows from every table in the public schema.

    This is schema-aware: it enumerates current tables at runtime and truncates
    them in one statement with CASCADE, so newly added tables are included
    automatically.
    """
    dsn = (database_url or database_url_from_env()).strip()
    if not dsn:
        return {"ok": False, "error": "DATABASE_URL not configured."}

    try:
        import psycopg
    except ImportError:
        return {"ok": False, "error": "psycopg is not installed."}

    try:
        with psycopg.connect(dsn, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select schemaname, tablename
                    from pg_tables
                    where schemaname = 'public'
                    order by tablename
                    """
                )
                tables = [(str(s), str(t)) for s, t in cur.fetchall()]

                if not tables:
                    return {"ok": True, "tables": [], "message": "No public tables found."}

                table_list = ", ".join(f'"{s}"."{t}"' for s, t in tables)
                cur.execute(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE")

                uncleared: list[dict[str, Any]] = []
                for s, t in tables:
                    cur.execute(f'SELECT COUNT(*) FROM "{s}"."{t}"')
                    cnt = int(cur.fetchone()[0])
                    if cnt:
                        uncleared.append({"schema": s, "table": t, "rows": cnt})

            conn.commit()

        if uncleared:
            return {
                "ok": False,
                "error": "Some tables were not fully cleared.",
                "tables": [f"{s}.{t}" for s, t in tables],
                "remaining": uncleared,
            }

        return {
            "ok": True,
            "tables": [f"{s}.{t}" for s, t in tables],
            "message": f"Cleared {len(tables)} table(s) in public schema.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}