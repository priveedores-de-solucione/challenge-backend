"""
notifications.py
────────────────
In-app notification system.

Two delivery mechanisms:
  1. DB-backed persistence  → clients poll GET /api/notifications/{wallet}
  2. Live push via WebSocket → if the target wallet has an open /ws/notify/{wallet}
                               connection the message is sent immediately
"""
from __future__ import annotations
import json, logging
from typing import Dict, List, Optional, Any
from fastapi import WebSocket
import asyncpg
from supabase import Client

logger = logging.getLogger(__name__)


class NType:
    PUBLIC_CHALLENGE   = "public_challenge"
    CHALLENGE_INVITE   = "challenge_invite"
    PLAYER_JOINED      = "player_joined"
    GAME_STARTING      = "game_starting"
    GAME_OVER          = "game_over"
    REMATCH_REQUEST    = "rematch_request"
    REWARD_AVAILABLE   = "reward_available"
    REWARD_CLAIMED     = "reward_claimed"
    REFUND_CLAIMED     = "refund_claimed"
    # ── DROPS economy ────────────────────────────────────────────────────
    REDEEM_COMPLETED   = "redeem_completed"
    STAKE_MATURED      = "stake_matured"
    STAKE_CLAIMED      = "stake_claimed"
    BADGE_UNLOCKED     = "badge_unlocked"
    WEEKLY_WINNER      = "weekly_winner"
    WELCOME_MINTED     = "welcome_minted"


_RATE_LIMITS: dict[str, tuple[int, int]] = {
    NType.PUBLIC_CHALLENGE: (2,  5),
    NType.REMATCH_REQUEST:  (1,  2),
    NType.CHALLENGE_INVITE: (2, 10),
    NType.STAKE_MATURED:    (1, 60),
    NType.BADGE_UNLOCKED:   (1, 1440),
    NType.WEEKLY_WINNER:    (1, 1440),
}


class NotificationService:
    def __init__(
        self,
        pool:             asyncpg.Pool,
        live_connections: Dict[str, List[WebSocket]],
        supabase:         Optional[Client] = None,   # ← new optional param
    ):
        self.pool     = pool
        self.conns    = live_connections
        self.supabase = supabase

    # ─── User profile helper ─────────────────────────────────────────────────

    async def _get_display(self, wallet: str) -> dict:
        """
        Returns {"username": str, "avatar_url": str} for a wallet.
        Falls back to truncated address if no profile found.
        """
        fallback = {"username": f"User{wallet[-4:].upper()}", "avatar_url": ""}

        if not self.supabase:
            return fallback

        try:
            res = self.supabase.table("user_profiles") \
                .select("username, avatar_url") \
                .eq("wallet_address", wallet.lower()) \
                .execute()
            if res.data:
                p = res.data[0]
                return {
                    "username":   p.get("username")   or fallback["username"],
                    "avatar_url": p.get("avatar_url") or "",
                }
        except Exception as e:
            logger.warning("_get_display failed wallet=%s: %s", wallet, e)

        return fallback

    # ─── Core send ───────────────────────────────────────────────────────────

    async def send(
        self,
        recipient: str,
        type_: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> str:
        recipient = recipient.lower()

        if type_ in _RATE_LIMITS:
            max_count, window_minutes = _RATE_LIMITS[type_]
            async with self.pool.acquire() as conn:
                recent = await conn.fetchval(
                    """SELECT COUNT(*) FROM notifications
                       WHERE recipient_wallet = $1
                         AND type            = $2
                         AND created_at      > NOW() - ($3 || ' minutes')::interval""",
                    recipient, type_, str(window_minutes),
                )
            if recent >= max_count:
                logger.info(
                    "Rate limit hit — skipping: type=%s recipient=%s "
                    "(already %d in last %d min)",
                    type_, recipient, recent, window_minutes,
                )
                return ""

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO notifications (recipient_wallet, type, title, body, data)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING id, created_at""",
                recipient, type_, title, body,
                json.dumps(data) if data else None,
            )

        notif_id = str(row["id"])
        payload  = {
            "id":        notif_id,
            "type":      type_,
            "title":     title,
            "body":      body,
            "data":      data,
            "isRead":    False,
            "createdAt": row["created_at"].isoformat(),
        }

        await self._push(recipient, payload)
        return notif_id

    # ─── Broadcast ───────────────────────────────────────────────────────────

    async def broadcast(
        self,
        recipients: List[str],
        type_: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        for wallet in recipients:
            await self.send(wallet, type_, title, body, data)

    # ─── Game / challenge helpers ─────────────────────────────────────────────

    async def notify_public_challenge(
        self,
        code: str,
        topic: str,
        creator: str,
        stake: float,
        token: str,
        creator_username: str = "",
    ) -> None:
        # Pull real display name from user_profiles
        profile      = await self._get_display(creator)
        display_name = creator_username or profile["username"]

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.wallet_address FROM challenge_players p
                WHERE p.wallet_address != $1
                  AND p.last_seen_at > NOW() - INTERVAL '10 minutes'
                  AND p.wallet_address NOT IN (
                      SELECT recipient_wallet FROM notifications
                      WHERE type        = 'public_challenge'
                        AND created_at  > NOW() - INTERVAL '10 minutes'
                      GROUP BY recipient_wallet
                      HAVING COUNT(*) >= 3
                  )
                LIMIT 100
                """,
                creator.lower(),
            )

        logger.info(
            "notify_public_challenge: code=%s  recipients=%d  creator=%s  display=%s",
            code, len(rows), creator, display_name,
        )

        for row in rows:
            await self.send(
                row["wallet_address"],
                NType.PUBLIC_CHALLENGE,
                "🎯 New Public Challenge",
                f'{display_name} is challenging your knowledge on "{topic}" — stake {stake} {token}!',
                {
                    "code":        code,
                    "topic":       topic,
                    "stake":       stake,
                    "token":       token,
                    "creatorName": display_name,
                    "avatar":      profile["avatar_url"],
                },
            )

    async def notify_friend_invite(self, code, topic, from_username, from_wallet, to_wallet, stake, token):
        sender_profile = await self._get_display(from_wallet)
        display_name   = from_username or sender_profile["username"]

        await self.send(
            to_wallet,
            NType.CHALLENGE_INVITE,
            f"⚔️ {display_name} challenged you!",
            f'You\'ve been invited to a "{topic}" quiz. Stake: {stake} {token}. Code: {code}.',
            {
                "code":        code,
                "topic":       topic,
                "stake":       stake,
                "token":       token,
                "creatorName": display_name,
                "avatar":      sender_profile["avatar_url"],
            },
        )
    async def notify_player_joined(
        self,
        code: str,
        joiner_username: str,
        creator_wallet: str,
    ) -> None:
        # Enrich joiner display from user_profiles by username lookup
        joiner_avatar = ""
        if self.supabase:
            try:
                res = self.supabase.table("user_profiles") \
                    .select("avatar_url") \
                    .ilike("username", joiner_username) \
                    .execute()
                if res.data:
                    joiner_avatar = res.data[0].get("avatar_url", "")
            except Exception as e:
                logger.warning("notify_player_joined avatar lookup failed: %s", e)

        await self.send(
            creator_wallet,
            NType.PLAYER_JOINED,
            "👥 Opponent joined!",
            f"{joiner_username} accepted your challenge. Head back to start!",
            {"code": code, "joinerUsername": joiner_username, "avatar": joiner_avatar},
        )

    async def notify_game_starting(self, code: str, wallets: List[str]) -> None:
        for wallet in wallets:
            await self.send(
                wallet,
                NType.GAME_STARTING,
                "🚀 Game is starting!",
                "Both players are ready. The challenge begins now!",
                {"code": code},
            )

    async def notify_game_over(
        self,
        code: str,
        winner_wallet: str,
        loser_wallet: str,
        stake: float,
        token: str,
    ) -> None:
        winner_profile = await self._get_display(winner_wallet)
        loser_profile  = await self._get_display(loser_wallet)

        await self.send(
            winner_wallet,
            NType.REWARD_AVAILABLE,
            "🏆 You won! Claim your reward.",
            f"You beat {loser_profile['username']}! Claim {stake * 2:.1f} {token} now.",
            {
                "code":            code,
                "opponentUsername": loser_profile["username"],
                "opponentAvatar":  loser_profile["avatar_url"],
            },
        )
        await self.send(
            loser_wallet,
            NType.GAME_OVER,
            "😔 Better luck next time",
            f"You lost to {winner_profile['username']}. Challenge them to a rematch!",
            {
                "code":            code,
                "opponentUsername": winner_profile["username"],
                "opponentAvatar":  winner_profile["avatar_url"],
            },
        )

    async def notify_rematch_request(
        self,
        code: str,
        requester_username: str,
        opponent_wallet: str,
    ) -> None:
        await self.send(
            opponent_wallet,
            NType.REMATCH_REQUEST,
            "🔁 Rematch requested!",
            f"{requester_username} wants a rematch. Accept the challenge!",
            {"code": code},
        )

    # ─── DROPS economy helpers ────────────────────────────────────────────────

    async def notify_reward_claimed(
        self,
        wallet:       str,
        drops_amount: float,
        tx_hash:      str,
        game_code:    str,
    ) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.REWARD_CLAIMED,
            "🏆 Reward Claimed!",
            f"Well done, {profile['username']}! You claimed {drops_amount:.1f} DROPS from game {game_code}.",
            {
                "txHash":   tx_hash,
                "code":     game_code,
                "drops":    drops_amount,
                "username": profile["username"],
                "avatar":   profile["avatar_url"],
            },
        )

    async def notify_refund_claimed(
        self,
        wallet:       str,
        drops_amount: float,
        tx_hash:      str,
        game_code:    str,
    ) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.REFUND_CLAIMED,
            "↩️ Stake Refunded",
            f"Hey {profile['username']}, your {drops_amount:.1f} DROPS stake from game {game_code} has been returned.",
            {
                "txHash":   tx_hash,
                "code":     game_code,
                "drops":    drops_amount,
                "username": profile["username"],
                "avatar":   profile["avatar_url"],
            },
        )

    async def notify_redeem_completed(
        self,
        wallet: str,
        drops_redeemed: float,
        player_g: float,
        staked_drops: float,
        apy_pct: int,
        tx_hash: str,
        stake_id: str,
    ) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.REDEEM_COMPLETED,
            "💸 Redeem complete!",
            (
                f"{profile['username']}, you redeemed {drops_redeemed:.0f} DROPS. "
                f"{player_g:.4f} $G sent to your wallet. "
                f"{staked_drops:.0f} DROPS locked at {apy_pct}% APY for 30 days."
            ),
            {
                "dropsRedeemed": drops_redeemed,
                "playerG":       player_g,
                "stakedDrops":   staked_drops,
                "apyPct":        apy_pct,
                "txHash":        tx_hash,
                "stakeId":       stake_id,
                "username":      profile["username"],
                "avatar":        profile["avatar_url"],
            },
        )

    async def notify_stake_matured(
        self,
        wallet: str,
        stake_id: str,
        staked_drops: float,
        apy_pct: int,
    ) -> None:
        profile      = await self._get_display(wallet)
        earned_drops = staked_drops * (apy_pct / 100.0)
        await self.send(
            wallet,
            NType.STAKE_MATURED,
            "🔓 Stake ready to claim!",
            (
                f"{profile['username']}, your {staked_drops:.0f} DROPS stake has matured. "
                f"Claim now to receive your principal + ~{earned_drops:.0f} DROPS in $G APY."
            ),
            {
                "stakeId":     stake_id,
                "stakedDrops": staked_drops,
                "apyPct":      apy_pct,
                "earnedDrops": earned_drops,
                "username":    profile["username"],
                "avatar":      profile["avatar_url"],
            },
        )

    async def notify_stake_claimed(
        self,
        wallet: str,
        stake_id: str,
        principal_drops: float,
        earned_g: float,
        apy_pct: int,
        mint_tx: str,
        release_tx: str,
    ) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.STAKE_CLAIMED,
            "✅ Stake claimed!",
            (
                f"{profile['username']}, your {principal_drops:.0f} DROPS principal is back. "
                f"{earned_g:.4f} $G APY ({apy_pct}%) sent to your wallet."
            ),
            {
                "stakeId":        stake_id,
                "principalDrops": principal_drops,
                "earnedG":        earned_g,
                "apyPct":         apy_pct,
                "mintTx":         mint_tx,
                "releaseTx":      release_tx,
                "username":       profile["username"],
                "avatar":         profile["avatar_url"],
            },
        )

    async def notify_badge_unlocked(
        self,
        wallet: str,
        total_duels: int,
    ) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.BADGE_UNLOCKED,
            "🏅 Rematch Badge unlocked!",
            (
                f"Congrats {profile['username']}! You've completed {total_duels} games. "
                "Rematches, custom stakes, and stake negotiation are now unlocked."
            ),
            {
                "badge":      "rematch",
                "totalDuels": total_duels,
                "username":   profile["username"],
                "avatar":     profile["avatar_url"],
            },
        )

    async def notify_redeem_badge_unlocked(
        self,
        wallet: str,
        qualifying_duels: int,
    ) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.BADGE_UNLOCKED,
            "💎 Redeem Badge unlocked!",
            (
                f"Congrats {profile['username']}! You've completed {qualifying_duels} "
                "qualifying games. Redeeming DROPS for $G and all redeem-pool "
                "actions are now unlocked."
            ),
            {
                "badge":           "redeem",
                "qualifyingDuels": qualifying_duels,
                "username":        profile["username"],
                "avatar":          profile["avatar_url"],
            },
        )

    async def notify_weekly_winner(
        self,
        wallet: str,
        username: str,
        weekly_wins: int,
        rank: int,
    ) -> None:
        profile = await self._get_display(wallet)
        ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(rank, f"{rank}th")
        await self.send(
            wallet,
            NType.WEEKLY_WINNER,
            f"🥇 Weekly #{ordinal} — {profile['username']}!",
            (
                f"You finished {ordinal} on the weekly leaderboard with {weekly_wins} wins. "
                "Keep climbing — reset happens every Sunday!"
            ),
            {
                "rank":       rank,
                "weeklyWins": weekly_wins,
                "username":   profile["username"],
                "avatar":     profile["avatar_url"],
            },
        )

    async def notify_welcome_minted(self, wallet: str) -> None:
        profile = await self._get_display(wallet)
        await self.send(
            wallet,
            NType.WELCOME_MINTED,
            "🎉 Welcome! 100 DROPS credited.",
            f"Welcome, {profile['username']}! Your 100 DROPS are ready. Enter your first duel!",
            {
                "drops":    100,
                "username": profile["username"],
                "avatar":   profile["avatar_url"],
            },
        )

    # ─── DB reads ─────────────────────────────────────────────────────────────

    async def get_unread(self, wallet: str, limit: int = 20) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, type, title, body, data, is_read, created_at
                   FROM notifications
                   WHERE recipient_wallet=$1
                   ORDER BY created_at DESC
                   LIMIT $2""",
                wallet.lower(), limit,
            )
        return [_row_to_dict(r) for r in rows]

    async def mark_read(self, wallet: str, notif_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notifications SET is_read=TRUE WHERE id=$1 AND recipient_wallet=$2",
                notif_id, wallet.lower(),
            )

    async def mark_all_read(self, wallet: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notifications SET is_read=TRUE WHERE recipient_wallet=$1",
                wallet.lower(),
            )

    async def unread_count(self, wallet: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM notifications WHERE recipient_wallet=$1 AND is_read=FALSE",
                wallet.lower(),
            )

    # ─── Internal push ────────────────────────────────────────────────────────

    async def _push(self, wallet: str, payload: dict) -> None:
        sockets = self.conns.get(wallet, [])
        if not sockets:
            return

        dead = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            sockets.remove(ws)

        if not sockets:
            self.conns.pop(wallet, None)


def _row_to_dict(row) -> dict:
    d              = dict(row)
    d["id"]        = str(d["id"])
    d["createdAt"] = d.pop("created_at").isoformat()
    d["isRead"]    = d.pop("is_read")
    if d["data"] and isinstance(d["data"], str):
        d["data"] = json.loads(d["data"])
    return d