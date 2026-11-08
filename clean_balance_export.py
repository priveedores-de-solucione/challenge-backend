"""
clean_balance_export.py
───────────────────────
1. Read challenge_player_balances_export.csv
2. Drop: total_payouts, total_burns, updated_at
3. Pull all wallets from user_profiles that are NOT already in the export
4. Add them as zero-value rows (username from user_profiles, all numeric = 0)
5. Sort: total_earned DESC → total_wins DESC → total_duels DESC → wallet_address
6. Write cleaned CSV

Env: DATABASE_URL / SUPABASE_DB_URL
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("clean_export")

CHAIN_ID = 677

KEEP_COLS = [
    "wallet_address", "username", "chain_id",
    "game_drops", "reward_drops", "total_earned",
    "total_duels", "total_wins", "qualifying_duels",
    "redeem_badge", "drops_minted",
]

DROP_COLS = {"total_payouts", "total_burns", "updated_at"}

ZERO_ROW_DEFAULTS = {
    "chain_id":        str(CHAIN_ID),
    "game_drops":      "0.000000",
    "reward_drops":    "0.000000",
    "total_earned":    "0.000000",
    "total_duels":     "0",
    "total_wins":      "0",
    "qualifying_duels":"0",
    "redeem_badge":    "False",
    "drops_minted":    "False",
}


def load_export(path: str) -> tuple[list[dict], set[str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # Strip dropped columns
    cleaned = []
    for r in rows:
        cleaned.append({k: r[k] for k in KEEP_COLS if k in r})
    wallets = {r["wallet_address"].lower() for r in cleaned}
    log.info("Loaded %d rows from export (%d unique wallets)", len(cleaned), len(wallets))
    return cleaned, wallets


async def fetch_missing_profiles(
    db_url: str,
    existing_wallets: set[str],
) -> list[dict]:
    """
    Pull wallet_address + username from user_profiles for every wallet
    that is NOT already in the export. Returns list of zero-value dicts.
    """
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT wallet_address, COALESCE(username, '') AS username
                FROM user_profiles
                ORDER BY wallet_address
                """
            )
    finally:
        await pool.close()

    log.info("user_profiles total rows: %d", len(rows))

    new_rows = []
    for r in rows:
        addr = (r["wallet_address"] or "").strip().lower()
        if not addr or addr in existing_wallets:
            continue
        row = {**ZERO_ROW_DEFAULTS, "wallet_address": addr, "username": r["username"] or ""}
        # ensure all KEEP_COLS present
        for col in KEEP_COLS:
            if col not in row:
                row[col] = ""
        new_rows.append(row)

    log.info("New wallets from user_profiles not in export: %d", len(new_rows))
    return new_rows


def sort_key(r: dict):
    return (
        -float(r.get("total_earned") or 0),
        -int(r.get("total_wins") or 0),
        -int(r.get("total_duels") or 0),
        r.get("wallet_address", ""),
    )


async def main():
    parser = argparse.ArgumentParser(description="Clean balance export + add missing user_profiles wallets")
    parser.add_argument("--input",   default="challenge_player_balances_export.csv")
    parser.add_argument("--output",  default="challenge_player_balances_clean.csv")
    parser.add_argument("--no-db",   action="store_true",
                        help="Skip DB fetch — only strip columns and re-sort")
    args = parser.parse_args()

    load_dotenv()
    db_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not args.no_db and not db_url:
        raise SystemExit("Set DATABASE_URL in env / .env (or pass --no-db to skip DB fetch)")

    # 1. Load and clean existing export
    rows, existing_wallets = load_export(args.input)

    # 2. Fetch missing user_profiles wallets
    new_rows: list[dict] = []
    if not args.no_db:
        new_rows = await fetch_missing_profiles(db_url, existing_wallets)
    else:
        log.info("--no-db: skipping user_profiles fetch")

    # 3. Merge
    all_rows = rows + new_rows
    log.info("Total rows after merge: %d", len(all_rows))

    # 4. Sort
    all_rows.sort(key=sort_key)

    # 5. Write
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=KEEP_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    log.info("Written to %s  (%d rows)", args.output, len(all_rows))
    log.info(
        "  existing wallets: %d  |  new zero-rows added: %d",
        len(rows), len(new_rows),
    )


if __name__ == "__main__":
    asyncio.run(main())