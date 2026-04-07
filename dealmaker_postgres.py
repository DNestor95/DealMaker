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
                    cur.execute(
                        """
                        SELECT id::text
                        FROM profiles
                        WHERE id = ANY(%s)
                        """,
                        (unique_rep_ids,),
                    )
                    existing_profile_ids = {str(row[0]) for row in cur.fetchall()}

                    missing_rep_ids = [rep_id for rep_id in unique_rep_ids if rep_id not in existing_profile_ids]

                    fallback_profile_ids: list[str] = []
                    if missing_rep_ids:
                        cur.execute(
                            """
                            SELECT id::text, email
                            FROM auth.users
                            ORDER BY created_at ASC
                            """
                        )
                        auth_users = [(str(user_id), str(email)) for user_id, email in cur.fetchall() if user_id]

                        if not auth_users:
                            return 0, [
                                "No auth.users rows available to satisfy events.sales_rep_id -> profiles.id FK. "
                                "Create at least one user/profile or pass explicit valid --sales-rep-ids."
                            ]

                        cur.executemany(
                            """
                            INSERT INTO profiles (id, email, role)
                            VALUES (%s::uuid, %s, 'sales_rep')
                            ON CONFLICT (id) DO NOTHING
                            """,
                            auth_users,
                        )
                        fallback_profile_ids = [user_id for user_id, _ in auth_users]

                    fallback_index = 0
                    for rep_id in unique_rep_ids:
                        if rep_id in existing_profile_ids:
                            rep_id_map[rep_id] = rep_id
                        elif fallback_profile_ids:
                            rep_id_map[rep_id] = fallback_profile_ids[fallback_index % len(fallback_profile_ids)]
                            fallback_index += 1
                        else:
                            rep_id_map[rep_id] = rep_id

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