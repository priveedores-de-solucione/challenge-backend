#!/usr/bin/env python3
"""
reset_wins.py — reset specific wallets to exact win/duel counts.
"""
from __future__ import annotations
import os, sys, asyncio, asyncpg
from dotenv import load_dotenv

load_dotenv()

CHAIN_ID        = 677
UPDATE_BALANCES = True
UPDATE_GLOBAL   = True

# wallet → (total_wins, total_duels)
WALLETS = {
    
    "0x3207D4728c32391405C7122E59CCb115A4af31eA": (37, 49),
}
# ──────────────────────────────────────────────────────────────────────────────


async def main(dry_run: bool) -> None:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    wallets = {w.lower(): v for w, v in WALLETS.items()}
    print(f"{len(wallets)} wallets | chain={CHAIN_ID} | dry_run={dry_run}\n")

    conn = await asyncpg.connect(dsn=db_url, ssl="require", statement_cache_size=0)
    try:
        async with conn.transaction():

            if UPDATE_BALANCES:
                print("── challenge_player_balances (leaderboard) ──")
                for wallet, (wins, duels) in wallets.items():
                    if not dry_run:
                        status = await conn.execute(
                            """UPDATE challenge_player_balances
                               SET total_wins  = $1,
                                   total_duels = $2,
                                   updated_at  = NOW()
                               WHERE wallet_address = $3 AND chain_id = $4""",
                            wins, duels, wallet, CHAIN_ID,
                        )
                        print(f"  {wallet}  → wins={wins} duels={duels}  ({status})")
                    else:
                        print(f"  {wallet}  → wins={wins} duels={duels}  (dry run)")

            if UPDATE_GLOBAL:
                print("\n── challenge_players (global profile) ──")
                for wallet, (wins, duels) in wallets.items():
                    if not dry_run:
                        status = await conn.execute(
                            """UPDATE challenge_players
                               SET total_wins  = $1,
                                   total_duels = $2
                               WHERE wallet_address = $3""",
                            wins, duels, wallet,
                        )
                        print(f"  {wallet}  → wins={wins} duels={duels}  ({status})")
                    else:
                        print(f"  {wallet}  → wins={wins} duels={duels}  (dry run)")

            if dry_run:
                raise _Rollback()

    except _Rollback:
        print("\nDRY RUN — rolled back, nothing written.")
    finally:
        await conn.close()


class _Rollback(Exception):
    pass


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))