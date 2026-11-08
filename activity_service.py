"""
activity_service.py
───────────────────
Per-chain activity ledger for FaucetDrops.

Every balance-affecting event (welcome mint, buy, game stake, win, tie,
claim, redeem, stake-claim) is written as one row in `player_activity_log`.

Columns
───────
  wallet_address  text        lower-cased EVM address
  chain_id        int         which chain the event happened on
  event_type      text        one of the EVENT_* constants below
  pouch           text        'game' | 'reward' | null (for informational events)
  drops_delta     numeric     signed — positive = credit, negative = debit
  g_delta         numeric     signed $G amount (redeem / stake-claim only)
  tx_hash         text        on-chain tx hash when available
  ref_code        text        game code (for quiz events)
  metadata        jsonb       extra context (reason, apy, opponent, …)
  created_at      timestamptz auto-set by DB default

Usage
─────
    from activity_service import record, EVENT_WELCOME_MINT

    await record(
        pool      = pool,
        wallet    = wallet,
        chain_id  = cfg.id,
        event     = EVENT_WELCOME_MINT,
        pouch     = "game",
        drops_delta = WELCOME_DROPS,
        tx_hash   = tx_hash,
    )
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

# ─── Event type constants ─────────────────────────────────────────────────────
# Keep these in sync with any frontend enum / migration.

EVENT_REGISTER          = "register"           # player row first created
EVENT_WELCOME_MINT      = "welcome_mint"        # 100 DROPS welcome bonus credited
EVENT_BUY_DROPS         = "buy_drops"           # $G → DROPS purchase
EVENT_GAME_CREATED      = "game_created"        # challenge created (no balance change)
EVENT_GAME_JOINED       = "game_joined"         # opponent joined
EVENT_STAKE_DEDUCTED    = "stake_deducted"      # game_drops debited at stake time
EVENT_GAME_WIN          = "game_win"            # pending claim written for winner (2× stake)
EVENT_GAME_TIE          = "game_tie"            # pending claim written for tie refund
EVENT_CLAIM_WIN         = "claim_win"           # winner claimed their DROPS (minted)
EVENT_CLAIM_REFUND      = "claim_refund"        # tie / cancelled game refund claimed
EVENT_REDEEM_START      = "redeem_start"        # reward_drops debited to start redemption
EVENT_STAKE_CREATED     = "stake_created"       # DropsRedeemPool stake opened
EVENT_STAKE_CLAIMED     = "stake_claimed"       # stake matured & principal + APY returned
EVENT_CANCEL_REFUND     = "cancel_refund"       # stake refunded after game cancel
EVENT_GAME_REWARD       = "game_reward"         # direct reward credit (non-claim path)


# ─── Pouch labels ─────────────────────────────────────────────────────────────

POUCH_GAME   = "game"
POUCH_REWARD = "reward"


# ─── Core writer ─────────────────────────────────────────────────────────────

async def record(
    pool:         asyncpg.Pool,
    wallet:       str,
    chain_id:     int,
    event:        str,
    pouch:        Optional[str]  = None,
    drops_delta:  float          = 0.0,
    g_delta:      float          = 0.0,
    tx_hash:      Optional[str]  = None,
    ref_code:     Optional[str]  = None,
    metadata:     Optional[dict[str, Any]] = None,
) -> None:
    """
    Write one activity row.  Always fire-and-forget safe — any DB error is
    logged but never propagated, so a logging failure never breaks a tx flow.
    """
    import json as _json

    wallet = wallet.lower()
    meta_json = _json.dumps(metadata) if metadata else None

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO player_activity_log
                    (wallet_address, chain_id, event_type, pouch,
                     drops_delta, g_delta, tx_hash, ref_code, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                wallet, chain_id, event, pouch,
                drops_delta, g_delta, tx_hash, ref_code, meta_json,
            )
        logger.debug(
            "activity  wallet=%s  chain=%d  event=%s  drops=%+.4f  g=%+.4f  ref=%s",
            wallet, chain_id, event, drops_delta, g_delta, ref_code or "",
        )
    except Exception as exc:
        logger.error(
            "activity_log INSERT FAILED  wallet=%s  chain=%d  event=%s  error=%s",
            wallet, chain_id, event, exc,
        )
        # Never re-raise — activity logging must never break main flows.


async def record_many(
    pool:    asyncpg.Pool,
    entries: list[dict],
) -> None:
    """
    Bulk-insert multiple activity rows.  Each dict must contain at minimum:
      wallet, chain_id, event
    Optional keys: pouch, drops_delta, g_delta, tx_hash, ref_code, metadata.
    """
    import json as _json

    rows = []
    for e in entries:
        rows.append((
            e["wallet"].lower(),
            e["chain_id"],
            e["event"],
            e.get("pouch"),
            float(e.get("drops_delta", 0.0)),
            float(e.get("g_delta", 0.0)),
            e.get("tx_hash"),
            e.get("ref_code"),
            _json.dumps(e["metadata"]) if e.get("metadata") else None,
        ))

    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO player_activity_log
                    (wallet_address, chain_id, event_type, pouch,
                     drops_delta, g_delta, tx_hash, ref_code, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                rows,
            )
    except Exception as exc:
        logger.error("activity_log bulk INSERT FAILED  count=%d  error=%s", len(rows), exc)


# ─── Query helpers ────────────────────────────────────────────────────────────

async def get_activity(
    pool:       asyncpg.Pool,
    wallet:     str,
    chain_id:   Optional[int]  = None,
    event_type: Optional[str]  = None,
    pouch:      Optional[str]  = None,
    limit:      int            = 50,
    offset:     int            = 0,
) -> list[dict]:
    """
    Return paginated activity rows for a wallet, optionally filtered by
    chain_id, event_type, and/or pouch.
    """
    wallet = wallet.lower()
    filters = ["wallet_address = $1"]
    params: list[Any] = [wallet]
    idx = 2

    if chain_id is not None:
        filters.append(f"chain_id = ${idx}")
        params.append(chain_id)
        idx += 1

    if event_type:
        filters.append(f"event_type = ${idx}")
        params.append(event_type)
        idx += 1

    if pouch:
        filters.append(f"pouch = ${idx}")
        params.append(pouch)
        idx += 1

    where = " AND ".join(filters)
    params += [limit, offset]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, chain_id, event_type, pouch,
                   drops_delta, g_delta, tx_hash, ref_code,
                   metadata, created_at
            FROM player_activity_log
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
    return [_row_to_dict(r) for r in rows]


async def get_activity_summary(
    pool:     asyncpg.Pool,
    wallet:   str,
    chain_id: Optional[int] = None,
) -> dict:
    """
    Aggregate totals per pouch per chain.  Useful for the dashboard balance
    overview — returns game_drops_in, game_drops_out, reward_drops_in,
    reward_drops_out broken down by chain.
    """
    wallet = wallet.lower()
    extra  = "AND chain_id = $2" if chain_id is not None else ""
    params: list[Any] = [wallet]
    if chain_id is not None:
        params.append(chain_id)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                chain_id,
                pouch,
                SUM(CASE WHEN drops_delta > 0 THEN drops_delta ELSE 0 END) AS drops_in,
                SUM(CASE WHEN drops_delta < 0 THEN ABS(drops_delta) ELSE 0 END) AS drops_out,
                SUM(CASE WHEN g_delta     > 0 THEN g_delta      ELSE 0 END) AS g_in,
                SUM(CASE WHEN g_delta     < 0 THEN ABS(g_delta)  ELSE 0 END) AS g_out,
                COUNT(*) AS event_count
            FROM player_activity_log
            WHERE wallet_address = $1 {extra}
              AND pouch IS NOT NULL
            GROUP BY chain_id, pouch
            ORDER BY chain_id, pouch
            """,
            *params,
        )
    return {
        "wallet": wallet,
        "chains": [dict(r) for r in rows],
    }


def _row_to_dict(row: asyncpg.Record) -> dict:
    import json as _json
    d = dict(row)
    d["id"] = str(d["id"])
    d["created_at"] = d["created_at"].isoformat()
    if isinstance(d.get("metadata"), str):
        try:
            d["metadata"] = _json.loads(d["metadata"])
        except Exception:
            pass
    # Cast numeric to float for JSON serialisation
    for key in ("drops_delta", "g_delta"):
        if d.get(key) is not None:
            d[key] = float(d[key])
    return d