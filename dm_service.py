"""
dm_service.py
─────────────
Private 1-to-1 direct messages between two wallets.

Thread identity is deterministic: the two lower-cased addresses sorted and
joined with ':'. No thread table needed — the id is derivable from any pair.

Message kinds
─────────────
  text          normal chat message
  duel_invite   system card written when a private duel goes on-chain
  system        anything else the backend wants to drop into a thread
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

KIND_TEXT        = "text"
KIND_DUEL_INVITE = "duel_invite"
KIND_SYSTEM      = "system"

MAX_BODY_LEN      = 1000
RATE_MAX_PER_MIN  = 30


def thread_id(a: str, b: str) -> str:
    x, y = sorted([a.lower(), b.lower()])
    return f"{x}:{y}"


# ─── Write ────────────────────────────────────────────────────────────────────

async def send(
    pool:  asyncpg.Pool,
    frm:   str,
    to:    str,
    body:  str,
    kind:  str = KIND_TEXT,
    meta:  Optional[dict[str, Any]] = None,
) -> dict:
    frm, to = frm.lower(), to.lower()
    if frm == to:
        raise ValueError("Cannot message yourself")

    body = (body or "").strip()[:MAX_BODY_LEN]
    if not body:
        raise ValueError("Message is empty")

    async with pool.acquire() as conn:
        if kind == KIND_TEXT:
            recent = await conn.fetchval(
                """SELECT COUNT(*) FROM dm_messages
                   WHERE from_wallet = $1
                     AND kind        = 'text'
                     AND created_at  > NOW() - INTERVAL '1 minute'""",
                frm,
            )
            if recent >= RATE_MAX_PER_MIN:
                raise ValueError("Slow down — too many messages.")

        row = await conn.fetchrow(
            """INSERT INTO dm_messages (thread_id, from_wallet, to_wallet, body, kind, meta)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb)
               RETURNING id, created_at""",
            thread_id(frm, to), frm, to, body, kind,
            json.dumps(meta) if meta else None,
        )

    return {
        "id":        str(row["id"]),
        "from":      frm,
        "to":        to,
        "body":      body,
        "kind":      kind,
        "meta":      meta,
        "createdAt": row["created_at"].isoformat(),
        "isRead":    False,
    }


async def mark_read(pool: asyncpg.Pool, wallet: str, peer: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE dm_messages SET read_at = NOW()
               WHERE to_wallet = $1 AND from_wallet = $2 AND read_at IS NULL""",
            wallet.lower(), peer.lower(),
        )


# ─── Read ─────────────────────────────────────────────────────────────────────

async def history(
    pool:   asyncpg.Pool,
    wallet: str,
    peer:   str,
    limit:  int = 50,
    before: Optional[int] = None,
) -> list[dict]:
    tid = thread_id(wallet, peer)
    sql = """SELECT id, from_wallet, to_wallet, body, kind, meta, read_at, created_at
             FROM dm_messages WHERE thread_id = $1"""
    params: list[Any] = [tid]
    if before:
        sql += " AND id < $2"
        params.append(before)
    sql += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return [_row(r) for r in reversed(rows)]


async def threads(pool: asyncpg.Pool, wallet: str) -> list[dict]:
    """Latest message per peer + unread count, newest conversation first."""
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH mine AS (
                SELECT *,
                       CASE WHEN from_wallet = $1 THEN to_wallet ELSE from_wallet END AS peer
                FROM dm_messages
                WHERE from_wallet = $1 OR to_wallet = $1
            ),
            latest AS (
                SELECT DISTINCT ON (peer) peer, id, body, kind, from_wallet, created_at
                FROM mine ORDER BY peer, created_at DESC
            ),
            counts AS (
                SELECT peer, COUNT(*) AS unread
                FROM mine
                WHERE to_wallet = $1 AND read_at IS NULL
                GROUP BY peer
            )
            SELECT l.peer, l.body, l.kind, l.from_wallet, l.created_at,
                   COALESCE(c.unread, 0) AS unread
            FROM latest l LEFT JOIN counts c ON c.peer = l.peer
            ORDER BY l.created_at DESC
            LIMIT 100
            """,
            wallet,
        )

    return [{
        "peer":     r["peer"],
        "lastBody": r["body"],
        "lastKind": r["kind"],
        "lastFrom": r["from_wallet"],
        "lastAt":   r["created_at"].isoformat(),
        "unread":   int(r["unread"]),
    } for r in rows]


async def unread_total(pool: asyncpg.Pool, wallet: str) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM dm_messages WHERE to_wallet=$1 AND read_at IS NULL",
            wallet.lower(),
        ) or 0


def _row(r: asyncpg.Record) -> dict:
    meta = r["meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    return {
        "id":        str(r["id"]),
        "from":      r["from_wallet"],
        "to":        r["to_wallet"],
        "body":      r["body"],
        "kind":      r["kind"],
        "meta":      meta,
        "isRead":    r["read_at"] is not None,
        "createdAt": r["created_at"].isoformat(),
    }