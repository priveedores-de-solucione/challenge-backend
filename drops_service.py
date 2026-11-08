"""
drops_service.py
────────────────
Handles all DROPS token logic.

KEY CHANGES (v3 — per-chain):
  • All token balances (game_drops, reward_drops, drops_minted) now live in
    challenge_player_balances(wallet_address, chain_id) instead of the
    wallet-global challenge_players table.
  • mint_welcome_drops: mints on-chain FIRST; only credits the DB row if the
    transaction confirms successfully. If mint fails, nothing is written.
  • Every function that touches balances takes an explicit chain_id and
    reads/writes only the row for that chain.
  • Stake, redeem, buy, and claim flows are all chain-scoped.
"""
from __future__ import annotations

import os
import asyncio
import logging
import datetime
from typing import Optional, TYPE_CHECKING

import httpx
import asyncpg
from web3 import Web3
from eth_account import Account

from abi import DROPS_TOKEN_ABI, DROP_POOL_ABI
from chain_utils import send_tx
from chain_config import get_chain, build_w3_for_chain, CELO_CHAIN_ID
import activity_service as _act

if TYPE_CHECKING:
    from notifications import NotificationService

logger = logging.getLogger(__name__)

# ─── Env ──────────────────────────────────────────────────────────────────────
RESOLVER_PRIVATE_KEY = os.getenv("RESOLVER_PRIVATE_KEY", "")
G_PRICE_API_URL      = os.getenv("G_PRICE_API_URL", "")

DROPS_DECIMALS      = 18
WELCOME_DROPS       = 100
MIN_STAKE_DROPS     = 10.0
PRE_BADGE_MAX_STAKE = 10.0
BADGE_UNLOCK_DUELS  = 10   # Rematch Badge: 10 games total (1v1 or solo)

# Redeem Badge: 10 "qualifying" games — a 1v1 duel, or a solo game against a
# high-difficulty bot (Downpour / Torrent / Flood). Unlocks ALL redeem-pool
# actions (redeem reward DROPS → $G, stake claims).
REDEEM_BADGE_QUALIFYING_GAMES = 10
DROPS_PER_USD       = 100.0   # 100 DROPS = $1

# ─── Tier config ──────────────────────────────────────────────────────────────
TIERS = [
    (501, "Flood",     35),
    (301, "Torrent",   30),
    (151, "Downpour",  25),
    ( 51, "Drizzle",   20),
    (  0, "Droplet",   15),
]

_g_price_cache: dict = {"price": None, "fetched_at": 0.0}
_G_PRICE_TTL = 300  # 5 minutes


def get_tier(total_duels: int) -> tuple[str, int]:
    for threshold, name, apy in TIERS:
        if total_duels >= threshold:
            return name, apy
    return "Droplet", 15


# ─── Balance helpers ──────────────────────────────────────────────────────────

async def _ensure_chain_balance_row(conn: asyncpg.Connection, wallet: str, chain_id: int) -> None:
    """Insert the per-chain balance row if it doesn't exist yet."""
    await conn.execute(
        """
        INSERT INTO challenge_player_balances (wallet_address, chain_id)
        VALUES ($1, $2)
        ON CONFLICT (wallet_address, chain_id) DO NOTHING
        """,
        wallet, chain_id,
    )


async def get_chain_balance(pool: asyncpg.Pool, wallet: str, chain_id: int) -> dict:
    """Return the balance row for wallet+chain, creating it if missing."""
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, chain_id)
        row = await conn.fetchrow(
            """
            SELECT game_drops, reward_drops, drops_minted, welcome_tx_hash
            FROM challenge_player_balances
            WHERE wallet_address=$1 AND chain_id=$2
            """,
            wallet, chain_id,
        )
    return dict(row)


# ─── Signing helper ───────────────────────────────────────────────────────────

def _sign_claim(recipient: str, amount_wei: int, ts: int, chain_id: int = CELO_CHAIN_ID) -> bytes:
    from eth_account.messages import encode_defunct
    packed  = Web3.solidity_keccak(
        ["address", "uint256", "uint256", "uint256"],
        [Web3.to_checksum_address(recipient), amount_wei, ts, chain_id],
    )
    account = Account.from_key(RESOLVER_PRIVATE_KEY)
    return account.sign_message(encode_defunct(packed)).signature


# ─── Price ────────────────────────────────────────────────────────────────────

async def _fetch_g_price_usd() -> float:
    import time as _time
    now = _time.time()

    if _g_price_cache["price"] is not None and (now - _g_price_cache["fetched_at"]) < _G_PRICE_TTL:
        return _g_price_cache["price"]

    url = G_PRICE_API_URL or (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=good-dollar&vs_currencies=usd"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "FaucetDrops/1.0 (https://faucetdrops.io)"},
            )
            resp.raise_for_status()
            data = resp.json()

        if G_PRICE_API_URL:
            price = float(data.get("price") or data.get("usd") or data["data"]["price"])
        else:
            if not data or "good-dollar" not in data:
                raise ValueError(f"CoinGecko returned empty response: {data}")
            price = float(data["good-dollar"]["usd"])

        _g_price_cache["price"] = price
        _g_price_cache["fetched_at"] = now
        logger.info("G price fetched: $%s", price)
        return price

    except Exception as e:
        if _g_price_cache["price"] is not None:
            logger.warning("G price fetch failed (%s), using cached $%s", e, _g_price_cache["price"])
            return _g_price_cache["price"]
        raise ValueError(f"G price unavailable and no cache: {e}") from e


def _g_price_to_wei(price_usd: float) -> int:
    g_per_usd = 1.0 / price_usd
    return int(g_per_usd * (10 ** DROPS_DECIMALS))


def _drops_to_g(drops: float, g_price_usd: float) -> float:
    usd = drops / DROPS_PER_USD
    return usd / g_price_usd


# ─── Core on-chain mint ───────────────────────────────────────────────────────

async def _mint_to(
    recipient:  str,
    amount_wei: int,
    label:      str = "mintTo",
    chain_id:   int | None = None,
) -> str:
    cfg         = get_chain(chain_id)
    w3, account = build_w3_for_chain(cfg.id)

    if not cfg.drops_token:
        raise RuntimeError(f"_mint_to: no drops_token configured for chain {cfg.name}")

    token        = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.drops_token),
        abi=DROPS_TOKEN_ABI,
    )
    recipient_cs = Web3.to_checksum_address(recipient)

    def _build(nonce: int) -> dict:
        return token.functions.mintTo(recipient_cs, amount_wei).build_transaction({
            "from":  account.address,
            "nonce": nonce,
            "gas":   120_000,
        })

    receipt = await send_tx(_build, account, w3, chain_id=cfg.id, label=label)
    return receipt["transactionHash"].hex()


# ─── Welcome mint ─────────────────────────────────────────────────────────────

async def mint_welcome_drops(
    pool:     asyncpg.Pool,
    wallet:   str,
    notif:    Optional["NotificationService"] = None,
    chain_id: int | None = None,
) -> dict:
    """
    Mint welcome DROPS for a new player ON THIS CHAIN.

    Flow (on-chain first):
      1. Check the per-chain balance row — if drops_minted=TRUE, return early.
      2. Mint on-chain via mintTo().  If it fails, return {"minted": False}.
      3. Only after tx confirms: write drops_minted=TRUE + credit game_drops.

    This means a player who registers on Celo and Botchain separately gets
    100 DROPS on each chain independently.
    """
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        row = await conn.fetchrow(
            "SELECT drops_minted FROM challenge_player_balances WHERE wallet_address=$1 AND chain_id=$2",
            wallet, cfg.id,
        )

    if row and row["drops_minted"]:
        return {"minted": False, "welcomeDrops": 0.0, "chain": cfg.name}

    # Fold any existing on-chain balance (migration / pre-existing balance)
    existing_balance = 0.0
    try:
        existing_balance = await get_onchain_drops_balance(wallet, chain_id=cfg.id)
    except Exception as e:
        logger.warning("Could not read on-chain DROPS balance wallet=%s chain=%s: %s", wallet, cfg.name, e)

    total_credit = WELCOME_DROPS + existing_balance
    amount_wei   = int(total_credit * (10 ** DROPS_DECIMALS))

    # ── Mint on-chain FIRST ───────────────────────────────────────────────────
    try:
        tx_hash = await _mint_to(
            wallet, amount_wei,
            label=f"welcome_mint chain={cfg.name}",
            chain_id=cfg.id,
        )
    except Exception as e:
        logger.error(
            "welcome mint FAILED — NOT crediting DB  wallet=%s  chain=%s  error=%s",
            wallet, cfg.name, e,
        )
        return {
            "minted": False,
            "welcomeDrops": 0.0,
            "chain": cfg.name,
            "error": str(e),
        }

    # ── Tx confirmed — now safe to credit DB ─────────────────────────────────
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE challenge_player_balances
            SET drops_minted=TRUE, game_drops=game_drops+$1,
                welcome_tx_hash=$2, updated_at=NOW()
            WHERE wallet_address=$3 AND chain_id=$4
            """,
            total_credit, tx_hash, wallet, cfg.id,
        )

    logger.info(
        "welcome drops minted+credited  wallet=%s  chain=%s  "
        "welcome=%s  existing=%s  total=%s  tx=%s",
        wallet, cfg.name, WELCOME_DROPS, existing_balance, total_credit, tx_hash,
    )

    asyncio.create_task(_act.record(
        pool        = pool,
        wallet      = wallet,
        chain_id    = cfg.id,
        event       = _act.EVENT_WELCOME_MINT,
        pouch       = _act.POUCH_GAME,
        drops_delta = total_credit,
        tx_hash     = tx_hash,
        metadata    = {"welcome_drops": WELCOME_DROPS, "existing_folded": existing_balance},
    ))

    if notif:
        try:
            await notif.notify_welcome_minted(wallet)
        except Exception as e:
            logger.warning("notify_welcome_minted failed wallet=%s: %s", wallet, e)

    return {
        "minted": True,
        "welcomeDrops": WELCOME_DROPS,
        "existingBalanceFolded": existing_balance,
        "totalCredited": total_credit,
        "txHash": tx_hash,
        "chain": cfg.name,
    }


# ─── Game reward ──────────────────────────────────────────────────────────────

async def credit_game_reward(
    pool:         asyncpg.Pool,
    wallet:       str,
    drops_amount: float,
    reason:       str = "game_win",
    chain_id:     int | None = None,
) -> None:
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    # Credit DB first (optimistic) — roll back on mint failure
    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        await conn.execute(
            """
            UPDATE challenge_player_balances
            SET reward_drops=reward_drops+$1, updated_at=NOW()
            WHERE wallet_address=$2 AND chain_id=$3
            """,
            drops_amount, wallet, cfg.id,
        )
        # Also update global stats row
        await conn.execute(
            "UPDATE challenge_players SET reward_drops=reward_drops+$1 WHERE wallet_address=$2",
            drops_amount, wallet,
        )
        await conn.execute("SELECT challenge_update_player_tier($1)", wallet)

    amount_wei = int(drops_amount * (10 ** DROPS_DECIMALS))

    try:
        tx = await _mint_to(wallet, amount_wei, label=f"credit_game_reward({reason})", chain_id=cfg.id)
        logger.info(
            "game reward minted  wallet=%s  amount=%s  reason=%s  chain=%s  tx=%s",
            wallet, drops_amount, reason, cfg.name, tx,
        )
        asyncio.create_task(_act.record(
            pool        = pool,
            wallet      = wallet,
            chain_id    = cfg.id,
            event       = _act.EVENT_GAME_REWARD,
            pouch       = _act.POUCH_REWARD,
            drops_delta = drops_amount,
            tx_hash     = tx,
            metadata    = {"reason": reason},
        ))
    except Exception as e:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE challenge_player_balances
                SET reward_drops=reward_drops-$1, updated_at=NOW()
                WHERE wallet_address=$2 AND chain_id=$3
                """,
                drops_amount, wallet, cfg.id,
            )
            await conn.execute(
                "UPDATE challenge_players SET reward_drops=reward_drops-$1 WHERE wallet_address=$2",
                drops_amount, wallet,
            )
        logger.error(
            "game reward mint FAILED  wallet=%s  amount=%s  reason=%s  chain=%s  error=%s",
            wallet, drops_amount, reason, cfg.name, e,
        )
        raise


async def credit_welcome_bonus(
    pool: asyncpg.Pool,
    wallet: str,
    tx_hash: str,
    chain_id: int | None = None,
) -> dict:
    """
    Frontend already called DropsToken.welcome() itself and the tx confirmed.
    This does NOT mint anything — it just credits the off-chain pouch once,
    idempotent per wallet+chain via the drops_minted flag.
    """
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        row = await conn.fetchrow(
            "SELECT drops_minted FROM challenge_player_balances WHERE wallet_address=$1 AND chain_id=$2",
            wallet, cfg.id,
        )

    if row and row["drops_minted"]:
        return {"credited": False, "alreadyCredited": True, "chain": cfg.name}

    # Verify the tx actually succeeded on this chain before crediting anything.
    ok = await _quick_verify_tx_success(tx_hash, chain_id=cfg.id)
    if not ok:
        raise ValueError("Could not verify welcome() transaction on-chain.")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE challenge_player_balances
            SET drops_minted=TRUE, game_drops=game_drops+$1,
                welcome_tx_hash=$2, updated_at=NOW()
            WHERE wallet_address=$3 AND chain_id=$4
            """,
            WELCOME_DROPS, tx_hash, wallet, cfg.id,
        )

    logger.info(
        "welcome bonus credited (frontend-minted)  wallet=%s  chain=%s  amount=%s  tx=%s",
        wallet, cfg.name, WELCOME_DROPS, tx_hash,
    )

    asyncio.create_task(_act.record(
        pool        = pool,
        wallet      = wallet,
        chain_id    = cfg.id,
        event       = _act.EVENT_WELCOME_MINT,
        pouch       = _act.POUCH_GAME,
        drops_delta = WELCOME_DROPS,
        tx_hash     = tx_hash,
        metadata    = {"source": "frontend_minted"},
    ))

    return {"credited": True, "welcomeDrops": WELCOME_DROPS, "chain": cfg.name, "txHash": tx_hash}


# ─── Claim pending drops ──────────────────────────────────────────────────────

async def claim_pending_drops(
    pool:     asyncpg.Pool,
    wallet:   str,
    code:     str,
    notif:    Optional["NotificationService"] = None,
    chain_id: int | None = None,
) -> dict:
    wallet = wallet.lower()

    # ── Step 1: Atomically claim the row
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE challenge_pending_claims
               SET claimed = TRUE, claimed_at = NOW()
               WHERE wallet_address = $1 AND game_code = $2 AND claimed = FALSE
               RETURNING id, drops_amount, reason, tx_hash""",
            wallet, code,
        )

    if not row:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """SELECT claimed, tx_hash, drops_amount, reason
                   FROM challenge_pending_claims
                   WHERE wallet_address = $1 AND game_code = $2""",
                wallet, code,
            )
        if existing and existing["claimed"]:
            if existing["tx_hash"]:
                return {
                    "success":        True,
                    "dropsMinted":    float(existing["drops_amount"]),
                    "txHash":         existing["tx_hash"],
                    "reason":         existing["reason"],
                    "alreadyClaimed": True,
                }
            raise RuntimeError(
                "Claim is mid-processing or recovering — refresh in a moment. "
                "If this persists, contact support with code=" + code
            )
        raise ValueError("No pending claim found for this wallet and game.")

    # Resolve chain_id from the game if not supplied
    if chain_id is None:
        async with pool.acquire() as conn:
            chain_id = await conn.fetchval(
                "SELECT chain_id FROM challenge_games WHERE code=$1", code
            )
    cfg = get_chain(chain_id)

    drops_amount = float(row["drops_amount"])
    reason       = row["reason"]
    amount_wei   = int(drops_amount * (10 ** DROPS_DECIMALS))
    claim_id     = row["id"]

    # ── Step 2: Mint on-chain
    try:
        tx_hash = await _mint_to(
            wallet, amount_wei,
            label=f"claim_pending({reason},code={code})",
            chain_id=cfg.id,
        )
    except Exception as e:
        # Mint failed — release the lock so wallet can retry
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_pending_claims
                   SET claimed = FALSE, claimed_at = NULL
                   WHERE id = $1""",
                claim_id,
            )
        logger.error(
            "claim_pending_drops mint FAILED  wallet=%s  code=%s  chain=%s  error=%s",
            wallet, code, cfg.name, e,
        )
        raise RuntimeError(f"Mint failed: {e}") from e

    # ── Step 3: Record tx_hash immediately
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_pending_claims SET tx_hash = $1 WHERE id = $2",
            tx_hash, claim_id,
        )

    # ── Step 4: Credit off-chain balance (per-chain)
    balance_col = "reward_drops" if reason == "game_win" else "game_drops"
    try:
        async with pool.acquire() as conn:
            await _ensure_chain_balance_row(conn, wallet, cfg.id)
            await conn.execute(
                f"""UPDATE challenge_player_balances
                    SET {balance_col} = {balance_col} + $1, updated_at=NOW()
                    WHERE wallet_address=$2 AND chain_id=$3""",
                drops_amount, wallet, cfg.id,
            )
            # Mirror to global stats row
            await conn.execute(
                f"UPDATE challenge_players SET {balance_col}={balance_col}+$1 WHERE wallet_address=$2",
                drops_amount, wallet,
            )
    except Exception as e:
        logger.error(
            "claim_pending_drops DB credit FAILED after successful mint  "
            "wallet=%s  code=%s  chain=%s  tx=%s  error=%s",
            wallet, code, cfg.name, tx_hash, e,
        )
        return {
            "success":             True,
            "dropsMinted":         drops_amount,
            "txHash":              tx_hash,
            "reason":              reason,
            "balanceCreditPending": True,
        }

    logger.info(
        "claim_pending_drops OK  wallet=%s  code=%s  drops=%s  reason=%s  chain=%s  tx=%s",
        wallet, code, drops_amount, reason, cfg.name, tx_hash,
    )

    claim_event = _act.EVENT_CLAIM_WIN if reason == "game_win" else _act.EVENT_CLAIM_REFUND
    asyncio.create_task(_act.record(
        pool        = pool,
        wallet      = wallet,
        chain_id    = cfg.id,
        event       = claim_event,
        pouch       = _act.POUCH_REWARD if reason == "game_win" else _act.POUCH_GAME,
        drops_delta = drops_amount,
        tx_hash     = tx_hash,
        ref_code    = code,
        metadata    = {"reason": reason},
    ))

    if notif:
        try:
            if reason == "game_win":
                await notif.notify_reward_claimed(
                    wallet=wallet, drops_amount=drops_amount,
                    tx_hash=tx_hash, game_code=code,
                )
            else:
                await notif.notify_refund_claimed(
                    wallet=wallet, drops_amount=drops_amount,
                    tx_hash=tx_hash, game_code=code,
                )
        except Exception as e:
            logger.warning("notify claim failed wallet=%s reason=%s: %s", wallet, reason, e)

    return {
        "success":     True,
        "dropsMinted": drops_amount,
        "txHash":      tx_hash,
        "reason":      reason,
        "chain":       cfg.name,
    }


# ─── Redeem reward drops ──────────────────────────────────────────────────────

async def redeem_reward_drops(
    pool:            asyncpg.Pool,
    wallet:          str,
    drops_to_redeem: float,
    player_g:        float,
    fee_g:           float,
    staked_drops:    float,
    apy_pct:         int,
    g_price_usd:     float,
    notif:           Optional["NotificationService"] = None,
    chain_id:        int | None = None,
) -> dict:
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    if not cfg.drops_redeem_pool:
        raise ValueError(
            f"Redeem is not available on {cfg.name} yet. Switch to Celo."
        )

    # Redeem-pool actions require the Redeem Badge on this chain.
    await _require_redeem_badge(pool, wallet, cfg.id)

    staked_drops_wei = int(staked_drops * (10 ** DROPS_DECIMALS))
    g_to_player_wei  = int(player_g     * (10 ** DROPS_DECIMALS))
    fee_g_wei        = int(fee_g        * (10 ** DROPS_DECIMALS))

    w3, account   = build_w3_for_chain(cfg.id)
    token         = w3.eth.contract(address=Web3.to_checksum_address(cfg.drops_token), abi=DROPS_TOKEN_ABI)
    pool_cs       = Web3.to_checksum_address(cfg.drops_redeem_pool)
    pool_contract = w3.eth.contract(address=pool_cs, abi=DROP_POOL_ABI)
    wallet_cs     = Web3.to_checksum_address(wallet)

    loop = asyncio.get_event_loop()
    pool_g_bal = await loop.run_in_executor(None, pool_contract.functions.poolGBalance().call)
    pool_d_bal = await loop.run_in_executor(None, pool_contract.functions.poolDropsBalance().call)
    logger.info(
        "redeem preflight  pool_g=%s  pool_drops=%s  need_g=%s  need_drops=%s  chain=%s",
        pool_g_bal / 1e18, pool_d_bal / 1e18,
        (g_to_player_wei + fee_g_wei) / 1e18,
        staked_drops_wei / 1e18,
        cfg.name,
    )

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        await conn.execute(
            """UPDATE challenge_player_balances
               SET reward_drops=reward_drops-$1, updated_at=NOW()
               WHERE wallet_address=$2 AND chain_id=$3""",
            drops_to_redeem, wallet, cfg.id,
        )
        # Mirror
        await conn.execute(
            "UPDATE challenge_players SET reward_drops=reward_drops-$1 WHERE wallet_address=$2",
            drops_to_redeem, wallet,
        )

    try:
        await send_tx(
            build_fn=lambda nonce: token.functions.mintTo(
                pool_cs, staked_drops_wei
            ).build_transaction({"from": account.address, "nonce": nonce, "gas": 120_000}),
            account=account, w3=w3, chain_id=cfg.id, label="redeem:mintStake",
        )

        receipt = await send_tx(
            build_fn=lambda nonce: pool_contract.functions.redeemForPlayer(
                wallet_cs, staked_drops_wei, int(apy_pct), g_to_player_wei, fee_g_wei,
            ).build_transaction({"from": account.address, "nonce": nonce}),
            account=account, w3=w3, chain_id=cfg.id, label="redeem:redeemForPlayer",
        )
        tx_hash = receipt["transactionHash"].hex()

        redeemed_event  = pool_contract.events.Redeemed().process_receipt(receipt)
        onchain_stake_id = int(redeemed_event[0]["args"]["stakeId"])

    except Exception as e:
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_player_balances
                   SET reward_drops=reward_drops+$1, updated_at=NOW()
                   WHERE wallet_address=$2 AND chain_id=$3""",
                drops_to_redeem, wallet, cfg.id,
            )
            await conn.execute(
                "UPDATE challenge_players SET reward_drops=reward_drops+$1 WHERE wallet_address=$2",
                drops_to_redeem, wallet,
            )
        logger.error("redeem FAILED  wallet=%s  chain=%s  error=%s", wallet, cfg.name, e)
        raise

    try:
        async with pool.acquire() as conn:
            stake_row = await conn.fetchrow(
                """INSERT INTO challenge_stake_pools
                   (wallet_address, chain_id, drops_staked, apy_pct, g_price_at_stake,
                    g_value_usd, matures_at, onchain_stake_id)
                   VALUES ($1,$2,$3,$4,$5,$6, NOW() + INTERVAL '30 days', $7)
                   RETURNING id""",
                wallet, cfg.id, staked_drops, float(apy_pct), g_price_usd,
                staked_drops / 100.0, onchain_stake_id,
            )
            await conn.execute(
                """INSERT INTO challenge_redeem_history
                   (wallet_address, chain_id, drops_burned, g_price_usd, player_g, fee_g,
                    staked_g, staked_drops, stake_pool_id, tx_hash)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                wallet, cfg.id, drops_to_redeem, g_price_usd, player_g, fee_g,
                staked_drops / 100.0, staked_drops, stake_row["id"], tx_hash,
            )
    except Exception as e:
        logger.error(
            "DB insert FAILED after confirmed tx  wallet=%s  chain=%s  tx=%s  error=%s",
            wallet, cfg.name, tx_hash, e,
        )
        raise

    logger.info(
        "redeem OK  wallet=%s  chain=%s  drops=%s  player_g=%s  onchain_stake_id=%s  tx=%s",
        wallet, cfg.name, drops_to_redeem, player_g, onchain_stake_id, tx_hash,
    )

    # Record the reward_drops debit (redeem start) and the stake creation.
    asyncio.create_task(_act.record_many(pool, [
        {
            "wallet":      wallet,
            "chain_id":    cfg.id,
            "event":       _act.EVENT_REDEEM_START,
            "pouch":       _act.POUCH_REWARD,
            "drops_delta": -drops_to_redeem,
            "g_delta":     player_g,
            "tx_hash":     tx_hash,
            "metadata":    {"g_price_usd": g_price_usd, "fee_g": fee_g, "apy_pct": apy_pct},
        },
        {
            "wallet":      wallet,
            "chain_id":    cfg.id,
            "event":       _act.EVENT_STAKE_CREATED,
            "pouch":       None,
            "drops_delta": staked_drops,
            "tx_hash":     tx_hash,
            "metadata":    {
                "onchain_stake_id": onchain_stake_id,
                "apy_pct":          apy_pct,
                "g_price_usd":      g_price_usd,
                "stake_pool_id":    str(stake_row["id"]),
            },
        },
    ]))

    return {
        "success":       True,
        "dropsRedeemed": drops_to_redeem,
        "stakedDrops":   staked_drops,
        "playerG":       player_g,
        "feeG":          fee_g,
        "apyPct":        apy_pct,
        "txHash":        tx_hash,
        "stakeId":       str(stake_row["id"]),
        "chain":         cfg.name,
    }


# ─── Buy DROPS with $G ────────────────────────────────────────────────────────

async def buy_drops_with_g(
    pool:              asyncpg.Pool,
    wallet:            str,
    g_tx_hash:         str,
    expected_g_amount: float,
    drops_amount:      float | None = None,
    chain_id:          int | None = None,
) -> dict:
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    if not cfg.g_token:
        raise ValueError(f"$G purchases aren't available on {cfg.name} yet. Switch to Celo.")

    if not g_tx_hash or not g_tx_hash.startswith("0x"):
        raise ValueError("Invalid gTxHash")

    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM challenge_buy_drops_history WHERE tx_hash=$1", g_tx_hash,
        )
    if exists:
        raise ValueError("This transaction has already been processed")

    tx_ok = await _quick_verify_tx_success(g_tx_hash, chain_id=cfg.id)
    if not tx_ok:
        raise ValueError(
            "Transaction not found or failed on-chain. "
            "Ensure the $G transfer confirmed before submitting."
        )

    g_price_usd: float | None = None
    if drops_amount is None:
        try:
            g_price_usd = await _fetch_g_price_usd()
            drops_amount = expected_g_amount * g_price_usd * DROPS_PER_USD
        except Exception as e:
            logger.error("buy_drops: price fetch failed: %s", e)
            raise ValueError(
                "Could not fetch $G price to calculate DROPS amount. "
                "Please include dropsAmount in your request."
            ) from e

    if g_price_usd is None:
        g_price_usd = (expected_g_amount / drops_amount) * DROPS_PER_USD if drops_amount else 0.0

    amount_wei = int(drops_amount * (10 ** DROPS_DECIMALS))

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        await conn.execute(
            """UPDATE challenge_player_balances
               SET game_drops=game_drops+$1, updated_at=NOW()
               WHERE wallet_address=$2 AND chain_id=$3""",
            drops_amount, wallet, cfg.id,
        )
        await conn.execute(
            "UPDATE challenge_players SET game_drops=game_drops+$1 WHERE wallet_address=$2",
            drops_amount, wallet,
        )
        await conn.execute(
            """INSERT INTO challenge_buy_drops_history
               (wallet_address, chain_id, g_paid, g_price_usd, drops_minted, tx_hash, status)
               VALUES ($1,$2,$3,$4,$5,$6,'pending_mint')""",
            wallet, cfg.id, expected_g_amount, g_price_usd, drops_amount, g_tx_hash,
        )

    try:
        mint_tx = await _mint_to(wallet, amount_wei, label="buy_drops:mintTo", chain_id=cfg.id)
    except Exception as e:
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_player_balances
                   SET game_drops=game_drops-$1, updated_at=NOW()
                   WHERE wallet_address=$2 AND chain_id=$3""",
                drops_amount, wallet, cfg.id,
            )
            await conn.execute(
                "UPDATE challenge_players SET game_drops=game_drops-$1 WHERE wallet_address=$2",
                drops_amount, wallet,
            )
            await conn.execute(
                "UPDATE challenge_buy_drops_history SET status='failed' WHERE tx_hash=$1",
                g_tx_hash,
            )
        logger.error("buy mintTo FAILED  wallet=%s  chain=%s  error=%s", wallet, cfg.name, e)
        raise RuntimeError(f"Mint failed: {e}") from e

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_buy_drops_history SET status='confirmed', mint_tx_hash=$1 WHERE tx_hash=$2",
            mint_tx, g_tx_hash,
        )

    logger.info(
        "buy DROPS OK  wallet=%s  drops=%s  g_paid=%s  chain=%s  g_tx=%s  mint_tx=%s",
        wallet, drops_amount, expected_g_amount, cfg.name, g_tx_hash, mint_tx,
    )

    # Record the $G → DROPS purchase (credit to game pouch, debit of $G).
    asyncio.create_task(_act.record(
        pool        = pool,
        wallet      = wallet,
        chain_id    = cfg.id,
        event       = _act.EVENT_BUY_DROPS,
        pouch       = _act.POUCH_GAME,
        drops_delta = drops_amount,
        g_delta     = -expected_g_amount,
        tx_hash     = mint_tx,
        metadata    = {"g_price_usd": g_price_usd, "g_tx_hash": g_tx_hash},
    ))

    return {
        "success":     True,
        "dropsMinted": drops_amount,
        "gPaid":       expected_g_amount,
        "mintTxHash":  mint_tx,
        "chain":       cfg.name,
    }


# ─── Claim stake ──────────────────────────────────────────────────────────────

async def claim_stake_db(
    pool:     asyncpg.Pool,
    wallet:   str,
    stake_id: str,
    notif:    Optional["NotificationService"] = None,
    chain_id: int | None = None,
) -> dict:
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    if not cfg.drops_redeem_pool:
        raise ValueError(
            f"Stake claims are not available on {cfg.name} yet. Switch to Celo."
        )

    # Redeem-pool actions require the Redeem Badge on this chain.
    await _require_redeem_badge(pool, wallet, cfg.id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM challenge_stake_pools WHERE id=$1 AND wallet_address=$2",
            stake_id, wallet,
        )

    if not row:
        raise ValueError("Stake not found or not owned by this wallet")
    if row["claimed"]:
        raise ValueError("Stake already claimed")

    now = datetime.datetime.utcnow().replace(tzinfo=row["matures_at"].tzinfo)
    if row["matures_at"] > now:
        raise ValueError(f"Stake not matured yet. Matures at {row['matures_at'].isoformat()}")

    staked_drops = float(row["drops_staked"])
    apy_pct      = float(row["apy_pct"])
    g_price_usd  = await _fetch_g_price_usd()
    earned_drops = staked_drops * (apy_pct / 100.0)
    earned_g     = _drops_to_g(earned_drops, g_price_usd)
    earned_g_wei = int(earned_g * (10 ** DROPS_DECIMALS))
    principal_g  = _drops_to_g(staked_drops, g_price_usd)

    w3, account   = build_w3_for_chain(cfg.id)
    principal_wei = int(staked_drops * (10 ** DROPS_DECIMALS))
    wallet_cs     = Web3.to_checksum_address(wallet)

    token = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.drops_token),
        abi=DROPS_TOKEN_ABI,
    )
    pool_contract = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.drops_redeem_pool),
        abi=DROP_POOL_ABI,
    )

    # 1. Mint principal DROPS back
    try:
        receipt  = await send_tx(
            build_fn=lambda nonce: token.functions.mintTo(wallet_cs, principal_wei).build_transaction({
                "from": account.address, "nonce": nonce, "gas": 120_000,
            }),
            account=account, w3=w3, chain_id=cfg.id,
            label="claim_stake:principal",
        )
        mint_tx = receipt["transactionHash"].hex()
    except Exception as e:
        logger.error("claim principal mint FAILED  wallet=%s  stake_id=%s  chain=%s  error=%s", wallet, stake_id, cfg.name, e)
        raise RuntimeError(f"Principal mint failed: {e}") from e

    # 2. releaseCapital
    try:
        receipt = await send_tx(
            build_fn=lambda nonce: pool_contract.functions.releaseCapital(
                int(stake_id), earned_g_wei,
            ).build_transaction({
                "from": account.address, "nonce": nonce, "gas": 200_000,
            }),
            account=account, w3=w3, chain_id=cfg.id,
            label="claim_stake:releaseCapital",
        )
        release_tx = receipt["transactionHash"].hex()
    except Exception as e:
        logger.error("releaseCapital FAILED  wallet=%s  stake_id=%s  chain=%s  error=%s", wallet, stake_id, cfg.name, e)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE challenge_stake_pools SET principal_returned=TRUE, mint_tx_hash=$1 WHERE id=$2",
                mint_tx, stake_id,
            )
        raise RuntimeError(f"APY release failed (principal already returned): {e}") from e

    # 3. DB update
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_stake_pools
               SET claimed=TRUE, claimed_at=NOW(),
                   earned_drops=$1, earned_g=$2,
                   g_price_at_claim=$3,
                   mint_tx_hash=$4, release_tx_hash=$5,
                   principal_returned=TRUE
               WHERE id=$6""",
            earned_drops, earned_g, g_price_usd, mint_tx, release_tx, stake_id,
        )
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        await conn.execute(
            """UPDATE challenge_player_balances
               SET game_drops=game_drops+$1, updated_at=NOW()
               WHERE wallet_address=$2 AND chain_id=$3""",
            staked_drops, wallet, cfg.id,
        )
        await conn.execute(
            "UPDATE challenge_players SET game_drops=game_drops+$1 WHERE wallet_address=$2",
            staked_drops, wallet,
        )

    logger.info(
        "stake claim OK  wallet=%s  stake_id=%s  principal_drops=%s  "
        "earned_g=%s  chain=%s  mint_tx=%s  release_tx=%s",
        wallet, stake_id, staked_drops, earned_g, cfg.name, mint_tx, release_tx,
    )

    if notif:
        try:
            await notif.notify_stake_claimed(
                wallet=wallet, stake_id=stake_id,
                principal_drops=staked_drops, earned_g=earned_g,
                apy_pct=int(apy_pct), mint_tx=mint_tx, release_tx=release_tx,
            )
        except Exception as e:
            logger.warning("notify_stake_claimed failed wallet=%s: %s", wallet, e)

    # Record the matured stake claim — principal returns to game_drops,
    # earned APY is paid out in $G.
    asyncio.create_task(_act.record(
        pool        = pool,
        wallet      = wallet,
        chain_id    = cfg.id,
        event       = _act.EVENT_STAKE_CLAIMED,
        pouch       = _act.POUCH_GAME,
        drops_delta = staked_drops,
        g_delta     = earned_g,
        tx_hash     = release_tx,
        metadata    = {
            "stake_id":    stake_id,
            "apy_pct":     apy_pct,
            "g_price_usd": g_price_usd,
            "mint_tx":     mint_tx,
        },
    ))

    return {
        "success":        True,
        "stakeId":        stake_id,
        "principalDrops": staked_drops,
        "earnedDrops":    earned_drops,
        "earnedG":        earned_g,
        "principalG":     principal_g,
        "gPriceUsd":      g_price_usd,
        "apyPct":         apy_pct,
        "mintTx":         mint_tx,
        "releaseTx":      release_tx,
        "chain":          cfg.name,
    }


# ─── Game drops deduction / check ─────────────────────────────────────────────

async def deduct_game_drops(
    pool:     asyncpg.Pool,
    wallet:   str,
    amount:   float,
    reason:   str = "stake",
    chain_id: int | None = None,
    ref_code: Optional[str] = None,
) -> None:
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        updated = await conn.execute(
            """UPDATE challenge_player_balances
               SET game_drops=game_drops-$1, updated_at=NOW()
               WHERE wallet_address=$2 AND chain_id=$3
                 AND game_drops >= $1""",
            amount, wallet, cfg.id,
        )

    if updated == "UPDATE 0":
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT game_drops FROM challenge_player_balances
                   WHERE wallet_address=$1 AND chain_id=$2""",
                wallet, cfg.id,
            )
        available = float(row["game_drops"]) if row else 0.0
        raise ValueError(
            f"Insufficient game DROPS on {cfg.name}. "
            f"Required: {amount}, Available: {available:.2f}"
        )

    # Mirror to global row (best-effort, non-critical)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE challenge_players SET game_drops=game_drops-$1 WHERE wallet_address=$2",
                amount, wallet,
            )
    except Exception as e:
        logger.warning("deduct_game_drops global mirror failed wallet=%s: %s", wallet, e)

    logger.info("game_drops deducted  wallet=%s  chain=%s  amount=%s  reason=%s", wallet, cfg.name, amount, reason)

    # Record the debit so it shows up in the per-chain activity ledger.
    asyncio.create_task(_act.record(
        pool        = pool,
        wallet      = wallet,
        chain_id    = cfg.id,
        event       = _act.EVENT_STAKE_DEDUCTED,
        pouch       = _act.POUCH_GAME,
        drops_delta = -amount,
        ref_code    = ref_code,
        metadata    = {"reason": reason},
    ))


async def check_game_drops_balance(
    pool:     asyncpg.Pool,
    wallet:   str,
    amount:   float,
    chain_id: int | None = None,
) -> None:
    cfg    = get_chain(chain_id)
    wallet = wallet.lower()

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        row = await conn.fetchrow(
            "SELECT game_drops FROM challenge_player_balances WHERE wallet_address=$1 AND chain_id=$2",
            wallet, cfg.id,
        )
    available = float(row["game_drops"]) if row else 0.0
    if available < amount:
        raise ValueError(
            f"Insufficient game DROPS on {cfg.name}. "
            f"Required: {amount}, Available: {available:.2f}"
        )


# ─── Pre-badge enforcement ────────────────────────────────────────────────────

async def check_pre_badge_rules(
    pool:         asyncpg.Pool,
    wallet_a:     str,
    wallet_b:     str,
    stake_amount: float,
    chain_id:     int | None = None,   # ← NEW
) -> str | None:
    wallet_a = wallet_a.lower()
    wallet_b = wallet_b.lower()
    cfg      = get_chain(chain_id)     # ← NEW

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT b.wallet_address,
                      COALESCE(b.total_duels, 0)            AS total_duels,
                      COALESCE(b.rematch_badge, false)       AS rematch_badge
               FROM challenge_player_balances b
               WHERE b.wallet_address = ANY($1::text[])
                 AND b.chain_id = $2""",
            [wallet_a, wallet_b], cfg.id,   # ← per-chain query
        )

    player_map = {r["wallet_address"]: r for r in rows}

    for wallet in [wallet_a, wallet_b]:
        p = player_map.get(wallet)
        if not p:
            continue
        if not p["rematch_badge"] and stake_amount > PRE_BADGE_MAX_STAKE:
            return (
                f"Players without the Rematch Badge cannot stake more than "
                f"{PRE_BADGE_MAX_STAKE} DROPS per game."
            )

    needs_check = any(
        not player_map.get(w, {}).get("rematch_badge", False)
        for w in [wallet_a, wallet_b]
    )
    if needs_check:
        key_a = min(wallet_a, wallet_b)
        key_b = max(wallet_a, wallet_b)
        async with pool.acquire() as conn:
            played = await conn.fetchval(
                "SELECT 1 FROM challenge_player_matchups WHERE wallet_a=$1 AND wallet_b=$2",
                key_a, key_b,
            )
        if played:
            return (
                "These two players have already faced each other. "
                "Earn the Rematch Badge (play 10 games) to play again."
            )

    return None

async def record_matchup(pool: asyncpg.Pool, wallet_a: str, wallet_b: str) -> None:
    key_a = min(wallet_a.lower(), wallet_b.lower())
    key_b = max(wallet_a.lower(), wallet_b.lower())
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_player_matchups (wallet_a, wallet_b)
               VALUES ($1, $2) ON CONFLICT DO NOTHING""",
            key_a, key_b,
        )


# ─── Badge unlock ─────────────────────────────────────────────────────────────

async def maybe_notify_badge_unlock(
    pool:     asyncpg.Pool,
    wallet:   str,
    notif:    "NotificationService",
    chain_id: int | None = None,        # ← ADD
) -> None:
    if not chain_id:
        logger.warning("maybe_notify_badge_unlock called without chain_id — skipping")
        return

    wallet = wallet.lower()
    BADGE_THRESHOLD = 10

    async with pool.acquire() as conn:
        # ── Check per-chain duel count + badge status ─────────────────────
        row = await conn.fetchrow(
            """SELECT b.total_duels,
                      COALESCE(b.rematch_badge, false) AS rematch_badge
               FROM challenge_player_balances b
               WHERE b.wallet_address = $1
                 AND b.chain_id       = $2""",
            wallet, chain_id,
        )
        if not row:
            return
        if row["rematch_badge"]:
            return   # already earned on this chain
        if row["total_duels"] < BADGE_THRESHOLD:
            return   # not yet

        # ── Grant badge on this chain ─────────────────────────────────────
        updated = await conn.fetchval(
            """UPDATE challenge_player_balances
               SET rematch_badge = true
               WHERE wallet_address = $1
                 AND chain_id       = $2
                 AND rematch_badge IS NOT TRUE   -- avoid double-notify race
               RETURNING wallet_address""",
            wallet, chain_id,
        )
        if not updated:
            return   # another task already set it

    logger.info("rematch_badge unlocked  wallet=%s  chain=%s  total_duels=%s",
                wallet, chain_id, row["total_duels"])
    await notif.notify_badge_unlocked(wallet, int(row["total_duels"] or 0))


async def maybe_notify_redeem_badge_unlock(
    pool:     asyncpg.Pool,
    wallet:   str,
    notif:    "NotificationService",
    chain_id: int | None = None,
) -> None:
    """
    Grant the Redeem Badge once a wallet has REDEEM_BADGE_QUALIFYING_GAMES
    qualifying games on this chain (1v1 duels + solo games vs difficulty >= 3).
    qualifying_duels is incremented in quiz_engine._mark_finished().
    """
    if not chain_id:
        logger.warning("maybe_notify_redeem_badge_unlock called without chain_id — skipping")
        return

    wallet = wallet.lower()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(b.qualifying_duels, 0)      AS qualifying_duels,
                      COALESCE(b.redeem_badge, false)       AS redeem_badge
               FROM challenge_player_balances b
               WHERE b.wallet_address = $1
                 AND b.chain_id       = $2""",
            wallet, chain_id,
        )
        if not row:
            return
        if row["redeem_badge"]:
            return   # already earned on this chain
        if row["qualifying_duels"] < REDEEM_BADGE_QUALIFYING_GAMES:
            return   # not yet

        updated = await conn.fetchval(
            """UPDATE challenge_player_balances
               SET redeem_badge = true
               WHERE wallet_address = $1
                 AND chain_id       = $2
                 AND redeem_badge IS NOT TRUE   -- avoid double-notify race
               RETURNING wallet_address""",
            wallet, chain_id,
        )
        if not updated:
            return   # another task already set it

    logger.info("redeem_badge unlocked  wallet=%s  chain=%s  qualifying_duels=%s",
                wallet, chain_id, row["qualifying_duels"])
    try:
        await notif.notify_redeem_badge_unlocked(wallet, int(row["qualifying_duels"] or 0))
    except Exception as e:
        logger.warning("redeem badge notify failed wallet=%s: %s", wallet, e)


async def _require_redeem_badge(pool: asyncpg.Pool, wallet: str, chain_id: int) -> None:
    """
    Raise ValueError if the wallet has NOT earned the Redeem Badge on this
    chain. Called at the top of every redeem-pool action.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(redeem_badge, false)    AS redeem_badge,
                      COALESCE(qualifying_duels, 0)     AS qualifying_duels
               FROM challenge_player_balances
               WHERE wallet_address = $1 AND chain_id = $2""",
            wallet.lower(), chain_id,
        )
    if not row or not row["redeem_badge"]:
        played  = int(row["qualifying_duels"]) if row else 0
        remaining = max(0, REDEEM_BADGE_QUALIFYING_GAMES - played)
        raise ValueError(
            f"Redeem Badge required. Play {remaining} more qualifying game(s) "
            f"(1v1 duels, or solo vs Downpour/Torrent/Flood) to unlock redeeming."
        )

# ─── Weekly stats ─────────────────────────────────────────────────────────────

async def increment_weekly_stats(
    pool:        asyncpg.Pool,
    winner:      str | None,
    all_wallets: list[str],
    chain_id:    int | None = None,   # ← ADD
) -> None:
    if not chain_id:
        logger.warning("increment_weekly_stats called without chain_id — skipping")
        return

    async with pool.acquire() as conn:
        # +1 weekly duel for all participants on this chain
        await conn.executemany(
            """INSERT INTO challenge_player_balances (wallet_address, chain_id, weekly_duels)
               VALUES ($1, $2, 1)
               ON CONFLICT (wallet_address, chain_id)
               DO UPDATE SET weekly_duels = challenge_player_balances.weekly_duels + 1""",
            [(w.lower(), chain_id) for w in all_wallets],
        )
        if winner:
            await conn.execute(
                """INSERT INTO challenge_player_balances (wallet_address, chain_id, weekly_wins)
                   VALUES ($1, $2, 1)
                   ON CONFLICT (wallet_address, chain_id)
                   DO UPDATE SET weekly_wins = challenge_player_balances.weekly_wins + 1""",
                winner.lower(), chain_id,
            )

# ─── Tie credit ───────────────────────────────────────────────────────────────

async def credit_tie_refund(
    pool:       asyncpg.Pool,
    wallet_a:   str,
    wallet_b:   str,
    drops_each: float,
    chain_id:   int | None = None,
) -> None:
    for wallet in [wallet_a, wallet_b]:
        await credit_game_reward(pool, wallet, drops_each, reason="tie_refund", chain_id=chain_id)


# ─── On-chain balance read ────────────────────────────────────────────────────

async def get_onchain_drops_balance(wallet: str, chain_id: int | None = None) -> float:
    cfg = get_chain(chain_id)
    if not cfg.drops_token:
        return 0.0

    w3, _ = build_w3_for_chain(cfg.id)
    token  = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.drops_token),
        abi=DROPS_TOKEN_ABI,
    )
    wallet_cs = Web3.to_checksum_address(wallet)

    loop        = asyncio.get_event_loop()
    balance_wei = await loop.run_in_executor(
        None, lambda: token.functions.balanceOf(wallet_cs).call()
    )
    return balance_wei / (10 ** DROPS_DECIMALS)


# ─── TX verification ──────────────────────────────────────────────────────────

async def _quick_verify_tx_success(tx_hash: str, chain_id: int | None = None) -> bool:
    cfg = get_chain(chain_id)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(cfg.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "eth_getTransactionReceipt",
                "params":  [tx_hash],
            })
            receipt = r.json().get("result")
        return receipt is not None and receipt.get("status") == "0x1"
    except Exception as e:
        logger.warning("TX verification failed on %s: %s", cfg.name, e)
        return False


# ─── Stake read helpers ───────────────────────────────────────────────────────

async def get_player_stakes_db(
    pool:     asyncpg.Pool,
    wallet:   str,
    chain_id: int | None = None,
) -> list:
    wallet = wallet.lower()
    cfg    = get_chain(chain_id)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, chain_id, drops_staked, apy_pct, g_price_at_stake,
                      staked_at, matures_at, claimed, claimed_at,
                      earned_drops, earned_g, g_price_at_claim,
                      principal_returned, mint_tx_hash, release_tx_hash
               FROM challenge_stake_pools
               WHERE wallet_address=$1 AND chain_id=$2
               ORDER BY staked_at DESC""",
            wallet, cfg.id,
        )

    results = []
    for r in rows:
        d             = dict(r)
        d["id"]       = str(d["id"])
        d["staked_at"]  = d["staked_at"].isoformat()
        d["matures_at"] = d["matures_at"].isoformat()
        d["matured"]    = datetime.datetime.utcnow().replace(
            tzinfo=r["matures_at"].tzinfo
        ) >= r["matures_at"]
        if d["claimed_at"]:
            d["claimed_at"] = d["claimed_at"].isoformat()
        results.append(d)
    return results


# ─── Stake maturity background checker ───────────────────────────────────────

async def stake_maturity_check_loop(
    pool:             asyncpg.Pool,
    notif:            "NotificationService",
    interval_minutes: int = 60,
) -> None:
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT id, wallet_address, drops_staked, apy_pct
                       FROM challenge_stake_pools
                       WHERE claimed=FALSE AND maturity_notified=FALSE AND matures_at<=NOW()""",
                )
            for row in rows:
                try:
                    await notif.notify_stake_matured(
                        wallet       = row["wallet_address"],
                        stake_id     = str(row["id"]),
                        staked_drops = float(row["drops_staked"]),
                        apy_pct      = int(row["apy_pct"]),
                    )
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE challenge_stake_pools SET maturity_notified=TRUE WHERE id=$1",
                            row["id"],
                        )
                except Exception as e:
                    logger.warning("stake maturity notify failed stake=%s: %s", row["id"], e)
        except Exception as e:
            logger.error("stake_maturity_check_loop error: %s", e)


# ─── Weekly leaderboard reset ─────────────────────────────────────────────────

async def weekly_leaderboard_reset_loop(
    pool:  asyncpg.Pool,
    notif: Optional["NotificationService"] = None,
) -> None:
    while True:
        now            = datetime.datetime.utcnow()
        days_to_sunday = (6 - now.weekday()) % 7 or 7
        next_reset     = (now + datetime.timedelta(days=days_to_sunday)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        wait_secs = (next_reset - now).total_seconds()
        logger.info(
            "Weekly leaderboard reset in %.0f seconds (at %s UTC)",
            wait_secs, next_reset.isoformat(),
        )

        await asyncio.sleep(wait_secs)

        try:
            # ── 1. Notify top 3 winners BEFORE resetting (per-chain aware) ──
            if notif:
                try:
                    async with pool.acquire() as conn:
                        # Aggregate across all chains — pick best chain per wallet
                        # (wallet could have wins on multiple chains; sum them)
                        top_rows = await conn.fetch(
                            """SELECT
                                   b.wallet_address,
                                   COALESCE(p.username, 'Anonymous') AS username,
                                   SUM(COALESCE(b.weekly_wins,  0))  AS weekly_wins,
                                   SUM(COALESCE(b.weekly_duels, 0))  AS weekly_duels
                               FROM challenge_player_balances b
                               LEFT JOIN challenge_players p
                                   ON p.wallet_address = b.wallet_address
                               WHERE COALESCE(b.weekly_duels, 0) > 0
                               GROUP BY b.wallet_address, p.username
                               ORDER BY weekly_wins DESC, weekly_duels DESC
                               LIMIT 3""",
                        )
                    for rank, row in enumerate(top_rows, start=1):
                        await notif.notify_weekly_winner(
                            wallet      = row["wallet_address"],
                            username    = row["username"],
                            weekly_wins = int(row["weekly_wins"] or 0),
                            rank        = rank,
                        )
                except Exception as e:
                    logger.error("weekly winner notifications failed: %s", e)

            # ── 2. Snapshot + reset challenge_players (existing SQL fn) ─────
            async with pool.acquire() as conn:
                await conn.execute("SELECT challenge_reset_weekly_leaderboard()")

            # ── 3. Reset per-chain weekly counters in challenge_player_balances
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE challenge_player_balances
                       SET weekly_wins = 0, weekly_duels = 0"""
                )

            logger.info("Weekly leaderboard reset at %s UTC", datetime.datetime.utcnow())

        except Exception as e:
            logger.error("Weekly leaderboard reset FAILED: %s", e)