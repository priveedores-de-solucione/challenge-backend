"""
quiz_engine.py
──────────────
Self-contained quiz logic: AI question generation, the async game loop,
and per-question scoring.

Token model (DROPS only)
─────────────────────────
  Stake  → player calls DropsToken.redeem(amount, quizCode)  [burns DROPS]
  Win    → backend calls DropsToken.claim(winner, amount*2, sig) [mints DROPS]
  Tie    → backend calls DropsToken.claim(each,   amount,   sig) [mints each]

On-chain resolution via QuizHub (activity ledger only, holds no tokens):
  Winner found → setWinner(quizId, winnerAddress)
  Tie          → declareTie(quizId)
Then immediately mint via DropsToken.claim().

Required env vars:
  RESOLVER_PRIVATE_KEY   — private key of the wallet set as `resolver` on QuizHub
  QUIZ_HUB_CONTRACT      — deployed QuizHub address
  DROPS_TOKEN_CONTRACT   — deployed DROPS token address
  CELO_RPC_URL           — defaults to https://forno.celo.org
"""
from __future__ import annotations
import os, json, asyncio, logging
from typing import Dict, Callable, Awaitable
import random  
from chain_config import get_chain
from chain_utils import send_tx, build_w3
import httpx
import asyncpg
import sp_engine
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from abi import DROPS_TOKEN_ABI, QUIZ_HUB_ABI
from notifications import NotificationService
import drops_service
from drops_service import (
    credit_game_reward, credit_tie_refund, record_matchup,
    increment_weekly_stats
)

logger = logging.getLogger(__name__)

# ─── Gemini / Groq config ─────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/gemini-2.5-flash:generateContent"
)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"

# ─── On-chain config ──────────────────────────────────────────────────────────
# Contract addresses and RPC URLs are resolved per-chain via chain_config.get_chain().
# Do NOT read QUIZ_HUB_CONTRACT / DROPS_TOKEN_CONTRACT / CELO_RPC_URL here —
# those are Celo-only globals and break Botchain support.

RESOLVER_PRIVATE_KEY = os.getenv("RESOLVER_PRIVATE_KEY", "")

DROPS_DECIMALS = 18

# ─── Round config ─────────────────────────────────────────────────────────────

ROUND_CONFIG = [
    {"type": "easy",   "timeLimit": 7},
    {"type": "medium", "timeLimit": 10},
    {"type": "hard",   "timeLimit": 13},
]

BASE_POINTS = 500
SPEED_BONUS = 500


# ─── AI Question Generation ───────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
        )
        resp.raise_for_status()
        raw  = resp.json()
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)


async def _call_groq(prompt: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {
                        "role":    "system",
                        "content": "You are a quiz generator. Return ONLY valid JSON, no markdown fences.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
            },
        )
        resp.raise_for_status()
        raw  = resp.json()
        text = raw["choices"][0]["message"]["content"]
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)


async def generate_questions(topic: str, total_count: int = 15) -> dict:
    per_round = total_count // 3
    prompt = f"""
Generate a 1v1 quiz challenge about "{topic}".
Create exactly {total_count} multiple-choice questions divided into 3 rounds:
- Round 1 (Easy):   {per_round} questions — straightforward recall.
- Round 2 (Medium): {per_round} questions — requires understanding.
- Round 3 (Hard):   {per_round} questions — tricky, nuanced, or analytical.
Important: keep all four options similar in length and level of detail.
The correct answer must NOT be noticeably longer, more specific, or more
hedged/qualified than the distractors. Distractors should be equally
plausible and equally detailed — avoid vague or obviously-short wrong answers.
Return ONLY valid JSON:
{{
  "rounds": [
    {{ "type": "easy",   "timeLimit": 7,  "questions": [ ... ] }},
    {{ "type": "medium", "timeLimit": 10, "questions": [ ... ] }},
    {{ "type": "hard",   "timeLimit": 13, "questions": [ ... ] }}
  ]
}}
Each question object:
{{
  "question": "Question text?",
  "options":  [ {{"id":"A","text":"..."}}, {{"id":"B","text":"..."}},
                {{"id":"C","text":"..."}}, {{"id":"D","text":"..."}} ],
  "correctId": "A"
}}
"""
    data = None
    if GEMINI_API_KEY:
        try:
            logger.info("generate_questions: trying Gemini...")
            data = await _call_gemini(prompt)
            logger.info("generate_questions: Gemini succeeded.")
        except Exception as e:
            logger.warning("Gemini failed (%s), falling back to Groq.", e)

    if data is None:
        if not GROQ_API_KEY:
            raise RuntimeError("Gemini unavailable and GROQ_API_KEY not set.")
        try:
            logger.info("generate_questions: trying Groq fallback...")
            data = await _call_groq(prompt)
            logger.info("generate_questions: Groq succeeded.")
        except Exception as e:
            raise RuntimeError(f"Both Gemini and Groq failed. Last error: {e}") from e

    for i, rnd in enumerate(data["rounds"]):
        rnd["timeLimit"] = ROUND_CONFIG[i]["timeLimit"]
        rnd["type"]      = ROUND_CONFIG[i]["type"]
        for q in rnd["questions"]:          # ← add this
            _shuffle_options(q)   
    return data


def strip_answers(rounds_data: dict) -> dict:
    import copy
    safe = copy.deepcopy(rounds_data)
    for rnd in safe.get("rounds", []):
        for q in rnd.get("questions", []):
            q.pop("correctId", None)
    return safe



def _shuffle_options(question: dict) -> dict:
    """
    Randomize option order server-side so position is never predictable,
    regardless of how the LLM ordered them.
    """
    options    = question["options"]
    correct_id = question["correctId"]

    for o in options:
        o["_correct"] = (o["id"] == correct_id)

    random.shuffle(options)

    ids = ["A", "B", "C", "D"]
    new_correct_id = None
    for i, o in enumerate(options):
        o["id"] = ids[i]
        if o.pop("_correct"):
            new_correct_id = o["id"]

    question["options"]   = options
    question["correctId"] = new_correct_id
    return question

# ─── Scoring ──────────────────────────────────────────────────────────────────

def calc_points(time_taken: float, time_limit: int) -> int:
    time_remaining = max(0.0, time_limit - time_taken)
    speed_factor   = time_remaining / time_limit
    return BASE_POINTS + int(SPEED_BONUS * speed_factor)


# ─── On-chain helpers ─────────────────────────────────────────────────────────

def _get_quiz_id(code: str) -> bytes:
    return Web3.keccak(text=code)
# ─── On-chain resolution: winner ─────────────────────────────────────────────

async def _set_winner_on_chain(
    code: str, winner_address: str, chain_id: int | None = None
) -> str | None:
    """Ledger-only — does NOT mint. Minting happens via the pending-claims flow."""
    cfg = get_chain(chain_id)
    if not RESOLVER_PRIVATE_KEY or not cfg.quiz_hub:
        logger.error("_set_winner_on_chain skipped — missing config for %s", cfg.name)
        return None

    w3, account = build_w3(cfg.id)
    hub = w3.eth.contract(address=Web3.to_checksum_address(cfg.quiz_hub), abi=QUIZ_HUB_ABI)
    quiz_id   = _get_quiz_id(code)
    winner_cs = Web3.to_checksum_address(winner_address)

    def _build(nonce: int) -> dict:
        return hub.functions.setWinner(quiz_id, winner_cs).build_transaction({
            "from": account.address, "nonce": nonce, "gas": 120_000,
        })

    try:
        receipt = await send_tx(_build, account, w3, label=f"setWinner:{code}", chain_id=cfg.id)
        tx_hash = receipt["transactionHash"].hex()
        logger.info("setWinner OK  code=%s  winner=%s  chain=%s  tx=%s", code, winner_address, cfg.name, tx_hash)
        return tx_hash
    except Exception as exc:
        logger.error("_set_winner_on_chain FAILED  code=%s  chain=%s  error=%s", code, cfg.name, exc)
        return None
 

# ─── On-chain resolution: tie (no mint — just ledger) ────────────────────────
 
async def _declare_tie_on_chain(code: str, chain_id: int | None = None) -> str | None:
    """Ledger-only — both players claim their refund via the pending-claims flow."""
    cfg = get_chain(chain_id)
    if not RESOLVER_PRIVATE_KEY or not cfg.quiz_hub:
        logger.error("_declare_tie_on_chain skipped — missing config for %s", cfg.name)
        return None

    w3, account = build_w3(cfg.id)
    hub     = w3.eth.contract(address=Web3.to_checksum_address(cfg.quiz_hub), abi=QUIZ_HUB_ABI)
    quiz_id = _get_quiz_id(code)

    def _build(nonce: int) -> dict:
        return hub.functions.declareTie(quiz_id).build_transaction({
            "from": account.address, "nonce": nonce, "gas": 120_000,
        })

    try:
        receipt = await send_tx(_build, account, w3, label=f"declareTie:{code}", chain_id=cfg.id)
        tx_hash = receipt["transactionHash"].hex()
        logger.info("declareTie OK  code=%s  chain=%s  tx=%s", code, cfg.name, tx_hash)
        return tx_hash
    except Exception as exc:
        logger.error("_declare_tie_on_chain FAILED  code=%s  chain=%s  error=%s", code, cfg.name, exc)
        return None
 
 
# ─── Pending claim writer ─────────────────────────────────────────────────────
 
async def _write_pending_claim(
    pool:         asyncpg.Pool,
    wallet:       str,
    code:         str,
    drops_amount: float,
    reason:       str,
) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO challenge_pending_claims
                   (wallet_address, game_code, drops_amount, reason)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (wallet_address, game_code) DO NOTHING""",
                wallet.lower(), code, drops_amount, reason,
            )
        logger.info(
            "pending claim written  wallet=%s  code=%s  drops=%s  reason=%s",
            wallet, code, drops_amount, reason,
        )
    except Exception as e:
        logger.error(                          # ← was silently dying before
            "pending claim FAILED  wallet=%s  code=%s  drops=%s  reason=%s  error=%s",
            wallet, code, drops_amount, reason, e,
        )
        raise   # re-raise so create_task logs it too
# ─── On-chain resolution: tie ─────────────────────────────────────────────────


# ─── Game Loop ────────────────────────────────────────────────────────────────

BroadcastFn = Callable[[str, dict], Awaitable[None]]


async def run_game_loop(
    code:               str,
    challenges:         dict,
    game_state:         dict,
    pool,
    broadcast,
    notif,
    single_player_cfg:  dict | None = None,   # ← NEW param (None = normal PvP)
) -> None:
    """
    Main game loop for both PvP and single-player modes.
 
    single_player_cfg — if provided, activates bot mode.  Expected keys:
        bot_wallet      str     bot's wallet address (from BOT_PROFILES)
        creator_wallet  str     human player's wallet address
        difficulty      int     1–5
    """
    import time as _time
    from quiz_engine import (                          # avoid circular if moved
        _mark_started, _mark_finished,
        _set_winner_on_chain, _declare_tie_on_chain,
        _write_pending_claim, _save_answer,
        calc_points,
    )
    import drops_service
    from drops_service import (
        credit_game_reward, credit_tie_refund, record_matchup,
        increment_weekly_stats,
    )
    from sp_engine import _bot_pick_answer, settle_sp_outcome
    from bot_config import BOT_WALLETS
 
    is_sp = single_player_cfg is not None
    bot_wallet     = single_player_cfg["bot_wallet"]     if is_sp else None
    creator_wallet = single_player_cfg["creator_wallet"] if is_sp else None
    difficulty     = single_player_cfg["difficulty"]     if is_sp else None
 
    challenge = challenges[code]
    state     = game_state[code]
    chain_id  = challenge.get("chainId")
    challenge["status"] = "active"
    await _mark_started(pool, code)
 
    await broadcast(code, {"type": "game_start", "message": "Challenge starting in 3…"})
    await asyncio.sleep(3)
 
    rounds = challenge["rounds"]
 
    # ── Pre-compute total question count for bot nudge logic ─────────────
    total_questions = sum(len(rnd["questions"]) for rnd in rounds)
    questions_done  = 0
 
    for r_idx, rnd in enumerate(rounds):
        await broadcast(code, {
            "type":        "round_announce",
            "round":       rnd["type"],
            "roundIndex":  r_idx,
            "totalRounds": len(rounds),
            "timeLimit":   rnd["timeLimit"],
        })
        await asyncio.sleep(3)
 
        for q_idx, q in enumerate(rnd["questions"]):
            current_q_payload = {
                "roundIndex":     r_idx,
                "questionIndex":  q_idx,
                "totalQuestions": len(rnd["questions"]),
                "roundName":      rnd["type"],
                "startedAt":      int(_time.time() * 1000),
                "data": {
                    "question":  q["question"],
                    "options":   q["options"],
                    "timeLimit": rnd["timeLimit"],
                },
            }
            state["currentQuestion"] = current_q_payload
 
            await broadcast(code, {"type": "question", **current_q_payload})
            await asyncio.sleep(rnd["timeLimit"])
 
            state["currentQuestion"] = None
 
            ans_key       = f"{r_idx}_{q_idx}"
            round_answers = state["answers"].get(ans_key, {})
 
            # ── [BOT HOOK] Inject bot answer for single-player mode ───────
            if is_sp and bot_wallet not in round_answers:
                questions_left = total_questions - questions_done - 1
                bot_answer_id, bot_time = _bot_pick_answer(
                    question       = q,
                    time_limit     = rnd["timeLimit"],
                    difficulty     = difficulty,
                    creator_points = challenge["players"].get(creator_wallet, {}).get("points", 0),
                    bot_points     = challenge["players"].get(bot_wallet, {}).get("points", 0),
                    questions_left = questions_left,
                )
                round_answers[bot_wallet] = {
                    "answerId":  bot_answer_id,
                    "timeTaken": bot_time,
                }
                state["answers"][ans_key] = round_answers
                logger.debug(
                    "BOT answer  code=%s  q=%d_%d  answer=%s  time=%.2f  difficulty=%d",
                    code, r_idx, q_idx, bot_answer_id, bot_time, difficulty,
                )
            # ── [END BOT HOOK] ────────────────────────────────────────────
 
            questions_done += 1
            q_scores: dict[str, int] = {}
 
            for wallet, ans in round_answers.items():
                correct = ans["answerId"] == q["correctId"]
                pts     = calc_points(ans["timeTaken"], rnd["timeLimit"]) if correct else 0
                challenge["players"][wallet]["points"] += pts
                q_scores[wallet] = pts
                asyncio.create_task(
                    _save_answer(pool, code, wallet, r_idx, q_idx,
                                 ans["answerId"], correct, ans["timeTaken"], pts)
                )
 
            await broadcast(code, {
                "type":           "question_end",
                "roundIndex":     r_idx,
                "questionIndex":  q_idx,
                "correctId":      q["correctId"],
                "questionScores": q_scores,
                "totalScores": {
                    w: p["points"]
                    for w, p in challenge["players"].items()
                },
            })
            await asyncio.sleep(3)
 
        await broadcast(code, {
            "type":       "round_end",
            "roundIndex": r_idx,
            "roundType":  rnd["type"],
            "scores":     {w: p["points"] for w, p in challenge["players"].items()},
        })
        await asyncio.sleep(4)
 
    # ── Determine outcome ─────────────────────────────────────────────────────
    players = challenge["players"]
    scores  = {w: p["points"] for w, p in players.items()}
    wallets = list(scores.keys())
 
    if scores[wallets[0]] == scores[wallets[1]]:
        winner  = None
        outcome = "tie"
    else:
        winner  = max(scores, key=scores.__getitem__)
        outcome = "winner"
 
    challenge["status"] = "finished"
    challenge["winner"] = winner
 
    await _mark_finished(pool, code, winner)
 
    # Badge unlocks — only for human wallets
    for _badge_wallet in wallets:
        if _badge_wallet not in BOT_WALLETS:
            asyncio.create_task(
                drops_service.maybe_notify_badge_unlock(
                    pool, _badge_wallet, notif, chain_id=chain_id
                )
            )
            asyncio.create_task(
                drops_service.maybe_notify_redeem_badge_unlock(
                    pool, _badge_wallet, notif, chain_id=chain_id
                )
            )
 
    stake_drops = float(challenge.get("stakeAmount", 0))
 
    # ── Record matchup + weekly stats ─────────────────────────────────────────
    if len(wallets) == 2:
        asyncio.create_task(record_matchup(pool, wallets[0], wallets[1]))
 
    # Only credit weekly stats for human wallets
    human_wallets = [w for w in wallets if w not in BOT_WALLETS]
    asyncio.create_task(
        increment_weekly_stats(pool, winner if winner not in BOT_WALLETS else None,
                               human_wallets, chain_id=chain_id)
    )
 
    # ── [SP OUTCOME] Single-player settlement ─────────────────────────────────
    if is_sp:
        sp_result = await settle_sp_outcome(
            pool           = pool,
            creator_wallet = creator_wallet,
            bot_wallet     = bot_wallet,
            code           = code,
            stake          = stake_drops,
            winner         = winner,
            chain_id       = chain_id or 42220,
            difficulty     = difficulty,
        )
 
        bot_profile = challenge.get("bot_profile", {})
 
        await broadcast(code, {
            "type":       "game_over",
            "outcome":    outcome,
            "winner":     winner,
            "finalScores": {
                w: {
                    "username": players[w]["username"],
                    "avatar_url": players[w].get("avatar_url", ""), 
                    "points":   players[w]["points"],
                }
                for w in wallets
            },
            "canRematch":    False,       # no rematch in single-player
            **sp_result,                  # singlePlayer, result, payout, pouch, difficulty
        })
 
        # Notify the human player
        if sp_result["result"] == "win":
            await notif.send(
                creator_wallet,
                "game_over",
                "🏆 You won!",
                f"You beat {bot_profile.get('username', 'the bot')} and earned {sp_result['payout']:.0f} DROPS!",
                {"code": code, "singlePlayer": True},
            )
        elif sp_result["result"] == "tie":
            await notif.send(
                creator_wallet,
                "game_over",
                "🤝 It's a tie!",
                f"Your {stake_drops:.0f} DROPS stake has been refunded.",
                {"code": code, "singlePlayer": True},
            )
        else:
            await notif.send(
                creator_wallet,
                "game_over",
                "💀 You lost!",
                f"Better luck next time. Try a lower difficulty to start!",
                {"code": code, "singlePlayer": True},
            )
 
        logger.info(
            "SP game finished  code=%s  winner=%s  result=%s  payout=%s",
            code, winner or "TIE", sp_result["result"], sp_result["payout"],
        )
        return   # ← Early return; skip all PvP claim / on-chain logic below.
    # ── [END SP OUTCOME] ──────────────────────────────────────────────────────
 
    # ── Normal PvP outcome ────────────────────────────────────────────────────
    if outcome == "winner" and winner:
        asyncio.create_task(_set_winner_on_chain(code, winner, chain_id))
 
        payout_drops = stake_drops * 2
        asyncio.create_task(_write_pending_claim(pool, winner, code, payout_drops, "game_win"))
 
        loser = next(w for w in wallets if w != winner)
        await notif.notify_game_over(code, winner, loser, stake_drops, "DROPS")
 
    else:
        p1, p2 = wallets[0], wallets[1]
        asyncio.create_task(_declare_tie_on_chain(code, chain_id))
 
        for wallet in [p1, p2]:
            asyncio.create_task(
                _write_pending_claim(pool, wallet, code, stake_drops, "tie_refund")
            )
 
        for wallet in wallets:
            await notif.send(
                wallet,
                "game_over",
                "🤝 It's a tie!",
                "The match ended in a draw. Click Claim to get your DROPS stake back.",
                {"code": code},
            )
 
    await broadcast(code, {
        "type":    "game_over",
        "outcome": outcome,
        "winner":  winner,
        "finalScores": {
            w: {
                "username": players[w]["username"],
                "points":   players[w]["points"],
            }
            for w in wallets
        },
        "canRematch": True,
    })
    logger.info("Game %s finished. Winner: %s", code, winner or "TIE")
 

# ─── DB Helpers ───────────────────────────────────────────────────────────────

async def _mark_started(pool: asyncpg.Pool, code: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_games SET status='active', started_at=NOW() WHERE code=$1",
            code,
        )


async def _mark_finished(pool: asyncpg.Pool, code: str, winner: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_games
               SET status='finished', winner_address=$1, finished_at=NOW()
               WHERE code=$2""",
            winner, code,
        )

        # ── Global counters ───────────────────────────────────────────────
        await conn.execute(
            """UPDATE challenge_players SET total_duels = COALESCE(total_duels, 0) + 1
               WHERE wallet_address IN (
                   SELECT wallet_address FROM challenge_game_players
                   WHERE challenge_id = (SELECT id FROM challenge_games WHERE code = $1)
               )""",
            code,
        )
        if winner:
            await conn.execute(
                "UPDATE challenge_players SET total_wins = COALESCE(total_wins, 0) + 1 WHERE wallet_address=$1",
                winner,
            )
            await conn.execute(
                """UPDATE challenge_players SET total_losses = COALESCE(total_losses, 0) + 1
                   WHERE wallet_address IN (
                       SELECT wallet_address FROM challenge_game_players
                       WHERE challenge_id = (SELECT id FROM challenge_games WHERE code = $1)
                         AND wallet_address != $2
                   )""",
                code, winner,
            )

        # ── Per-chain counters ────────────────────────────────────────────
        game_row = await conn.fetchrow(
            """SELECT chain_id, stake_amount,
                      COALESCE(is_single_player, FALSE) AS is_single_player,
                      bot_difficulty
               FROM challenge_games WHERE code=$1""", code
        )
        if game_row and game_row["chain_id"]:
            chain_id = game_row["chain_id"]
            stake    = float(game_row["stake_amount"] or 0)
            prize    = stake * 2

            # +1 duel for every participant on this chain
            # (counts toward the Rematch Badge — any game mode, any difficulty)
            await conn.execute(
                """INSERT INTO challenge_player_balances (wallet_address, chain_id, total_duels)
                   SELECT wallet_address, $2, 1
                   FROM challenge_game_players
                   WHERE challenge_id = (SELECT id FROM challenge_games WHERE code = $1)
                   ON CONFLICT (wallet_address, chain_id)
                   DO UPDATE SET total_duels = challenge_player_balances.total_duels + 1""",
                code, chain_id,
            )

            # ── Redeem Badge progress ─────────────────────────────────────
            # A game "qualifies" if it was a 1v1 duel, OR a solo game against
            # a high-difficulty bot (Downpour / Torrent / Flood). Only human
            # wallets accrue qualifying_duels.
            from bot_config import BOT_WALLETS as _BOTS, HIGH_DIFFICULTY_MIN as _HIGH_MIN
            _is_sp   = bool(game_row["is_single_player"])
            _bot_dif = int(game_row["bot_difficulty"] or 0)
            if (not _is_sp) or (_bot_dif >= _HIGH_MIN):
                await conn.execute(
                    """INSERT INTO challenge_player_balances (wallet_address, chain_id, qualifying_duels)
                       SELECT wallet_address, $2, 1
                       FROM challenge_game_players
                       WHERE challenge_id = (SELECT id FROM challenge_games WHERE code = $1)
                         AND NOT (wallet_address = ANY($3::text[]))
                       ON CONFLICT (wallet_address, chain_id)
                       DO UPDATE SET qualifying_duels =
                           COALESCE(challenge_player_balances.qualifying_duels, 0) + 1""",
                    code, chain_id, list(_BOTS),
                )

            if winner:
                await conn.execute(
                    """INSERT INTO challenge_player_balances (wallet_address, chain_id, total_wins, total_earned)
                       VALUES ($1, $2, 1, $3)
                       ON CONFLICT (wallet_address, chain_id)
                       DO UPDATE SET
                           total_wins   = challenge_player_balances.total_wins + 1,
                           total_earned = challenge_player_balances.total_earned + $3""",
                    winner, chain_id, prize,
                )

    # ── Final points snapshot ─────────────────────────────────────────────
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_game_players cgp
               SET final_points = COALESCE((
                   SELECT SUM(cga.points_awarded)
                   FROM challenge_game_answers cga
                   WHERE cga.challenge_id = cgp.challenge_id
                     AND cga.wallet_address = cgp.wallet_address
               ), 0)
               WHERE cgp.challenge_id = (SELECT id FROM challenge_games WHERE code = $1)""",
            code,
        )
async def _save_answer(
    pool:       asyncpg.Pool,
    code:       str,
    wallet:     str,
    r_idx:      int,
    q_idx:      int,
    answer_id:  str,
    is_correct: bool,
    time_taken: float,
    points:     int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_game_answers
               (challenge_id, wallet_address, round_index, question_index,
                answer_id, is_correct, time_taken, points_awarded)
               VALUES (
                 (SELECT id FROM challenge_games WHERE code=$1),
                 $2, $3, $4, $5, $6, $7, $8
               )""",
            code, wallet, r_idx, q_idx, answer_id, is_correct, time_taken, points,
        )