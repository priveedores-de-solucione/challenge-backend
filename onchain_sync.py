"""
onchain_sync.py
───────────────
Dual-chain incremental event scanner for FaucetDrops.

Scans BOTH Celo Mainnet (42220) and Botchain (677) independently,
persisting progress in sync_state with a per-chain key so each chain's
checkpoint is managed separately.  A single run_sync() call processes
both chains and returns a combined result.

First run      → full scan from each contract's deploy block → latest.
Subsequent run → last_synced_block+1 → latest (fast, seconds not minutes).

Background scheduler:
    Fires automatically every SYNC_INTERVAL_HOURS inside the FastAPI process.

Call from FastAPI:
    from onchain_sync import run_sync, start_background_scheduler
    result = await run_sync(pool, supabase)

Run as a standalone script:
    python onchain_sync.py [--full] [--chain celo|botchain|both]

Tables written
──────────────
  • sync_state                  — persists last scanned block per (chain, key)
  • user_profiles               — new wallets get auto username from pool
  • challenge_players           — total_duels, total_wins per wallet (additive merge)
  • challenge_player_balances   — game_drops, reward_drops, drops_minted per (wallet, chain_id)
  • player_activity_log         — one EVENT_REGISTER row per newly discovered wallet
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from username_pool import assign_username

logger = logging.getLogger(__name__)

# ── Schedule ──────────────────────────────────────────────────────────────────

SYNC_INTERVAL_HOURS = 2

# ── Chain constants ───────────────────────────────────────────────────────────

CELO_CHAIN_ID     = 42220
BOTCHAIN_CHAIN_ID = 677

# forno.celo.org rejects eth_getLogs ranges wider than 5 000 blocks.
LOG_CHUNK   = 5_000
CONCURRENCY = 10

# ── Per-chain deploy blocks ───────────────────────────────────────────────────
# These are the first blocks containing each contract's bytecode.
# Adjust BOTCHAIN_* when you have the actual deploy blocks.

CELO_QUIZ_HUB_DEPLOY_BLOCK    = 71_127_962
CELO_DROPS_TOKEN_DEPLOY_BLOCK = 71_127_711

# Botchain: set via env vars; defaults are conservative (block 1 = full scan).
BOTCHAIN_QUIZ_HUB_DEPLOY_BLOCK    = int(os.getenv("BOTCHAIN_QUIZ_HUB_DEPLOY_BLOCK",    "1"))
BOTCHAIN_DROPS_TOKEN_DEPLOY_BLOCK = int(os.getenv("BOTCHAIN_DROPS_TOKEN_DEPLOY_BLOCK", "1"))


# ── Chain descriptors (all config in one place) ───────────────────────────────

@dataclass(frozen=True)
class ChainSpec:
    chain_id:               int
    name:                   str
    rpc_url:                str
    quiz_hub_addr:          str
    drops_token_addr:       str
    infra_addrs:            tuple          # extra addresses to exclude from stats
    quiz_hub_deploy_block:  int
    drops_token_deploy_block: int
    sync_state_key:         str
    poa:                    bool = False   # inject ExtraDataToPOAMiddleware


def _build_chain_specs() -> list[ChainSpec]:
    celo_quiz_hub    = os.getenv("QUIZ_HUB_CONTRACT_CELO",    os.getenv("QUIZ_HUB_CONTRACT",    "0xd73170170E002b45eA4AA51e7E93302D61c30173"))
    celo_drops_token = os.getenv("DROPS_TOKEN_CONTRACT_CELO", os.getenv("DROPS_TOKEN_CONTRACT", "0x9825670865B896738CF8E6c98d093aD5b40F0A11"))
    celo_redeem_pool = os.getenv("DROPS_REDEEM_POOL_CELO",    os.getenv("DROPS_REDEEM_POOL",    "0x636685bCFeEf6Baeb05872f01e69405077eAF633"))
    backend_wallet   = os.getenv("NEXT_PUBLIC_BACKEND_WALLET", "")
    zero_addr        = "0x0000000000000000000000000000000000000000"

    bot_quiz_hub    = os.getenv("QUIZ_HUB_CONTRACT_BOTCHAIN",    "0xE7F217A8447087600C1CBFb63192586edFa619fe")
    bot_drops_token = os.getenv("DROPS_TOKEN_CONTRACT_BOTCHAIN", "0xBAd791F200f1F8Fb639d83125FcF732F5f6eCD03")
    bot_redeem_pool = os.getenv("DROPS_REDEEM_POOL_BOTCHAIN",    "0x9825670865B896738CF8E6c98d093aD5b40F0A11")
    # Default matches chain_config.py — set BOTCHAIN_SYNC_DISABLED=1 to opt out.
    bot_rpc = "" if os.getenv("BOTCHAIN_SYNC_DISABLED") else \
              os.getenv("BOTCHAIN_RPC_URL", "https://rpc.botchain.ai")
              
    specs: list[ChainSpec] = [
        ChainSpec(
            chain_id               = CELO_CHAIN_ID,
            name                   = "Celo",
            rpc_url                = os.getenv("CELO_RPC_URL", "https://forno.celo.org"),
            quiz_hub_addr          = celo_quiz_hub,
            drops_token_addr       = celo_drops_token,
            infra_addrs            = tuple(filter(None, [celo_quiz_hub, celo_drops_token, celo_redeem_pool, backend_wallet, zero_addr])),
            quiz_hub_deploy_block  = CELO_QUIZ_HUB_DEPLOY_BLOCK,
            drops_token_deploy_block = CELO_DROPS_TOKEN_DEPLOY_BLOCK,
            sync_state_key         = "celo_onchain_users_v2",
            poa                    = False,
        ),
    ]

    # Only add Botchain if RPC is configured
    if bot_rpc:
        specs.append(ChainSpec(
            chain_id               = BOTCHAIN_CHAIN_ID,
            name                   = "Botchain",
            rpc_url                = bot_rpc,
            quiz_hub_addr          = bot_quiz_hub,
            drops_token_addr       = bot_drops_token,
            infra_addrs            = tuple(filter(None, [bot_quiz_hub, bot_drops_token, bot_redeem_pool, backend_wallet, zero_addr])),
            quiz_hub_deploy_block  = BOTCHAIN_QUIZ_HUB_DEPLOY_BLOCK,
            drops_token_deploy_block = BOTCHAIN_DROPS_TOKEN_DEPLOY_BLOCK,
            sync_state_key         = "botchain_onchain_users_v2",
            poa                    = True,   # Botchain is POA
        ))
    else:
        logger.info("BOTCHAIN_RPC_URL not set — Botchain sync disabled")

    return specs


# ── Minimal event ABIs ────────────────────────────────────────────────────────

QUIZ_HUB_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "quizId",         "type": "bytes32"},
            {"indexed": True,  "name": "player1",        "type": "address"},
            {"indexed": True,  "name": "player2",        "type": "address"},
            {"indexed": False, "name": "stakePerPlayer", "type": "uint256"},
        ],
        "name": "QuizRegistered",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "quizId", "type": "bytes32"},
            {"indexed": True,  "name": "winner", "type": "address"},
            {"indexed": False, "name": "payout", "type": "uint256"},
        ],
        "name": "WinnerSet",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "quizId",     "type": "bytes32"},
            {"indexed": True,  "name": "player1",    "type": "address"},
            {"indexed": True,  "name": "player2",    "type": "address"},
            {"indexed": False, "name": "refundEach", "type": "uint256"},
        ],
        "name": "TieDeclared",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "quizId",  "type": "bytes32"},
            {"indexed": True,  "name": "creator", "type": "address"},
        ],
        "name": "QuizCreated",
        "type": "event",
    },
]

DROPS_TOKEN_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "from",  "type": "address"},
            {"indexed": True,  "name": "to",    "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "user",     "type": "address"},
            {"indexed": False, "name": "amount",   "type": "uint256"},
            {"indexed": False, "name": "rewardId", "type": "string"},
        ],
        "name": "PointsRedeemed",
        "type": "event",
    },
]


# ── Per-wallet stats accumulator ──────────────────────────────────────────────

@dataclass
class WalletStats:
    total_duels:        int   = 0
    total_wins:         int   = 0
    total_ties:         int   = 0
    game_drops_wei:     int   = 0    # minted (from=0x0 Transfer)
    reward_drops_wei:   int   = 0    # PointsRedeemed
    burns_wei:          int   = 0    # burned (to=0x0 Transfer — stake)
    payouts_wei:        int   = 0    # WinnerSet payout amount


# ── sync_state table helpers ──────────────────────────────────────────────────

async def _ensure_sync_state_table(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            key          TEXT PRIMARY KEY,
            last_block   BIGINT      NOT NULL DEFAULT 0,
            last_run_at  TIMESTAMPTZ,
            meta         JSONB
        )
        """
    )


async def _get_last_block(pool, key: str) -> int:
    async with pool.acquire() as conn:
        await _ensure_sync_state_table(conn)
        row = await conn.fetchrow(
            "SELECT last_block FROM sync_state WHERE key = $1", key,
        )
    return int(row["last_block"]) if row else 0


async def _save_last_block(pool, key: str, block: int, meta: dict) -> None:
    import json as _json
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sync_state (key, last_block, last_run_at, meta)
            VALUES ($1, $2, NOW(), $3::jsonb)
            ON CONFLICT (key) DO UPDATE
                SET last_block  = EXCLUDED.last_block,
                    last_run_at = EXCLUDED.last_run_at,
                    meta        = EXCLUDED.meta
            """,
            key, block, _json.dumps(meta),
        )


# ── Web3 helpers ──────────────────────────────────────────────────────────────

def _make_w3(spec: ChainSpec) -> Web3:
    w3 = Web3(Web3.HTTPProvider(spec.rpc_url, request_kwargs={"timeout": 30}))
    if spec.poa:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def _build_ranges(from_block: int, to_block: int) -> list[tuple[int, int]]:
    ranges, cur = [], from_block
    while cur <= to_block:
        end = min(cur + LOG_CHUNK - 1, to_block)
        ranges.append((cur, end))
        cur = end + 1
    return ranges


MIN_CHUNK = 500   # smallest range we'll split down to before declaring failure


async def _fetch_logs_chunked(
    loop: asyncio.AbstractEventLoop,
    w3: Web3,
    contract: Any,
    event_name: str,
    from_block: int,
    to_block: int,
    failed_ranges: list[tuple[int, int]] | None = None,
) -> list:
    """
    Fetch logs in LOG_CHUNK windows. A failing window is retried by splitting
    it in half, down to MIN_CHUNK blocks; ranges that still fail are appended
    to `failed_ranges` (if provided) so the caller can refuse to advance the
    checkpoint past them — previously failures were only WARN-logged and the
    checkpoint moved on anyway, permanently skipping those blocks.
    """
    if from_block > to_block:
        return []
    ranges = _build_ranges(from_block, to_block)
    event  = getattr(contract.events, event_name)
    all_logs: list = []

    def _fetch_range(fr: int, to: int, results: list) -> None:
        """Fetch one range, splitting in half on failure."""
        try:
            results.extend(event.get_logs(from_block=fr, to_block=to))
            return
        except Exception as exc:
            if (to - fr + 1) <= MIN_CHUNK:
                logger.warning(
                    "Log fetch PERMANENTLY failed blocks %d–%d event=%s: %s",
                    fr, to, event_name, exc,
                )
                if failed_ranges is not None:
                    failed_ranges.append((fr, to))
                return
            mid = (fr + to) // 2
            logger.info(
                "Log fetch failed blocks %d–%d event=%s (%s) — splitting",
                fr, to, event_name, str(exc)[:80],
            )
            _fetch_range(fr, mid, results)
            _fetch_range(mid + 1, to, results)

    for i in range(0, len(ranges), CONCURRENCY):
        batch = ranges[i : i + CONCURRENCY]

        def _get_batch(batch=batch):
            results: list = []
            for (fr, to) in batch:
                _fetch_range(fr, to, results)
            return results

        all_logs.extend(await loop.run_in_executor(None, _get_batch))

    return all_logs


# ── Per-chain event scan ──────────────────────────────────────────────────────

async def scan_chain_events(
    spec: ChainSpec,
    from_block: int,
) -> tuple[dict[str, WalletStats], int, list[tuple[int, int]]]:
    """
    Scan one chain from `from_block` to chain tip.

    Returns (stats_dict, latest_block_number, failed_ranges).
    stats_dict values are INCREMENTAL — only the delta since from_block.
    failed_ranges lists block windows the RPC could not serve even after
    split-retries; the caller must not checkpoint past the earliest one.
    """
    from bot_config import BOT_WALLETS

    loop    = asyncio.get_event_loop()
    w3      = await loop.run_in_executor(None, lambda: _make_w3(spec))
    latest  = await loop.run_in_executor(None, lambda: w3.eth.block_number)

    failed_ranges: list[tuple[int, int]] = []

    if from_block > latest:
        logger.info(
            "%s scan: already at tip (from=%d latest=%d) — nothing to do",
            spec.name, from_block, latest,
        )
        return {}, latest, failed_ranges

    logger.info(
        "%s scan: blocks %d → %d  (%d blocks)",
        spec.name, from_block, latest, latest - from_block,
    )

    quiz_hub    = w3.eth.contract(
        address = Web3.to_checksum_address(spec.quiz_hub_addr),
        abi     = QUIZ_HUB_EVENTS_ABI,
    )
    drops_token = w3.eth.contract(
        address = Web3.to_checksum_address(spec.drops_token_addr),
        abi     = DROPS_TOKEN_EVENTS_ABI,
    )

    infra: set[str] = (
        {a.lower() for a in spec.infra_addrs if a and a.startswith("0x")}
        | {b.lower() for b in BOT_WALLETS}
    )

    def skip(addr: str) -> bool:
        return not addr or addr.lower() in infra

    stats: dict[str, WalletStats] = defaultdict(WalletStats)
    zero = "0x0000000000000000000000000000000000000000"

    # ── QuizCreated → creator seen on this chain ──────────────────────────────
    created_logs = await _fetch_logs_chunked(loop, w3, quiz_hub, "QuizCreated", from_block, latest, failed_ranges)
    logger.info("  %s QuizCreated: %d", spec.name, len(created_logs))
    for log in created_logs:
        creator = (log["args"].get("creator") or "").lower()
        if not skip(creator):
            # Just ensure the wallet appears in stats so it gets a user_profile row.
            _ = stats[creator]

    # ── QuizRegistered → total_duels ──────────────────────────────────────────
    reg_logs = await _fetch_logs_chunked(loop, w3, quiz_hub, "QuizRegistered", from_block, latest, failed_ranges)
    logger.info("  %s QuizRegistered: %d", spec.name, len(reg_logs))
    for log in reg_logs:
        for key in ("player1", "player2"):
            p = (log["args"].get(key) or "").lower()
            if not skip(p):
                stats[p].total_duels += 1

    # ── WinnerSet → total_wins + payouts ──────────────────────────────────────
    win_logs = await _fetch_logs_chunked(loop, w3, quiz_hub, "WinnerSet", from_block, latest, failed_ranges)
    logger.info("  %s WinnerSet: %d", spec.name, len(win_logs))
    for log in win_logs:
        w_addr = (log["args"].get("winner") or "").lower()
        if not skip(w_addr):
            stats[w_addr].total_wins  += 1
            stats[w_addr].payouts_wei += int(log["args"].get("payout") or 0)

    # ── TieDeclared → total_ties ──────────────────────────────────────────────
    tie_logs = await _fetch_logs_chunked(loop, w3, quiz_hub, "TieDeclared", from_block, latest, failed_ranges)
    logger.info("  %s TieDeclared: %d", spec.name, len(tie_logs))
    for log in tie_logs:
        for key in ("player1", "player2"):
            p = (log["args"].get(key) or "").lower()
            if not skip(p):
                stats[p].total_ties += 1

    # ── Transfer events ───────────────────────────────────────────────────────
    transfer_logs = await _fetch_logs_chunked(loop, w3, drops_token, "Transfer", from_block, latest, failed_ranges)
    logger.info("  %s Transfer: %d total", spec.name, len(transfer_logs))
    for log in transfer_logs:
        from_addr = (log["args"].get("from") or "").lower()
        to_addr   = (log["args"].get("to")   or "").lower()
        value     = int(log["args"].get("value") or 0)

        if from_addr == zero and not skip(to_addr):
            # Mint → game_drops received
            stats[to_addr].game_drops_wei += value

        elif to_addr == zero and not skip(from_addr):
            # Burn → stake deducted
            stats[from_addr].burns_wei += value

    # ── PointsRedeemed → reward_drops ────────────────────────────────────────
    redeem_logs = await _fetch_logs_chunked(loop, w3, drops_token, "PointsRedeemed", from_block, latest, failed_ranges)
    logger.info("  %s PointsRedeemed: %d", spec.name, len(redeem_logs))
    for log in redeem_logs:
        user = (log["args"].get("user") or "").lower()
        if not skip(user):
            _ = stats[user]

    if failed_ranges:
        logger.error(
            "%s scan: %d block range(s) could not be fetched (first: %d–%d) — "
            "checkpoint will not advance past them",
            spec.name, len(failed_ranges), failed_ranges[0][0], failed_ranges[0][1],
        )
    logger.info(
        "%s scan complete — wallets active in range: %d  (failed ranges: %d)",
        spec.name, len(stats), len(failed_ranges),
    )
    return dict(stats), latest, failed_ranges


# ── DB upsert (delta merge) ───────────────────────────────────────────────────

async def upsert_users(
    pool,
    supabase,
    stats: dict[str, WalletStats],
    chain_id: int,
    chain_name: str,
    mode: str = "delta",     # "delta" = additive merge, "absolute" = overwrite stats
) -> dict:
    """
    Merge incremental per-chain stats into the DB.

    user_profiles    — one row per wallet (chain-agnostic; username assigned once)
    challenge_players — total_duels / total_wins across ALL chains (additive)
    challenge_player_balances — per (wallet, chain_id) balance row
    """
    wallets = list(stats.keys())
    if not wallets:
        return {"wallets_found": 0, "profiles_new": 0, "profiles_updated": 0, "chain_id": chain_id}

    # ── 1. Fetch existing profiles to avoid renaming real users ───────────────
    existing: dict[str, str] = {}
    try:
        for i in range(0, len(wallets), 500):
            res = (
                supabase.table("user_profiles")
                .select("wallet_address, username")
                .in_("wallet_address", wallets[i : i + 500])
                .execute()
            )
            for row in res.data or []:
                existing[row["wallet_address"].lower()] = row["username"]
    except Exception as exc:
        logger.warning("Could not fetch existing profiles: %s", exc)

    # ── 2. Assign usernames to brand-new wallets ──────────────────────────────
    # username_pool.generate_username() is deterministic per-wallet, so two
    # different wallets CAN hash to the same name (e.g. both landing on
    # "Almaz"). user_profiles.username has a UNIQUE constraint, so a
    # colliding row poisons the whole upsert batch and Supabase rejects ALL
    # rows in that chunk — silently dropping every other new wallet in it
    # from user_profiles (and therefore from ranks). Build a `taken` set of
    # every username already in use and route through
    # generate_unique_username(), which appends address-derived digits on
    # collision, exactly like rename_synced_users.py already does.
    from username_pool import generate_unique_username

    taken: set[str] = {u.lower() for u in existing.values() if u}
    try:
        page, size = 0, 1000
        while True:
            res = (
                supabase.table("user_profiles")
                .select("username")
                .range(page * size, page * size + size - 1)
                .execute()
            )
            batch = res.data or []
            taken.update((r["username"] or "").lower() for r in batch)
            if len(batch) < size:
                break
            page += 1
    except Exception as exc:
        logger.warning("Could not fetch full taken-username set (%s): %s", chain_name, exc)

    new_wallets_ordered = [w for w in wallets if w not in existing]
    new_rows = []
    for w in new_wallets_ordered:
        uname = generate_unique_username(w, taken)
        taken.add(uname.lower())
        new_rows.append({"wallet_address": w, "username": uname})
    inserted = len(new_rows)

    if new_rows:
        failed_rows: list[dict] = []
        try:
            for i in range(0, len(new_rows), 200):
                chunk = new_rows[i : i + 200]
                try:
                    supabase.table("user_profiles").upsert(
                        chunk,
                        on_conflict="wallet_address",
                        ignore_duplicates=True,
                    ).execute()
                except Exception as chunk_exc:
                    # A single colliding row shouldn't drop the whole chunk —
                    # retry one row at a time so the rest still land.
                    logger.warning(
                        "%s: user_profiles chunk upsert failed, retrying row-by-row: %s",
                        chain_name, chunk_exc,
                    )
                    for row in chunk:
                        try:
                            supabase.table("user_profiles").upsert(
                                [row], on_conflict="wallet_address", ignore_duplicates=True,
                            ).execute()
                        except Exception as row_exc:
                            failed_rows.append(row)
                            logger.error(
                                "%s: user_profiles row upsert failed wallet=%s username=%s: %s",
                                chain_name, row["wallet_address"], row["username"], row_exc,
                            )
            ok_count = inserted - len(failed_rows)
            logger.info(
                "%s: user_profiles — inserted %d new rows (%d failed)",
                chain_name, ok_count, len(failed_rows),
            )
            if failed_rows:
                failed_set = {r["wallet_address"] for r in failed_rows}
                new_rows = [r for r in new_rows if r["wallet_address"] not in failed_set]
        except Exception as exc:
            logger.error("user_profiles upsert failed (%s): %s", chain_name, exc)

    # ── 3. Emit activity EVENT_REGISTER for new wallets ───────────────────────
    if new_rows:
        try:
            from activity_service import record_many, EVENT_REGISTER
            entries = [
                {
                    "wallet":   r["wallet_address"],
                    "chain_id": chain_id,
                    "event":    EVENT_REGISTER,
                    "metadata": {"source": "onchain_sync", "chain": chain_name},
                }
                for r in new_rows
            ]
            await record_many(pool, entries)
            logger.info("%s: activity_log — registered %d new wallets", chain_name, len(entries))
        except Exception as exc:
            logger.warning("activity_log register entries failed (%s): %s", chain_name, exc)

    # ── 4. Merge challenge_players (global duel/win counters) ─────────────────
    new_username_map = {r["wallet_address"]: r["username"] for r in new_rows}

    async def _merge_player(wallet: str, st: WalletStats, username: str):
        if mode == "absolute":
            duel_sql = "total_duels = EXCLUDED.total_duels, total_wins = EXCLUDED.total_wins,"
        else:
            duel_sql = (
                "total_duels = challenge_players.total_duels + EXCLUDED.total_duels, "
                "total_wins  = challenge_players.total_wins  + EXCLUDED.total_wins,"
            )
        # Each concurrent task gets its OWN connection from the pool.
        # asyncpg connections can't run more than one query at a time, so
        # sharing a single connection across gather()'d tasks causes
        # "another operation is in progress" and aborts the whole merge.
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO challenge_players
                    (wallet_address, username, total_duels, total_wins)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (wallet_address) DO UPDATE
                    SET {duel_sql}
                        username = CASE
                                     WHEN challenge_players.username IS NULL
                                       OR challenge_players.username ~ '^User[0-9A-Fa-f]{{4}}$'
                                       OR challenge_players.username ~ '^Player[0-9A-Fa-f]{{6}}$'
                                     THEN EXCLUDED.username
                                     ELSE challenge_players.username
                                   END
                """,
                wallet, username, st.total_duels, st.total_wins,
            )

    tasks = [
        _merge_player(
            w, st,
            existing.get(w) or new_username_map.get(w) or f"User{w[-4:].upper()}",
        )
        for w, st in stats.items()
    ]
    for i in range(0, len(tasks), 100):
        await asyncio.gather(*tasks[i : i + 100])

    logger.info("%s: challenge_players — merged %d rows", chain_name, len(stats))

    # ── 5. Merge challenge_player_balances (per-chain) ────────────────────────
    async def _merge_balance(wallet: str, st: WalletStats):
        game_drops    = st.game_drops_wei   / 1e18
        reward_drops  = st.reward_drops_wei / 1e18
        payouts       = st.payouts_wei      / 1e18
        burns         = st.burns_wei        / 1e18

        if mode == "absolute":
            update_sql = """
                    game_drops    = challenge_player_balances.game_drops,
                    reward_drops  = challenge_player_balances.reward_drops,
                    drops_minted  = challenge_player_balances.drops_minted OR (EXCLUDED.game_drops > 0),
                    total_payouts = EXCLUDED.total_payouts,
                    total_burns   = EXCLUDED.total_burns,
                    total_duels   = EXCLUDED.total_duels,
                    total_wins    = EXCLUDED.total_wins,
                    total_earned  = EXCLUDED.total_earned
            """
        else:
            update_sql = """
                    game_drops    = challenge_player_balances.game_drops   + EXCLUDED.game_drops,
                    reward_drops  = challenge_player_balances.reward_drops,
                    drops_minted  = challenge_player_balances.drops_minted OR (EXCLUDED.game_drops > 0),
                    total_payouts = COALESCE(challenge_player_balances.total_payouts, 0) + EXCLUDED.total_payouts,
                    total_burns   = COALESCE(challenge_player_balances.total_burns,   0) + EXCLUDED.total_burns,
                    total_duels   = COALESCE(challenge_player_balances.total_duels,   0) + EXCLUDED.total_duels,
                    total_wins    = COALESCE(challenge_player_balances.total_wins,    0) + EXCLUDED.total_wins,
                    total_earned  = COALESCE(challenge_player_balances.total_earned,  0) + EXCLUDED.total_earned
            """

        # Own connection per task — see note in _merge_player above.
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO challenge_player_balances
                    (wallet_address, chain_id, game_drops, reward_drops, drops_minted,
                     total_payouts, total_burns, total_duels, total_wins, total_earned)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (wallet_address, chain_id) DO UPDATE
                    SET {update_sql}
                """,
                wallet, chain_id,
                game_drops, reward_drops, game_drops > 0,
                payouts, burns,
                st.total_duels, st.total_wins, payouts,
            )

    # Add total_payouts / total_burns columns if they don't exist yet.
    # Safe to run each time — ALTER TABLE ADD COLUMN IF NOT EXISTS is idempotent.
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE challenge_player_balances ADD COLUMN IF NOT EXISTS total_payouts NUMERIC DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE challenge_player_balances ADD COLUMN IF NOT EXISTS total_burns NUMERIC DEFAULT 0"
        )

    tasks = [_merge_balance(w, st) for w, st in stats.items()]
    for i in range(0, len(tasks), 100):
        await asyncio.gather(*tasks[i : i + 100])

    logger.info("%s: challenge_player_balances — merged %d rows", chain_name, len(stats))

    return {
        "wallets_found":    len(stats),
        "profiles_new":     inserted,
        "profiles_updated": len(stats) - inserted,
        "chain_id":         chain_id,
    }


# ── Public entry point ────────────────────────────────────────────────────────

async def run_sync_chain(
    pool,
    supabase,
    spec: ChainSpec,
    *,
    force_full: bool = False,
) -> dict:
    """
    Run one complete incremental sync cycle for a single chain.
    """
    logger.info("=== onchain_sync [%s]: starting ===", spec.name)

    # mode: full-history scans (force_full or first run) write ABSOLUTE totals
    # so they're idempotent; incremental scans write additive deltas.
    if force_full:
        from_block = min(spec.quiz_hub_deploy_block, spec.drops_token_deploy_block)
        mode = "absolute"
        logger.info("%s: force-full scan from block %d (absolute mode)", spec.name, from_block)
    else:
        last = await _get_last_block(pool, spec.sync_state_key)
        if last == 0:
            from_block = min(spec.quiz_hub_deploy_block, spec.drops_token_deploy_block)
            mode = "absolute"
            logger.info("%s: first run — scanning from deploy block %d (absolute mode)", spec.name, from_block)
        else:
            from_block = last + 1
            mode = "delta"
            logger.info("%s: incremental scan from block %d (last synced: %d)", spec.name, from_block, last)

    stats, latest_block, failed_ranges = await scan_chain_events(spec, from_block)

    # Never checkpoint past a block range the RPC failed to serve — otherwise
    # those blocks are skipped FOREVER (this is likely how Botchain history got
    # lost: chunk fetches failed with only a WARN, then the checkpoint jumped
    # to the tip anyway). Checkpoint up to just before the earliest failure so
    # the next run retries from there.
    if failed_ranges:
        first_failed     = min(fr for fr, _ in failed_ranges)
        checkpoint_block = max(from_block - 1, first_failed - 1)
        logger.warning(
            "%s: %d failed range(s) — checkpointing at %d instead of tip %d",
            spec.name, len(failed_ranges), checkpoint_block, latest_block,
        )
    else:
        checkpoint_block = latest_block

    result: dict = {
        "chain":            spec.name,
        "chain_id":         spec.chain_id,
        "mode":             mode,
        "from_block":       from_block,
        "to_block":         latest_block,
        "checkpoint_block": checkpoint_block,
        "failed_ranges":    len(failed_ranges),
        "blocks_scanned":   max(0, latest_block - from_block + 1),
    }

    if stats:
        upsert_result = await upsert_users(pool, supabase, stats, spec.chain_id, spec.name, mode=mode)
        result.update(upsert_result)
    else:
        result.update({"wallets_found": 0, "profiles_new": 0, "profiles_updated": 0})

    result.update({
        "total_duels_in_range":    sum(s.total_duels        for s in stats.values()),
        "total_wins_in_range":     sum(s.total_wins         for s in stats.values()),
        "total_ties_in_range":     sum(s.total_ties         for s in stats.values()),
        "drops_minted_in_range":   round(sum(s.game_drops_wei   for s in stats.values()) / 1e18, 4),
        "drops_redeemed_in_range": round(sum(s.reward_drops_wei for s in stats.values()) / 1e18, 4),
        "drops_burned_in_range":   round(sum(s.burns_wei         for s in stats.values()) / 1e18, 4),
        "payouts_in_range":        round(sum(s.payouts_wei       for s in stats.values()) / 1e18, 4),
        "synced_at":               datetime.now(timezone.utc).isoformat(),
    })

    await _save_last_block(pool, spec.sync_state_key, checkpoint_block, result)
    logger.info("=== onchain_sync [%s]: complete — wallets=%d new=%d ===",
                spec.name, result.get("wallets_found", 0), result.get("profiles_new", 0))
    return result


async def run_sync(
    pool,
    supabase,
    *,
    force_full: bool = False,
    chains: Optional[list[str]] = None,   # None = all; or ["celo"], ["botchain"], ["celo","botchain"]
) -> dict:
    """
    Run the full sync for all configured chains (or a subset).

    Returns a combined result dict with per-chain breakdowns under 'chains'.
    """
    specs = _build_chain_specs()

    if chains:
        chain_filter = {c.lower() for c in chains}
        specs = [s for s in specs if s.name.lower() in chain_filter]

    if not specs:
        logger.warning("run_sync: no chains to sync (check BOTCHAIN_RPC_URL env var)")
        return {"chains": [], "synced_at": datetime.now(timezone.utc).isoformat()}

    # Run chains concurrently — they don't share DB locks (different chain_id rows).
    chain_results = await asyncio.gather(
        *[run_sync_chain(pool, supabase, spec, force_full=force_full) for spec in specs],
        return_exceptions=True,
    )

    combined: dict = {
        "chains":     [],
        "synced_at":  datetime.now(timezone.utc).isoformat(),
        # Aggregate totals across all chains
        "total_wallets_found": 0,
        "total_profiles_new":  0,
        "total_duels":         0,
        "total_wins":          0,
    }

    for res in chain_results:
        if isinstance(res, Exception):
            logger.error("Chain sync error: %s", res, exc_info=res)
            combined["chains"].append({"error": str(res)})
        else:
            combined["chains"].append(res)
            combined["total_wallets_found"] += res.get("wallets_found", 0)
            combined["total_profiles_new"]  += res.get("profiles_new",  0)
            combined["total_duels"]         += res.get("total_duels_in_range", 0)
            combined["total_wins"]          += res.get("total_wins_in_range",  0)

    return combined


# ── Background scheduler ──────────────────────────────────────────────────────

_scheduler_task: Optional[asyncio.Task] = None


async def _scheduler_loop(pool, supabase) -> None:
    interval = SYNC_INTERVAL_HOURS * 3600
    logger.info("onchain_sync scheduler started — interval=%dh", SYNC_INTERVAL_HOURS)

    while True:
        try:
            await run_sync(pool, supabase)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("onchain_sync scheduler error: %s", exc, exc_info=True)

        logger.info("onchain_sync: next run in %d hours", SYNC_INTERVAL_HOURS)
        await asyncio.sleep(interval)


def start_background_scheduler(pool, supabase) -> asyncio.Task:
    """
    Call once from your FastAPI lifespan / startup handler.

    Example:
        @app.on_event("startup")
        async def startup():
            app.state.sync_task = start_background_scheduler(pool, supabase)

        @app.on_event("shutdown")
        async def shutdown():
            app.state.sync_task.cancel()
    """
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        logger.warning("Scheduler already running — not starting a second one")
        return _scheduler_task
    _scheduler_task = asyncio.create_task(_scheduler_loop(pool, supabase))
    return _scheduler_task


def stop_background_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        logger.info("onchain_sync scheduler stopped")


# ── CLI runner ────────────────────────────────────────────────────────────────

async def _cli_main() -> None:
    import argparse
    import asyncpg
    import json
    from dotenv import load_dotenv
    from supabase import create_client

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    parser = argparse.ArgumentParser(description="FaucetDrops dual-chain on-chain user sync")
    parser.add_argument("--full",  action="store_true", help="Ignore checkpoint, rescan from deploy blocks")
    parser.add_argument(
        "--chain",
        choices=["celo", "botchain", "both"],
        default="both",
        help="Which chain(s) to sync (default: both)",
    )
    args = parser.parse_args()

    chain_filter: Optional[list[str]] = None
    if args.chain == "celo":
        chain_filter = ["celo"]
    elif args.chain == "botchain":
        chain_filter = ["botchain"]

    db_url   = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    supa_url = os.getenv("NEXT_PUBLIC_SUPABASE_URL") or os.getenv("SUPABASE_URL")
    supa_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

    if not db_url:   raise SystemExit("Set DATABASE_URL in env / .env")
    if not supa_url: raise SystemExit("Set SUPABASE_URL in env / .env")
    if not supa_key: raise SystemExit("Set SUPABASE_SERVICE_ROLE_KEY in env / .env")

    pool     = await asyncpg.create_pool(db_url, min_size=2, max_size=5)
    supabase = create_client(supa_url, supa_key)

    result = await run_sync(pool, supabase, force_full=args.full, chains=chain_filter)
    print("\n=== Sync result ===")
    print(json.dumps(result, indent=2, default=str))
    await pool.close()


if __name__ == "__main__":
    asyncio.run(_cli_main())