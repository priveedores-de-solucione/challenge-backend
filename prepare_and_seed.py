#!/usr/bin/env python3
"""
Export the user_profiles table from Supabase (Postgres) to CSV.

Usage:
    export DATABASE_URL="postgresql://user:pass@host:port/dbname"
    python export_user_profiles.py [output.csv]

If DATABASE_URL isn't set, falls back to individual PG* env vars
(PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE) or SUPABASE_DB_URL.
"""

import asyncio
import csv
import os
import sys
from datetime import datetime

import asyncpg

TABLE_NAME = "user_profiles"
BATCH_SIZE = 1000


def get_db_url() -> str:
    url = (
        os.getenv("DATABASE_URL","postgresql://postgres.sdivyxgmyrjevjzufodz:Damilare1%26%40@aws-0-us-east-1.pooler.supabase.com:6543/postgres")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("SUPABASE_DATABASE_URL")
    )
    if url:
        return url

    host = os.getenv("PGHOST")
    if host:
        user = os.getenv("PGUSER", "postgres")
        password = os.getenv("PGPASSWORD", "")
        port = os.getenv("PGPORT", "5432")
        dbname = os.getenv("PGDATABASE", "postgres")
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    print(
        "ERROR: No DB connection info found. Set DATABASE_URL "
        "(e.g. postgresql://postgres:PASS@db.<project-ref>.supabase.co:5432/postgres) "
        "or SUPABASE_DB_URL.",
        file=sys.stderr,
    )
    sys.exit(1)


async def export_table(output_path: str):
    db_url = get_db_url()

    # asyncpg needs sslmode handled via ssl= param, not in the URL query string
    conn = await asyncpg.connect(db_url, statement_cache_size=0)

    try:
        # Confirm table exists and grab column order
        col_rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            TABLE_NAME,
        )
        if not col_rows:
            print(f"ERROR: table '{TABLE_NAME}' not found in public schema.", file=sys.stderr)
            sys.exit(1)

        columns = [r["column_name"] for r in col_rows]

        total = await conn.fetchval(f"SELECT COUNT(*) FROM {TABLE_NAME}")
        print(f"Exporting {total} rows from {TABLE_NAME}...")

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)

            offset = 0
            written = 0
            while True:
                rows = await conn.fetch(
                    f"SELECT {', '.join(columns)} FROM {TABLE_NAME} "
                    f"ORDER BY 1 LIMIT {BATCH_SIZE} OFFSET {offset}"
                )
                if not rows:
                    break
                for row in rows:
                    writer.writerow(
                        [_serialize(row[c]) for c in columns]
                    )
                written += len(rows)
                offset += BATCH_SIZE
                print(f"  {written}/{total} rows written", end="\r")

        print(f"\nDone. Wrote {written} rows to {output_path}")

    finally:
        await conn.close()


def _serialize(value):
    if value is None:
        return ""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value)
    return value


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "user_profiles_export.csv"
    asyncio.run(export_table(out))