"""
sp_engine.py
────────────
Single-player mode helpers for FaucetDrops.

Responsibilities
────────────────
• _bot_pick_answer()       — decides the bot's answer for one question
• settle_sp_outcome()      — credits game_drops to the creator based on
                             win / tie / loss; never writes pending claims
                             and never touches the on-chain contracts

The bot is deliberately "off-chain" — its stake deduction is simulated
(just a DB record), its answers are generated here, and its "winnings"
are simply not credited (the creator's burned stake covers the pool).

All payouts go to the creator's game_drops pouch; there is no reward_drops
credit for single-player results (per spec).
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import asyncpg

from bot_config import BOT_PROFILES, get_bot_profile, HIGH_DIFFICULTY_MIN
from activity_service import record as _act_record
from activity_service import (
    EVENT_GAME_WIN, EVENT_GAME_TIE, EVENT_STAKE_DEDUCTED,
    POUCH_GAME, POUCH_REWARD,
)

logger = logging.getLogger(__name__)

# ── Score-state nudge thresholds ──────────────────────────────────────────────
# When the bot is behind by NUDGE_GAP points it gets a small accuracy boost,
# and when it's ahead by the same margin it gets a small accuracy penalty.
# This keeps games from snowballing too quickly while staying within the
# difficulty's intended win-rate band.

NUDGE_GAP       = 300   # point gap that triggers a nudge
NUDGE_AMOUNT    = 0.08  # accuracy delta applied as the nudge
MIN_ACCURACY    = 0.05
MAX_ACCURACY    = 0.97


# ── Bot answer selection ──────────────────────────────────────────────────────

def _bot_pick_answer(
    question:         dict,
    time_limit:       int,
    difficulty:       int,
    creator_points:   int,
    bot_points:       int,
    questions_left:   int,
) -> tuple[str, float]:
    """
    Pick an answer id and simulated time-taken for the bot this question.

    Returns
    -------
    (answer_id, time_taken_seconds)
    """
    profile       = get_bot_profile(difficulty)
    base_accuracy = profile["base_accuracy"]
    speed_max     = profile["speed_max_frac"]

    # ── Score-state nudge ────────────────────────────────────────────────
    nudge = 0.0
    if questions_left > 2:                     # don't nudge in the last 2 qs
        gap = bot_points - creator_points
        if gap < -NUDGE_GAP:
            nudge = +NUDGE_AMOUNT              # bot losing big → a bit smarter
        elif gap > NUDGE_GAP:
            nudge = -NUDGE_AMOUNT              # bot winning big → a bit dumber

    effective_accuracy = max(MIN_ACCURACY, min(MAX_ACCURACY, base_accuracy + nudge))

    correct_id = question["correctId"]
    wrong_ids  = [o["id"] for o in question["options"] if o["id"] != correct_id]

    # ── Decide correct vs wrong ──────────────────────────────────────────
    if random.random() < effective_accuracy:
        answer_id  = correct_id
        # Smarter bots answer faster → more speed bonus
        time_taken = random.uniform(0.4, time_limit * speed_max)
    else:
        answer_id  = random.choice(wrong_ids)
        # Wrong answers: slower (the bot "thought about it")
        time_taken = random.uniform(time_limit * 0.45, time_limit * 0.95)

    return answer_id, round(time_taken, 3)

async def _mint_to_bot(bot_wallet: str, amount: float, code: str, chain_id: int) -> None:
    """Mint won DROPS to bot wallet when bot wins a single-player game."""
    try:
        from drops_service import _mint_to
        amount_wei = int(amount * (10 ** 18))
        tx = await _mint_to(bot_wallet, amount_wei, label=f"SP:botWin:{code}", chain_id=chain_id)
        logger.info("Bot win mint OK  bot=%s  amount=%s  code=%s  tx=%s", bot_wallet, amount, code, tx)
    except Exception as e:
        logger.error("Bot win mint FAILED  bot=%s  code=%s  error=%s", bot_wallet, code, e)
# ── Outcome settlement ────────────────────────────────────────────────────────

async def settle_sp_outcome(
    pool:           asyncpg.Pool,
    creator_wallet: str,
    bot_wallet:     str,
    code:           str,
    stake:          float,
    winner:         Optional[str],   # wallet address of winner, or None for tie
    chain_id:       int,
    difficulty:     int,
) -> dict:
    """
    Settle a finished single-player game.

    - Creator win/tie  → mint payout on-chain to creator wallet + credit game_drops in DB
    - Bot win          → mint payout on-chain to bot wallet + credit bot game_drops in DB

    Both sides are symmetric: on-chain mint first, then DB game_drops credit + activity log.
    Returns a dict describing the payout, included in the game_over broadcast.
    """
    from drops_service import _ensure_chain_balance_row, _mint_to   # local import avoids circular

    creator_wallet = creator_wallet.lower()
    bot_wallet     = bot_wallet.lower()

    if winner == creator_wallet:
        payout      = stake * 2
        outcome     = "win"
        mint_wallet = creator_wallet
    elif winner is None:
        payout      = stake
        outcome     = "tie"
        mint_wallet = creator_wallet
    else:
        # bot wins
        payout      = stake * 2
        outcome     = "loss"
        mint_wallet = bot_wallet

    # ── Pouch routing ─────────────────────────────────────────────────────────
    # A creator WIN against a high-difficulty bot (Downpour / Torrent / Flood,
    # difficulty >= HIGH_DIFFICULTY_MIN) is credited to the REWARD pouch.
    # Ties (refunds), low-difficulty wins, and bot wins stay in the game pouch.
    high_diff_win = (outcome == "win" and difficulty >= HIGH_DIFFICULTY_MIN)
    pouch_column  = "reward_drops" if high_diff_win else "game_drops"
    pouch_label   = POUCH_REWARD   if high_diff_win else POUCH_GAME

    payout_wei = int(payout * (10 ** 18))

    # ── 1. Mint payout on-chain (same path for creator and bot) ──────────────
    mint_tx: Optional[str] = None
    try:
        mint_tx = await _mint_to(
            mint_wallet,
            payout_wei,
            label=f"SP:{'creatorWin' if outcome != 'loss' else 'botWin'}:{code}",
            chain_id=chain_id,
        )
        logger.info(
            "SP mint OK  code=%s  wallet=%s  outcome=%s  amount=%s  tx=%s",
            code, mint_wallet, outcome, payout, mint_tx,
        )
    except Exception as e:
        logger.error(
            "SP mint FAILED  code=%s  wallet=%s  outcome=%s  error=%s",
            code, mint_wallet, outcome, e,
        )

    # ── 2. Credit game_drops in DB for the receiving wallet ──────────────────
    credit_wallet = mint_wallet   # creator on win/tie, bot on loss
    try:
        async with pool.acquire() as conn:
            await _ensure_chain_balance_row(conn, credit_wallet, chain_id)
            await conn.execute(
                f"""
                UPDATE challenge_player_balances
                SET    {pouch_column} = {pouch_column} + $1
                WHERE  wallet_address = $2
                  AND  chain_id       = $3
                """,
                payout, credit_wallet, chain_id,
            )

        event = EVENT_GAME_WIN if outcome == "win" else EVENT_GAME_TIE
        asyncio.create_task(
            _act_record(
                pool        = pool,
                wallet      = credit_wallet,
                chain_id    = chain_id,
                event       = event,
                pouch       = pouch_label,
                drops_delta = payout,
                ref_code    = code,
                metadata    = {
                    "mode":       "single_player",
                    "difficulty": difficulty,
                    "opponent":   bot_wallet if credit_wallet == creator_wallet else creator_wallet,
                    "outcome":    outcome if credit_wallet == creator_wallet else "win",
                    "mint_tx":    mint_tx,
                },
            )
        )
        logger.info(
            "SP settle  code=%s  wallet=%s  outcome=%s  payout=%s  chain=%d  mint_tx=%s",
            code, credit_wallet, outcome, payout, chain_id, mint_tx,
        )
    except Exception as exc:
        logger.error(
            "SP settle DB FAILED  code=%s  wallet=%s  error=%s",
            code, credit_wallet, exc,
        )

    return {
        "singlePlayer": True,
        "result":       outcome,     # "win" | "loss" | "tie"  (creator POV)
        "payout":       payout,
        "pouch":        pouch_label,   # "reward" for high-difficulty wins, else "game"
        "difficulty":   difficulty,
        "mint_tx":      mint_tx,
    }
    
    
async def bot_stake_onchain(
    bot_private_key: str,
    bot_wallet:      str,
    creator_wallet:  str,
    code:            str,
    stake:           float,
    chain_id:        int,
) -> dict:
    """
    Called after creator has already called createQuiz() on-chain client-side.

    Steps handled here:
    1. resolver  → registerQuiz(quizId, creator, bot, stakeWei)
    2. bot       → redeem(stakeWei, code)
    3. resolver  → confirmBurn(quizId, creator)
    4. resolver  → confirmBurn(quizId, bot)

    Creator's redeem() is handled client-side via confirm-burn endpoint
    (same as multiplayer). confirmBurn for creator is called here after
    bot's redeem confirms, so both are confirmed in sequence.
    """
    from chain_config import get_chain
    from chain_utils import send_tx, build_w3
    from abi import DROPS_TOKEN_ABI, QUIZ_HUB_ABI
    from quiz_engine import _get_quiz_id
    from web3 import Web3
    from eth_account import Account

    cfg               = get_chain(chain_id)
    w3, resolver_acct = build_w3(cfg.id)

    quiz_id    = _get_quiz_id(code)
    stake_wei  = int(stake * (10 ** 18))
    creator_cs = Web3.to_checksum_address(creator_wallet)
    bot_cs     = Web3.to_checksum_address(bot_wallet)

    hub   = w3.eth.contract(address=Web3.to_checksum_address(cfg.quiz_hub),    abi=QUIZ_HUB_ABI)
    token = w3.eth.contract(address=Web3.to_checksum_address(cfg.drops_token), abi=DROPS_TOKEN_ABI)
    bot_account = Account.from_key(bot_private_key)

    results = {}

    # ── 1. resolver: registerQuiz(quizId, creator, bot, stakeWei) ────────────
    # createQuiz was already called by the creator client-side
    def _build_register(nonce: int) -> dict:
        return hub.functions.registerQuiz(
            quiz_id, creator_cs, bot_cs, stake_wei
        ).build_transaction({
            "from":  resolver_acct.address,
            "nonce": nonce,
            "gas":   150_000,
        })

    try:
        receipt = await send_tx(
            _build_register, resolver_acct, w3,
            label=f"SP:registerQuiz:{code}", chain_id=cfg.id,
        )
        results["register_tx"] = receipt["transactionHash"].hex()
        logger.info("SP registerQuiz OK  code=%s  tx=%s", code, results["register_tx"])
    except Exception as e:
        logger.error("SP registerQuiz FAILED  code=%s  error=%s", code, e)
        raise RuntimeError(f"registerQuiz failed: {e}") from e

    # ── 2. bot: redeem(stakeWei, code) ───────────────────────────────────────
    def _build_bot_redeem(nonce: int) -> dict:
        return token.functions.redeem(stake_wei, code).build_transaction({
            "from":  bot_cs,
            "nonce": nonce,
            "gas":   120_000,
        })

    try:
        receipt = await send_tx(
            _build_bot_redeem, bot_account, w3,
            label=f"SP:botRedeem:{code}", chain_id=cfg.id,
        )
        results["bot_redeem_tx"] = receipt["transactionHash"].hex()
        logger.info("SP botRedeem OK  code=%s  tx=%s", code, results["bot_redeem_tx"])
    except Exception as e:
        logger.error("SP botRedeem FAILED  code=%s  error=%s", code, e)
        raise RuntimeError(f"bot redeem failed: {e}") from e

    # ── 3. resolver: confirmBurn(quizId, bot) ────────────────────────────────
    def _build_confirm_bot(nonce: int) -> dict:
        return hub.functions.confirmBurn(quiz_id, bot_cs).build_transaction({
            "from":  resolver_acct.address,
            "nonce": nonce,
            "gas":   100_000,
        })

    try:
        receipt = await send_tx(
            _build_confirm_bot, resolver_acct, w3,
            label=f"SP:botConfirmBurn:{code}", chain_id=cfg.id,
        )
        results["bot_confirm_tx"] = receipt["transactionHash"].hex()
        logger.info("SP botConfirmBurn OK  code=%s  tx=%s", code, results["bot_confirm_tx"])
    except Exception as e:
        logger.error("SP botConfirmBurn FAILED  code=%s  error=%s", code, e)
        raise RuntimeError(f"bot confirmBurn failed: {e}") from e

    return results

# ── Simulated bot stake (DB-only, no real tx) ─────────────────────────────────

async def record_bot_stake(
    pool:       asyncpg.Pool,
    bot_wallet: str,
    stake:      float,
    code:       str,
    chain_id:   int,
    difficulty: int,
) -> None:
    """
    Record the bot's simulated stake in the activity log for completeness.
    No balance row is touched because the bot's balance is never real money.
    """
    try:
        await _act_record(
            pool        = pool,
            wallet      = bot_wallet,
            chain_id    = chain_id,
            event       = EVENT_STAKE_DEDUCTED,
            pouch       = POUCH_GAME,
            drops_delta = -stake,
            ref_code    = code,
            metadata    = {
                "mode":       "single_player",
                "difficulty": difficulty,
                "simulated":  True,
            },
        )
    except Exception as exc:
        logger.warning("record_bot_stake failed (non-critical): %s", exc)