from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from dealmaker_postgres import clear_public_tables


def load_env_file(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1]

        os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear all rows from every table in the public schema.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file()
    database_url = os.getenv("DATABASE_URL") or os.getenv("TOPREP_DATABASE_URL")

    if not args.yes:
        answer = input(
            "This will permanently delete all rows from public schema tables. Continue? [y/N]: "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    result = clear_public_tables(database_url)
    if not result.get("ok"):
        print(f"Error: {result.get('error', 'Unknown error')}")
        return 1

    print(result.get("message", "Database cleared."))
    for table in result.get("tables", []):
        print(f"- {table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
