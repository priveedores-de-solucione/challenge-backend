"""
rename_synced_users.py
──────────────────────
One-off migration: re-assign usernames for wallets that received an
auto-generated v1 name (the obvious AdjNoun pool names, or UserXXXX
fallbacks), WITHOUT touching names users chose themselves.

How it decides a profile is safe to rename
──────────────────────────────────────────
A username is treated as auto-assigned iff it is:
  • an exact member of the deterministic v1 pool (seed 42, first 500), or
  • matches  ^User[0-9A-Fa-f]{4}$    (v1 overflow fallback), or
  • matches  ^Player[0-9A-Fa-f]{6}$  (older fallback pattern)

Anything else — including a pool name the user later edited by one
character — is left alone. If a user happened to hand-pick a name that's
literally in the 500-name pool, pass --exclude 0xwallet1,0xwallet2 to
protect them.

Usage
─────
  # Preview only — prints old → new mapping, writes nothing
  python rename_synced_users.py --dry-run

  # Apply
  python rename_synced_users.py

  # Apply, protecting specific wallets
  python rename_synced_users.py --exclude 0xabc...,0xdef...

Env (same as onchain_sync):
  DATABASE_URL / SUPABASE_DB_URL
  SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY

Tables updated
──────────────
  • user_profiles       (Supabase)  — username
  • challenge_players   (Postgres)  — username, only where it still equals
                                      the old auto name (won't clobber edits)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re

import asyncpg
from dotenv import load_dotenv
from supabase import create_client

from username_pool import legacy_pool_names, generate_unique_username

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

FALLBACK_RE = re.compile(r"^(User[0-9A-Fa-f]{4}|Player[0-9A-Fa-f]{6})$")


def fetch_all_profiles(supabase) -> list[dict]:
    """Page through user_profiles (Supabase caps selects at 1000 rows)."""
    rows: list[dict] = []
    page, size = 0, 1000
    while True:
        res = (
            supabase.table("user_profiles")
            .select("wallet_address, username")
            .range(page * size, page * size + size - 1)
            .execute()
        )
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < size:
            return rows
        page += 1


async def main() -> None:
    parser = argparse.ArgumentParser(description="Re-assign auto-generated usernames")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes, write nothing")
    parser.add_argument("--exclude", default="", help="Comma-separated wallets to skip")
    args = parser.parse_args()

    load_dotenv()
    db_url   = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    supa_url = os.getenv("NEXT_PUBLIC_SUPABASE_URL") or os.getenv("SUPABASE_URL")
    supa_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not (db_url and supa_url and supa_key):
        raise SystemExit("Set DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY")

    excluded = {w.strip().lower() for w in args.exclude.split(",") if w.strip()}
    supabase = create_client(supa_url, supa_key)
    pool     = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    legacy = legacy_pool_names()
    log.info("Legacy pool rebuilt: %d names", len(legacy))

    profiles = fetch_all_profiles(supabase)
    log.info("Fetched %d profiles", len(profiles))

    # Names already in use — new names must not collide with ANY of these.
    taken: set[str] = {(p["username"] or "").lower() for p in profiles}

    renames: list[tuple[str, str, str]] = []   # (wallet, old, new)
    for p in profiles:
        wallet = (p["wallet_address"] or "").lower()
        old    = p["username"] or ""
        if not wallet or wallet in excluded:
            continue
        if old in legacy or FALLBACK_RE.match(old):
            new = generate_unique_username(wallet, taken)
            taken.add(new.lower())
            renames.append((wallet, old, new))

    log.info("Profiles to rename: %d / %d", len(renames), len(profiles))
    for wallet, old, new in renames[:25]:
        log.info("  %s  %-24s → %s", wallet[:10], old, new)
    if len(renames) > 25:
        log.info("  … and %d more", len(renames) - 25)

    if args.dry_run:
        log.info("DRY RUN — nothing written")
        await pool.close()
        return

    if not renames:
        log.info("Nothing to do")
        await pool.close()
        return

    # ── 1. Supabase user_profiles ─────────────────────────────────────────────
    ok = fail = 0
    for wallet, old, new in renames:
        try:
            # Guard: only update if the username is STILL the old auto name,
            # so a user who edits mid-run isn't clobbered.
            supabase.table("user_profiles") \
                .update({"username": new}) \
                .eq("wallet_address", wallet) \
                .eq("username", old) \
                .execute()
            ok += 1
        except Exception as exc:
            fail += 1
            log.error("user_profiles update failed %s: %s", wallet, exc)
    log.info("user_profiles updated: %d ok, %d failed", ok, fail)

    # ── 2. Postgres challenge_players (same guard) ────────────────────────────
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            UPDATE challenge_players
            SET    username = $2
            WHERE  wallet_address = $1
              AND  username       = $3
            """,
            [(w, new, old) for (w, old, new) in renames],
        )
    log.info("challenge_players updated")

    await pool.close()
    log.info("Done. Renamed %d profiles.", ok)


if __name__ == "__main__":
    asyncio.run(main())