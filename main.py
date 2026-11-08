from __future__ import annotations
import os, json, random, string, asyncio, logging, uuid
from typing import Dict, List, Optional
import datetime
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Query,BackgroundTasks,Header
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from web3 import Web3
import httpx
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import dm_service
import time as _time_module
import asyncpg
from bot_config import get_bot_profile, BOT_WALLETS, BOT_PROFILES, DIFFICULTY_LEVELS
from sp_engine import record_bot_stake
from supabase import create_client, Client
from models import (
    CheckAvailabilityRequest, CreateChallengeRequest, JoinChallengeRequest, CreateSingleChallengeRequest,
    RematachRequest, ClaimRequest, UpdateProfileRequest, UserProfile, SyncProfileRequest, DMSendRequest,
    StakeOfferRequest, Employee, VerifyRequest, ClaimStakeRequest, RedeemRequest, BuyDropsRequest,ClaimRequest
)
from onchain_sync import run_sync as _run_onchain_sync
from onchain_sync import (
    run_sync           as _run_onchain_sync,
    run_sync_chain     as _run_sync_chain,
    scan_chain_events,
    _build_chain_specs,
    _get_last_block,
    start_background_scheduler,
    stop_background_scheduler,
)
 
import asyncio as _asyncio
from fastapi.responses import Response
from fastapi import Request
from datetime import datetime, timedelta
from quiz_engine import (_mark_finished, _set_winner_on_chain)
from abi import QUIZ_HUB_ABI
from eth_account import Account
import re
from chain_config import  build_w3_for_chain
from chain_utils import send_tx
from pydantic import BaseModel
from drops_service import (
    mint_welcome_drops,
    check_pre_badge_rules,
    redeem_reward_drops,
    buy_drops_with_g,
    get_player_stakes_db,
    _fetch_g_price_usd,
    claim_stake_db,
    claim_pending_drops,
    weekly_leaderboard_reset_loop,
    get_tier,
    BADGE_UNLOCK_DUELS,
    PRE_BADGE_MAX_STAKE,
    MIN_STAKE_DROPS,
    check_game_drops_balance,   # ← add
    deduct_game_drops,          # ← add
)
import time as _time
 
_stats_cache:      dict[int, dict] = {}   # chain_id → {result, fetched_at}

STATS_CACHE_TTL_SECS = 600 
_sync_lock    = _asyncio.Lock()
_sync_running = False

# Public (non-admin) callers of /api/admin/sync-onchain-users are rate-limited
# per chain so the endpoint can be called by anyone without letting it be used
# to hammer the RPC / DB. Admin callers (valid x-admin-secret) bypass this.
_PUBLIC_SYNC_COOLDOWN_SECS = 300   # 5 min between public-triggered syncs, per chain
_public_sync_last_call: dict[str, float] = {}   # chain name ("celo"/"botchain") → last trigger time

load_dotenv()

_required = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "DATABASE_URL",
    "QUIZ_HUB_CONTRACT",
    "DROPS_TOKEN_CONTRACT",
    "RESOLVER_PRIVATE_KEY",
]

DROPS_DECIMALS  = 18
MIN_STAKE_DROPS = 10.0

_missing = [v for v in _required if not os.getenv(v)]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")
from chain_config import get_chain, is_supported, CELO_CHAIN_ID, CHAINS
from quiz_engine import generate_questions, run_game_loop, strip_answers
from notifications import NotificationService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Quizhub Quiz Platform", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BACKEND_ADDRESS = os.getenv("BACKEND_ADDRESS")
PRIVATE_KEY     = os.getenv("RESOLVER_PRIVATE_KEY")
DATABASE_URL    = os.getenv("DATABASE_URL")
STORAGE_BUCKET  = "employee-photos"

# ─── Global In-Memory State ───────────────────────────────────────────────────
challenges:  Dict[str, dict]            = {}
game_state:  Dict[str, dict]            = {}
connections: Dict[str, List[WebSocket]] = {}
notify_conn: Dict[str, List[WebSocket]] = {}
offers:      Dict[str, dict]            = {}
pool: asyncpg.Pool = None
notif: NotificationService = None

DROPS_REDEEM_EVENT_SIG = Web3.keccak(
    text="PointsRedeemed(address,uint256,string)"
).hex()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def smart_checksum(address: str) -> str:
    if not address:
        return ""
    return Web3.to_checksum_address(address)

def normalize_db_address(address: str) -> str:
    if not address:
        return ""
    return address.lower()

def make_code(k: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=k))

def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr) if addr else addr

async def _get_live_profile(wallet: str) -> dict:
    """Always read display identity from user_profiles — the source of truth —
    rather than trusting frozen in-memory or challenge_players values."""
    wallet = wallet.lower()
    res = supabase.table("user_profiles").select("username, avatar_url").eq("wallet_address", wallet).execute()
    if res.data:
        return {
            "username":   res.data[0].get("username") or f"User{wallet[-4:].upper()}",
            "avatar_url": res.data[0].get("avatar_url") or "",
        }
    return {"username": f"User{wallet[-4:].upper()}", "avatar_url": ""}

def generate_unique_id():
    while True:
        num    = str(random.randint(0, 999)).zfill(3)
        new_id = f"FD{num}"
        response = supabase.table("challenge_employees").select("id").eq("id", new_id).execute()
        if not response.data:
            return new_id
@app.options("/{full_path:path}")
async def preflight(full_path: str, request: Request):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
        },
    )
# ─── Lifecycle ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global pool, notif

    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    pool = await asyncpg.create_pool(
        dsn=db_url,
        min_size=5,
        max_size=15,
        statement_cache_size=0,
        ssl="require",
        timeout=30,
        command_timeout=60,
        server_settings={"application_name": "quizhub_backend"},
    )
    notif = NotificationService(pool, notify_conn, supabase=supabase) 
    asyncio.create_task(_nightly_snapshot_loop())
    asyncio.create_task(weekly_leaderboard_reset_loop(pool))
    asyncio.create_task(_stale_challenge_cleanup_loop()) 
    asyncio.create_task(_presence_drift_loop())
    logger.info("🚀 Quiz Platform Started")
    try:
        await _fetch_g_price_usd()
        logger.info("G price cache warmed on startup")
    except Exception as e:
        logger.warning("Could not warm G price cache on startup: %s", e)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()

# ─── Broadcast ────────────────────────────────────────────────────────────────

async def broadcast(code: str, payload: dict) -> None:
    sockets = connections.get(code, [])
    dead    = []
    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.remove(ws)

# ─── On-chain helpers ─────────────────────────────────────────────────────────

async def _verify_redeem_tx(
    tx_hash: str,
    expected_wallet: str,
    expected_code: str,
    chain_id: int,          # <-- ADD explicitly
    scan_mode: bool = False,
) -> tuple[bool, float]:
    
    # Resolve chain config dynamically
    cfg = get_chain(chain_id)
    rpc_url = cfg.rpc_url
    drops_contract = cfg.drops_token

    async with httpx.AsyncClient() as client:
        if scan_mode:
            latest_resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber", "params": []
            })
            latest_block = int(latest_resp.json()["result"], 16)
            from_block   = hex(max(0, latest_block - 5000))

            logs_resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{
                    "fromBlock": from_block,
                    "toBlock":   "latest",
                    "address":   drops_contract, # <-- USE DYNAMIC ADDRESS
                    "topics":    [
                        DROPS_REDEEM_EVENT_SIG,
                        "0x" + "0" * 24 + expected_wallet[2:],
                    ],
                }],
            })
            logs = logs_resp.json().get("result", [])
        else:
            r = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash],
            })
            receipt = r.json().get("result")
            if not receipt or receipt.get("status") != "0x1":
                return False, 0.0
            if receipt.get("to", "").lower() != drops_contract.lower():
                return False, 0.0
            logs = receipt.get("logs", [])

    for log in logs:
        if log["topics"][0].lower() != DROPS_REDEEM_EVENT_SIG.lower():
            continue
        log_user = "0x" + log["topics"][1][-40:]
        if log_user.lower() != expected_wallet.lower():
            continue
        data_hex = log["data"][2:]
        try:
            amount_wei = int(data_hex[:64], 16)
            str_len    = int(data_hex[128:192], 16)
            str_hex    = data_hex[192: 192 + str_len * 2]
            reward_id  = bytes.fromhex(str_hex).decode("utf-8")
        except Exception:
            continue
        if reward_id.upper() != expected_code.upper():
            continue
        return True, amount_wei / (10 ** DROPS_DECIMALS)

    return False, 0.0

async def _call_register_quiz_on_chain(
    code: str, player1: str, player2: str, agreed_stake: float, chain_id: int | None = None
) -> None:
    from quiz_engine import _get_quiz_id, QUIZ_HUB_ABI

    cfg = get_chain(chain_id)
    if not cfg.quiz_hub:
        logger.error("registerQuiz skipped — no QuizHub on %s", cfg.name)
        return

    def _build(nonce: int) -> dict:
        w3, account = build_w3_for_chain(cfg.id)
        hub = w3.eth.contract(address=Web3.to_checksum_address(cfg.quiz_hub), abi=QUIZ_HUB_ABI)
        quiz_id   = _get_quiz_id(code)
        stake_wei = int(agreed_stake * (10 ** DROPS_DECIMALS))
        return hub.functions.registerQuiz(
            quiz_id, Web3.to_checksum_address(player1), Web3.to_checksum_address(player2), stake_wei
        ).build_transaction({"from": account.address, "nonce": nonce, "gas": 150_000})

    w3, account = build_w3_for_chain(cfg.id)
    try:
        receipt = await send_tx(_build, account, w3, label=f"registerQuiz:{code}", chain_id=cfg.id)
        logger.info("registerQuiz OK  code=%s  chain=%s  tx=%s", code, cfg.name, receipt["transactionHash"].hex())
    except Exception as e:
        logger.error("registerQuiz FAILED  code=%s  chain=%s  error=%s", code, cfg.name, e)


async def _call_confirm_burn_on_chain(code: str, player_address: str, chain_id: int | None = None) -> None:
    from quiz_engine import _get_quiz_id, QUIZ_HUB_ABI

    cfg = get_chain(chain_id)
    if not cfg.quiz_hub:
        logger.error("confirmBurn skipped — no QuizHub on %s", cfg.name)
        return

    w3, account = build_w3_for_chain(cfg.id)
    hub = w3.eth.contract(address=Web3.to_checksum_address(cfg.quiz_hub), abi=QUIZ_HUB_ABI)
    quiz_id   = _get_quiz_id(code)
    player_cs = Web3.to_checksum_address(player_address)

    def _build(nonce: int) -> dict:
        return hub.functions.confirmBurn(quiz_id, player_cs).build_transaction({
            "from": account.address, "nonce": nonce, "gas": 100_000,
        })

    try:
        receipt = await send_tx(_build, account, w3, label=f"confirmBurn:{code}", chain_id=cfg.id)
        logger.info("confirmBurn OK  code=%s  player=%s  chain=%s  tx=%s",
                     code, player_address, cfg.name, receipt["transactionHash"].hex())
    except Exception as e:
        logger.error("confirmBurn FAILED  code=%s  player=%s  chain=%s  error=%s", code, player_address, cfg.name, e)


# ─── Player Endpoints ─────────────────────────────────────────────────────────

BURN_WINDOW_SECONDS = 7200   # mirrors QuizHub's `BURN_WINDOW = 2 hours`

STALE_CHALLENGE_SECONDS = 2 * 3600  # 5 hours

async def _stale_challenge_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(600)  # check every 10 min
        try:
            await _cleanup_stale_challenges()
        except Exception as e:
            logger.error("Stale challenge cleanup failed: %s", e)

async def _refund_stake(wallet: str, amount: float, code: str, chain_id: int | None = None) -> None:
    from drops_service import _mint_to, _ensure_chain_balance_row
    wallet     = wallet.lower()
    cfg        = get_chain(chain_id)
    amount_wei = int(amount * (10 ** DROPS_DECIMALS))
    tx = await _mint_to(wallet, amount_wei, label=f"expired_refund:{code}", chain_id=cfg.id)
    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, cfg.id)
        await conn.execute(
            """UPDATE challenge_player_balances
               SET game_drops=game_drops+$1
               WHERE wallet_address=$2 AND chain_id=$3""",
            amount, wallet, cfg.id,
        )
    logger.info("stake refunded  wallet=%s  code=%s  amount=%s  chain=%s  tx=%s",
                wallet, code, amount, cfg.name, tx)
    
async def _get_stake_amount(code: str) -> float:
    """Read stake_amount for a challenge code from DB."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stake_amount FROM challenge_games WHERE code=$1", code
        )
    return float(row["stake_amount"]) if row else 0.0
async def _cleanup_stale_challenges() -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=STALE_CHALLENGE_SECONDS)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT g.code, g.id, g.stake_amount
               FROM challenge_games g
               WHERE g.status IN ('pending','waiting')
                 AND g.created_at < $1""",
            cutoff,
        )
        if not rows:
            return

        codes = [r["code"] for r in rows]

        # Find anyone who already staked into one of these stale challenges
        # so we can refund them before wiping the challenge.
        staked_rows = await conn.fetch(
            """SELECT cgp.wallet_address, g.code, g.stake_amount, g.chain_id
               FROM challenge_game_players cgp
               JOIN challenge_games g ON g.id = cgp.challenge_id
               WHERE g.code = ANY($1::text[]) AND cgp.tx_verified = TRUE""",
            codes,
        )

        await conn.execute(
            """UPDATE challenge_games SET status='cancelled', cancel_reason='stale_no_activity'
               WHERE code = ANY($1::text[])""",
            codes,
        )

    # Refund anyone who staked before the challenge went stale
    refunded_by_code: dict[str, list[str]] = {}
    for row in staked_rows:
        wallet = row["wallet_address"].lower()
        code = row["code"]
        amount = float(row["stake_amount"])
        try:
            await _refund_stake(wallet, amount, code, chain_id=row["chain_id"])
            refunded_by_code.setdefault(code, []).append(wallet)
            try:
                await notif.send(wallet, "challenge_expired", "⏳ Challenge expired",
                                  f"Your {amount} DROPS stake was refunded.", {"code": code})
            except Exception:
                pass
        except Exception as e:
            logger.error("stale-cleanup refund FAILED  wallet=%s  code=%s  error=%s", wallet, code, e)

    for code in codes:
        challenges.pop(code, None)
        game_state.pop(code, None)
        await broadcast(code, {
            "type": "challenge_expired",
            "code": code,
            "refunded": refunded_by_code.get(code, []),
            "message": "This challenge was removed due to inactivity.",
        })

    logger.info("Stale challenge cleanup removed %d challenges: %s", len(codes), codes)
    
async def _get_onchain_quiz(code: str, chain_id: int | None = None) -> dict:
    cfg = get_chain(chain_id)

    def _call():
        # Use build_w3_for_chain so POA middleware (Botchain) is always injected.
        w3, _ = build_w3_for_chain(cfg.id)
        contract = w3.eth.contract(address=Web3.to_checksum_address(cfg.quiz_hub), abi=QUIZ_HUB_ABI)
        return contract.functions.getQuiz(Web3.keccak(text=code)).call()

    loop = asyncio.get_event_loop()
    q = await loop.run_in_executor(None, _call)
    return {
        "player1": q[2], "player2": q[3], "stakePerPlayer": q[4],
        "status": q[5], "p1BurnConfirmed": q[7], "p2BurnConfirmed": q[8],
        "registeredAt": q[10],
    }
    
    
async def _call_cancel_quiz_on_chain(quiz_id: bytes, chain_id: int | None = None) -> str:
    from quiz_engine import _QUIZ_HUB_ABI

    cfg = get_chain(chain_id)
    _CANCEL_ITEM = {
        "inputs": [{"internalType": "bytes32", "name": "quizId", "type": "bytes32"}],
        "name": "cancelQuiz", "outputs": [],
        "stateMutability": "nonpayable", "type": "function",
    }

    w3, account = build_w3_for_chain(cfg.id)
    hub = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.quiz_hub),
        abi=_QUIZ_HUB_ABI + [_CANCEL_ITEM],
    )

    def _build(nonce: int) -> dict:
        return hub.functions.cancelQuiz(quiz_id).build_transaction({
            "from": account.address, "nonce": nonce, "gas": 100_000,
        })

    receipt = await send_tx(_build, account, w3, label="cancelQuiz", chain_id=cfg.id)
    return receipt["transactionHash"].hex()

# ─── Authoritative game stats (same table /api/ranks reads) ───────────────────

async def _get_player_stats(wallet: str, chain_id: int | None = None) -> dict:
    from drops_service import (
        get_tier, BADGE_UNLOCK_DUELS, PRE_BADGE_MAX_STAKE,
        REDEEM_BADGE_QUALIFYING_GAMES,
    )

    wallet = wallet.lower()

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT chain_id,
                          COALESCE(game_drops,       0)     AS game_drops,
                          COALESCE(reward_drops,     0)     AS reward_drops,
                          COALESCE(total_wins,       0)     AS total_wins,
                          COALESCE(total_duels,      0)     AS total_duels,
                          COALESCE(total_ties,       0)     AS total_ties,
                          COALESCE(total_earned,     0)     AS total_earned,
                          COALESCE(redeem_badge,     FALSE) AS redeem_badge,
                          COALESCE(qualifying_duels, 0)     AS qualifying_duels
                   FROM challenge_player_balances
                   WHERE LOWER(wallet_address) = $1""",
                wallet,
            )
            gp = await conn.fetchrow(
                """SELECT COALESCE(rematch_badge, FALSE) AS rematch_badge
                   FROM challenge_players
                   WHERE LOWER(wallet_address) = $1""",
                wallet,
            )
    except Exception as e:
        # Fail loudly — a silent zero here looks identical to "player has no games"
        logger.error("_get_player_stats failed  wallet=%s  chain=%s  error=%s",
                     wallet, chain_id, e)
        raise HTTPException(status_code=500, detail=f"stats query failed: {e}")

    # Collapse duplicate rows for the same chain (mixed-case wallet writes)
    best: dict[int, dict] = {}
    for r in rows:
        cur = best.get(r["chain_id"])
        if cur is None or int(r["total_duels"]) > int(cur["total_duels"]):
            best[r["chain_id"]] = r
    rows = list(best.values())

    selected = [r for r in rows if chain_id is None or r["chain_id"] == chain_id]

    total_wins       = sum(int(r["total_wins"])       for r in selected)
    total_duels      = sum(int(r["total_duels"])      for r in selected)
    qualifying_duels = sum(int(r["qualifying_duels"]) for r in selected)

    tier_name, apy_pct = get_tier(total_duels)

    # Badges: honour the stored flag, but also unlock off authoritative counts
    rematch_badge = bool(gp and gp["rematch_badge"]) or total_duels >= BADGE_UNLOCK_DUELS
    redeem_badge  = any(bool(r["redeem_badge"]) for r in selected) \
                    or qualifying_duels >= REDEEM_BADGE_QUALIFYING_GAMES

    return {
        "chain_id":         chain_id,
        "game_drops":       sum(float(r["game_drops"])   for r in selected),
        "reward_drops":     sum(float(r["reward_drops"]) for r in selected),
        "total_wins":       total_wins,
        "total_duels":      total_duels,
        "total_ties":       sum(int(r["total_ties"])     for r in selected),
        "total_earned":     sum(float(r["total_earned"]) for r in selected),
        "rank_delta":       0,   # computed by /api/ranks from snapshots, not stored here
        "qualifying_duels": qualifying_duels,
        "tier":             tier_name,
        "apy_pct":          apy_pct,
        "rematch_badge":    rematch_badge,
        "redeem_badge":     redeem_badge,
        "games_until_badge":        max(0, BADGE_UNLOCK_DUELS - total_duels),
        "games_until_redeem_badge": max(0, REDEEM_BADGE_QUALIFYING_GAMES - qualifying_duels),
        "max_stake":        None if rematch_badge else PRE_BADGE_MAX_STAKE,
        "win_rate":         round(total_wins / total_duels * 100, 1) if total_duels else 0.0,
        "chains": [
            {
                "chain_id":     r["chain_id"],
                "total_wins":   int(r["total_wins"]),
                "total_duels":  int(r["total_duels"]),
                "total_earned": float(r["total_earned"]),
            }
            for r in rows
        ],
    }

# ─── Presence filler ──────────────────────────────────────────────────────────
#
# When genuine presence is thin, pad the online list so the app doesn't look
# dead. Padding only applies while real online < PRESENCE_REAL_FLOOR, and the
# padded total absorbs the real users (so the count doesn't jump when the 6th
# real user connects).

PRESENCE_REAL_FLOOR  = 6      # pad only while real online is below this
PRESENCE_FILLER_MIN  = 20
PRESENCE_FILLER_MAX  = 80
PRESENCE_POOL_TTL    = 900    # re-query candidate names every 15 min
PRESENCE_DRIFT_SECS  = 90     # nudge the count every 90s so it isn't frozen
PRESENCE_USE_REAL_NAMES = True   # False → synthetic UserXXXX names instead

_filler_shuffled: list[dict] = []   # stable shuffled candidate order
_filler_pool_at:  float      = 0.0
_filler_count:    int        = 0
_filler_at:       float      = 0.0

_BOT_WALLETS_LOWER = {
    (p.get("wallet") or "").lower()
    for p in BOT_PROFILES.values()
    if p.get("wallet")
}


async def _load_filler_pool() -> list[dict]:
    """Top-100 ranked wallets + their display names, cached and shuffled once."""
    global _filler_shuffled, _filler_pool_at

    now = _time.time()
    if _filler_shuffled and (now - _filler_pool_at) < PRESENCE_POOL_TTL:
        return _filler_shuffled

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT wallet_address,
                          MAX(COALESCE(total_wins, 0))  AS wins,
                          MAX(COALESCE(total_duels, 0)) AS duels
                   FROM challenge_player_balances
                   WHERE COALESCE(total_duels, 0) > 0
                   GROUP BY wallet_address
                   ORDER BY wins DESC, duels DESC
                   LIMIT 100"""
            )
    except Exception as e:
        logger.warning("filler pool query failed: %s", e)
        return _filler_shuffled

    wallets = [
        r["wallet_address"].lower()
        for r in rows
        if r["wallet_address"] and r["wallet_address"].lower() not in _BOT_WALLETS_LOWER
    ]
    if not wallets:
        return _filler_shuffled

    profiles_map: dict[str, dict] = {}
    if PRESENCE_USE_REAL_NAMES:
        try:
            res = (
                supabase.table("user_profiles")
                .select("wallet_address, username, avatar_url")
                .in_("wallet_address", wallets)
                .execute()
            )
            profiles_map = {p["wallet_address"].lower(): p for p in (res.data or [])}
        except Exception as e:
            logger.warning("filler profile fetch failed: %s", e)

    candidates = []
    for w in wallets:
        p = profiles_map.get(w, {})
        candidates.append({
            "wallet":     w,
            "username":   (p.get("username") if PRESENCE_USE_REAL_NAMES else None)
                          or f"User{w[-4:].upper()}",
            "avatar_url": (p.get("avatar_url") or "") if PRESENCE_USE_REAL_NAMES else "",
            "filler":     True,
        })

    random.shuffle(candidates)
    _filler_shuffled = candidates
    _filler_pool_at  = now
    logger.info("presence filler pool loaded — %d candidates", len(candidates))
    return _filler_shuffled


def _drift_filler_count() -> int:
    """Pick a target once, then wander it slowly so the number looks alive."""
    global _filler_count, _filler_at

    now = _time.time()
    if _filler_count == 0:
        _filler_count = random.randint(PRESENCE_FILLER_MIN, PRESENCE_FILLER_MAX)
        _filler_at    = now
    elif now - _filler_at > PRESENCE_DRIFT_SECS:
        _filler_count = max(
            PRESENCE_FILLER_MIN,
            min(PRESENCE_FILLER_MAX, _filler_count + random.randint(-4, 4)),
        )
        _filler_at = now
    return _filler_count


def _current_filler(real_wallets: set[str]) -> list[dict]:
    """The filler slice currently 'online' — stable membership, drifting size."""
    if len(real_wallets) >= PRESENCE_REAL_FLOOR or not _filler_shuffled:
        return []
    target = _drift_filler_count()
    need   = max(0, target - len(real_wallets))
    pool_  = [f for f in _filler_shuffled if f["wallet"] not in real_wallets]
    return pool_[:need]


async def _online_list() -> list[dict]:
    real_wallets = set(presence_wallets)
    real = [
        {"wallet": w, "username": info["username"], "avatar_url": info["avatar_url"]}
        for w, info in presence_wallets.items()
    ]
    if len(real) >= PRESENCE_REAL_FLOOR:
        return real

    await _load_filler_pool()
    return real + _current_filler(real_wallets)


def _is_online(wallet: str) -> bool:
    """Presence check that also honours the filler set."""
    w = wallet.lower()
    if w in presence_wallets:
        return True
    return any(f["wallet"] == w for f in _current_filler(set(presence_wallets)))


async def _presence_drift_loop() -> None:
    """Presence only broadcasts on connect/disconnect — with few real users the
    number would sit frozen. Re-broadcast while padding is active."""
    while True:
        await asyncio.sleep(PRESENCE_DRIFT_SECS)
        try:
            if presence_sockets and len(presence_wallets) < PRESENCE_REAL_FLOOR:
                await _broadcast_presence()
        except Exception as e:
            logger.warning("presence drift loop error: %s", e)
   
@app.post("/api/challenge/{code}/cancel-expired")
async def cancel_expired_challenge(code: str, body: dict):
    code = code.upper()
    # Accept either field name for backwards-compat with old frontend
    requester_wallet = (
        body.get("walletAddress") or body.get("creatorWallet") or ""
    ).lower()

    if not requester_wallet:
        raise HTTPException(status_code=400, detail="walletAddress required")

    # Fetch chain_id from DB first so every on-chain call hits the right network
    async with pool.acquire() as conn:
        db_chain_row = await conn.fetchrow(
            "SELECT chain_id FROM challenge_games WHERE code=$1", code
        )
    if not db_chain_row:
        raise HTTPException(status_code=404, detail="Challenge not found")
    cancel_chain_id = db_chain_row["chain_id"] or CELO_CHAIN_ID

    quiz = await _get_onchain_quiz(code, chain_id=cancel_chain_id)

    # ── Not yet registered on-chain (stale window) ────────────────────────
    if quiz["registeredAt"] == 0:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, creator_address, created_at, stake_amount, chain_id FROM challenge_games WHERE code=$1", code
            )
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")

        created_ts = int(row["created_at"].timestamp())
        expires_at = created_ts + STALE_CHALLENGE_SECONDS
        now = int(_time_module.time())
        if now <= expires_at:
            raise HTTPException(
                status_code=400,
                detail=f"Stale window still open — {expires_at - now}s left."
            )

        # Any participant can cancel (not just creator)
        async with pool.acquire() as conn:
            prow = await conn.fetchrow(
                """SELECT wallet_address FROM challenge_game_players
                   WHERE challenge_id=$1 AND wallet_address=$2""",
                row["id"], requester_wallet,
            )
        is_creator = row["creator_address"].lower() == requester_wallet
        if not prow and not is_creator:
            raise HTTPException(status_code=403, detail="You are not a participant in this challenge")

        async with pool.acquire() as conn:
            staked_rows = await conn.fetch(
                "SELECT wallet_address FROM challenge_game_players WHERE challenge_id=$1 AND tx_verified=TRUE",
                row["id"],
            )
            await conn.execute(
                "UPDATE challenge_games SET status='cancelled', cancel_reason='expired_unstaked' WHERE code=$1",
                code,
            )

        row_chain_id = row["chain_id"] or CELO_CHAIN_ID
        stake_amount = float(row["stake_amount"])
        refunded = []
        for sr in staked_rows:
            wallet = sr["wallet_address"].lower()
            try:
                await _refund_stake(wallet, stake_amount, code, chain_id=row_chain_id)
                refunded.append(wallet)
            except Exception as e:
                logger.error("refund FAILED  wallet=%s  code=%s  error=%s", wallet, code, e)

        challenges.pop(code, None)
        game_state.pop(code, None)
        await broadcast(code, {
            "type": "challenge_expired", "code": code, "refunded": refunded,
            "message": "This challenge expired before being registered on-chain. Staked DROPS have been refunded.",
        })
        return {"success": True, "refunded": refunded}

    # ── Registered on-chain — burn window expired ─────────────────────────
    if quiz["status"] != 1:
        raise HTTPException(status_code=400, detail="Quiz is not in a cancellable state.")

    expires_at = quiz["registeredAt"] + BURN_WINDOW_SECONDS
    now = int(_time_module.time())
    if now <= expires_at:
        raise HTTPException(
            status_code=400, detail=f"Burn window still open — {expires_at - now}s left."
        )

    # Any participant (player1, player2, or DB creator) can cancel
    zero = "0x0000000000000000000000000000000000000000"
    p1 = quiz["player1"].lower()
    p2 = quiz["player2"].lower()
    valid_players = {p1} | ({p2} if p2 != zero else set())

    if requester_wallet not in valid_players:
        async with pool.acquire() as conn:
            db_row = await conn.fetchrow(
                "SELECT creator_address FROM challenge_games WHERE code=$1", code
            )
        db_creator = db_row["creator_address"].lower() if db_row else None
        if requester_wallet != db_creator:
            raise HTTPException(status_code=403, detail="You are not a participant in this challenge")

    try:
        await _call_cancel_quiz_on_chain(Web3.keccak(text=code), chain_id=cancel_chain_id)
    except Exception as e:
        logger.error("cancelQuiz on-chain FAILED  code=%s  error=%s", code, e)
        raise HTTPException(status_code=502, detail=f"On-chain cancel failed: {e}")

    challenge    = challenges.get(code)
    stake_amount = (
        float(challenge.get("agreedStake") or challenge.get("stakeAmount", 0))
        if challenge else float(quiz["stakePerPlayer"]) / (10 ** DROPS_DECIMALS)
    )

    staked_players = [
        addr.lower() for addr, flag in
        [(quiz["player1"], quiz["p1BurnConfirmed"]), (quiz["player2"], quiz["p2BurnConfirmed"])]
        if flag and addr.lower() != zero
    ]

    refunded = []
    for wallet in staked_players:
        try:
            await _refund_stake(wallet, stake_amount, code, chain_id=cancel_chain_id)
            refunded.append(wallet)
        except Exception as e:
            logger.error("refund FAILED  wallet=%s  code=%s  error=%s", wallet, code, e)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_games SET status='cancelled', cancel_reason='expired_unstaked' WHERE code=$1",
            code,
        )
    if challenge:
        challenges.pop(code, None)
        game_state.pop(code, None)

    await broadcast(code, {
        "type": "challenge_expired", "code": code, "refunded": refunded,
        "message": "This challenge expired before both players staked. Staked DROPS have been refunded.",
    })
    for wallet in refunded:
        try:
            await notif.send(wallet, "challenge_expired", "⏳ Challenge expired",
                              f"Your {stake_amount} DROPS stake was refunded.", {"code": code})
        except Exception:
            pass

    return {"success": True, "refunded": refunded, "stakeAmount": stake_amount}

@app.get("/api/challenge/{code}/expiry")
async def get_challenge_expiry(code: str):
    code = code.upper()
    now = int(_time_module.time())

    # ── Always read DB first — never 502 on RPC failure ──────────────────
    async with pool.acquire() as conn:
        # Fetch chain_id as well
        row = await conn.fetchrow(
            """SELECT id, created_at, creator_address, status, stake_amount, chain_id
               FROM challenge_games WHERE code=$1""",
            code,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Challenge not found")

    created_ts = int(row["created_at"].timestamp())

    # ── Try on-chain read, but NEVER let it 502 the whole endpoint ────────
    expiry_chain_id = row["chain_id"] or CELO_CHAIN_ID
    quiz = None
    try:
        quiz = await asyncio.wait_for(_get_onchain_quiz(code, chain_id=expiry_chain_id), timeout=8.0)
    except asyncio.TimeoutError:
        logger.warning("expiry: on-chain read timed out  code=%s", code)
    except Exception as e:
        logger.warning("expiry: on-chain read failed  code=%s  error=%s", code, e)

    # ── Not registered on-chain (or RPC failed) — use DB stale window ────
    if quiz is None or quiz["registeredAt"] == 0:
        expires_at = created_ts + STALE_CHALLENGE_SECONDS
        return {
            "success":       True,
            "onChain":       False,
            "createdAt":     created_ts,
            "expiresAt":     expires_at,
            "secondsLeft":   max(0, expires_at - now),
            "expired":       now > expires_at,
            "creator":       row["creator_address"].lower(),
            "windowSeconds": STALE_CHALLENGE_SECONDS,
            # Stub stake dots from DB — no on-chain data yet
            "p1Staked":      False,
            "p2Staked":      False,
            "player1":       row["creator_address"].lower(),
            "player2":       None,
        }

    # ── Registered on-chain ───────────────────────────────────────────────
    expires_at = quiz["registeredAt"] + BURN_WINDOW_SECONDS
    return {
        "success":       True,
        "onChain":       True,
        "status":        quiz["status"],
        "registeredAt":  quiz["registeredAt"],
        "expiresAt":     expires_at,
        "secondsLeft":   max(0, expires_at - now),
        "expired":       now > expires_at and quiz["status"] == 1,
        "p1Staked":      quiz["p1BurnConfirmed"],
        "p2Staked":      quiz["p2BurnConfirmed"],
        "player1":       quiz["player1"].lower(),
        "player2":       quiz["player2"].lower(),
        "windowSeconds": BURN_WINDOW_SECONDS,
    }
# ─── Welcome DROPS: issue claim signature ─────────────────────────────────────
@app.post("/api/drops/buy")
async def buy_drops(body: BuyDropsRequest):
    wallet = body.walletAddress.lower()
    if body.expectedGAmount <= 0:
        raise HTTPException(status_code=400, detail="expectedGAmount must be > 0")
    if not body.gTxHash:
        raise HTTPException(status_code=400, detail="gTxHash required")
    try:
        result = await buy_drops_with_g(
            pool, wallet,
            g_tx_hash=body.gTxHash,
            expected_g_amount=body.expectedGAmount,
            drops_amount=body.dropsAmount,
            chain_id=body.chainId,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result

@app.get("/api/drops/welcome-status")
async def welcome_status(wallet: str = Query(...), chainId: int | None = Query(default=None)):
    """
    Lets the frontend check whether the player has already called welcome()
    on-chain, to decide whether to show the claim button.
    Reads directly from the contract — no DB involved.
    """
    from abi import DROPS_TOKEN_ABI as _DROPS_TOKEN_ABI

    chain_id  = chainId or CELO_CHAIN_ID
    cfg       = get_chain(chain_id)
    wallet_cs = Web3.to_checksum_address(wallet.lower())

    def _check() -> bool:
        w3, _ = build_w3_for_chain(cfg.id)
        token  = w3.eth.contract(
            address=Web3.to_checksum_address(cfg.drops_token),
            abi=_DROPS_TOKEN_ABI,
        )
        return token.functions.hasClaimedWelcome(wallet_cs).call()

    loop    = asyncio.get_event_loop()
    claimed = await loop.run_in_executor(None, _check)
    return {"success": True, "claimed": claimed, "chainId": cfg.id}


# ─── Buy DROPS: confirm on-chain claim after user submits tx ──────────────────

@app.post("/api/drops/buy-claimed")
async def confirm_buy_claimed(body: dict):
    """
    Frontend calls this after the claim() tx for a buy purchase confirms.
    Updates challenge_buy_drops_history status from 'pending_claim' → 'confirmed'.
    """
    wallet    = body.get("walletAddress", "").lower()
    g_tx_hash = body.get("gTxHash", "")      # original $G payment tx (used as key)
    mint_hash = body.get("mintTxHash", "")   # the DROP claim() tx hash

    if not wallet or not g_tx_hash:
        raise HTTPException(status_code=400, detail="walletAddress and gTxHash required")

    async with pool.acquire() as conn:
        updated = await conn.execute(
            """UPDATE challenge_buy_drops_history
               SET status='confirmed', mint_tx_hash=$1
               WHERE tx_hash=$2 AND wallet_address=$3""",
            mint_hash, g_tx_hash, wallet,
        )

    if updated == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Buy record not found")

    logger.info("Buy DROPS confirmed  wallet=%s  gTx=%s  mintTx=%s", wallet, g_tx_hash, mint_hash)
    return {"success": True}


# ─── Game-win DROPS signature (for reward_drops credited after a win) ─────────
# Only needed if you also want winners to claim DROP tokens on-chain for game wins.
# reward_drops are currently DB-only; skip this if that's intentional.

@app.post("/api/drops/game-reward-signature")
async def get_game_reward_signature(body: dict):
    """
    After a game win, if you want to let the winner mint their reward_drops
    as actual on-chain DROP tokens, call this endpoint.
    The frontend then calls DropsToken.claim() with the returned payload.
    """
    from drops_service import _sign_claim

    wallet       = body.get("walletAddress", "").lower()
    drops_amount = float(body.get("dropsAmount", 0))
    chain_id     = body.get("chainId") or CELO_CHAIN_ID
    cfg          = get_chain(chain_id)

    if not wallet:
        raise HTTPException(status_code=400, detail="walletAddress required")
    if drops_amount <= 0:
        raise HTTPException(status_code=400, detail="dropsAmount must be > 0")

    wallet_cs = Web3.to_checksum_address(wallet)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT reward_drops FROM challenge_player_balances
               WHERE wallet_address=$1 AND chain_id=$2""",
            wallet, cfg.id,
        )

    if not row:
        raise HTTPException(status_code=404, detail="Player balance not found for this chain")
    if float(row["reward_drops"]) < drops_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient reward drops on {cfg.name}. Available: {row['reward_drops']}"
        )

    amount_wei = int(drops_amount * (10 ** DROPS_DECIMALS))
    ts         = int((_time_module.time() // 600) * 600)
    sig        = _sign_claim(wallet_cs, amount_wei, ts, chain_id=cfg.id)

    # Debit reward_drops now — re-credit if user never submits the tx
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_player_balances
               SET reward_drops=reward_drops-$1
               WHERE wallet_address=$2 AND chain_id=$3""",
            drops_amount, wallet, cfg.id,
        )

    return {
        "success":   True,
        "contract":  cfg.drops_token,
        "amount":    str(amount_wei),
        "timestamp": ts,
        "signature": "0x" + sig.hex(),
        "chainId":   cfg.id,
    }
    
@app.get("/api/players/by-username/{username}")
async def get_player_by_username(username: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT wallet_address, username FROM user_profiles WHERE LOWER(username) = LOWER($1)",
            username.strip(),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"success": True, "wallet": row["wallet_address"], "username": row["username"]}


@app.get("/api/players/{wallet}")
async def get_player(wallet: str):
    wallet = wallet.lower()
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM challenge_players WHERE wallet_address=$1", wallet
        )
        if not row:
            # Auto-register with a placeholder username
            generated_username = f"User{wallet[-4:].upper()}"
            await conn.execute(
                """INSERT INTO challenge_players (wallet_address, username)
                   VALUES ($1, $2) ON CONFLICT (wallet_address) DO NOTHING""",
                wallet, generated_username,
            )
            row = await conn.fetchrow(
                "SELECT * FROM challenge_players WHERE wallet_address=$1", wallet
            )
    
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    
    result = dict(row)
    
    # Overlay username/avatar from shared user_profiles
    profile_res = supabase.table("user_profiles").select("username, avatar_url, bio").eq("wallet_address", wallet).execute()
    if profile_res.data:
        result["username"]   = profile_res.data[0].get("username") or result["username"]
        result["avatar_url"] = profile_res.data[0].get("avatar_url", "")
        result["bio"]        = profile_res.data[0].get("bio", "")
    
    return result
# ─── Challenge Creation ───────────────────────────────────────────────────────

@app.post("/api/challenge/create")
async def create_challenge(body: CreateChallengeRequest):
    if not body.chainId:
        raise HTTPException(status_code=400, detail="chainId is required")

    if body.stakeAmount < MIN_STAKE_DROPS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum stake is {MIN_STAKE_DROPS} DROPS."
        )

    code     = make_code()
    topic    = body.topic
    creator  = body.creatorAddress.lower()
    stake    = body.stakeAmount
    chain_id = body.chainId

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rematch_badge, total_duels FROM challenge_players WHERE wallet_address=$1", creator
        )

    has_badge   = False
    total_duels = 0
    if row:
        total_duels = int(row["total_duels"] or 0)
        has_badge   = row["rematch_badge"]
        if not has_badge:
            if total_duels < BADGE_UNLOCK_DUELS:
                if body.stakeAmount != MIN_STAKE_DROPS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Play {BADGE_UNLOCK_DUELS - total_duels} more game(s) to unlock custom stakes. "
                               f"Your stake must be exactly {MIN_STAKE_DROPS} DROPS for now."
                    )
            elif body.stakeAmount > PRE_BADGE_MAX_STAKE:
                raise HTTPException(
                    status_code=400,
                    detail=f"Earn the Rematch Badge (play {BADGE_UNLOCK_DUELS} games) "
                           f"to stake more than {PRE_BADGE_MAX_STAKE} DROPS."
                )

    # ── Check creator has enough game_drops to cover the stake ───────────────
    try:
       await check_game_drops_balance(pool, creator, stake, chain_id=chain_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    questions = await generate_questions(topic, body.questionCount)

    async with pool.acquire() as conn:
        challenge_id = await conn.fetchval(
            """INSERT INTO challenge_games
            (code, creator_address, topic, stake_amount, token_symbol, chain_id,
                status, is_public, rounds_data)
            VALUES ($1,$2,$3,$4,'DROPS',$5,'pending',$6,$7)
            RETURNING id""",
            code, creator, topic, stake, chain_id,
            body.isPublic, json.dumps(questions),
        )
        await conn.execute(
            """INSERT INTO challenge_players (wallet_address, username)
               VALUES ($1,$2)
               ON CONFLICT (wallet_address) DO UPDATE SET username=EXCLUDED.username""",
            creator, body.creatorUsername,
        )
        await conn.execute(
            """INSERT INTO challenge_game_players (challenge_id, wallet_address, username)
               VALUES ($1,$2,$3)""",
            challenge_id, creator, body.creatorUsername,
        )

    creator_profile = await _get_live_profile(creator)

    challenges[code] = {
        "id":           str(challenge_id),
        "code":         code,
        "topic":        topic,
        "creator":      creator,
        "creatorName":  creator_profile["username"],
        "creatorAvatar": creator_profile["avatar_url"],   # ← add
        "stakeAmount":  stake,
        "agreedStake":  None,
        "token":        "DROPS",
        "chainId":      chain_id,
        "status":       "pending",
        "isPublic":     body.isPublic,
        "inviteWallet": body.inviteWallet,
        "rounds":       questions["rounds"],
        "players": {
            creator: {
                "username":   creator_profile["username"],
                "avatar_url": creator_profile["avatar_url"],  # ← add
                "points":     0,
                "ready":      False,
                "txVerified": False,
            }
        },
        "player_offers":    {},
        "onChainConfirmed": False,
    }
    game_state[code] = {"answers": {}, "currentQuestion": None}

    return {
        "success": True,
        "code": code,
        "token": "DROPS",
        "minStake": MIN_STAKE_DROPS,
        "negotiationLocked": not has_badge and total_duels < BADGE_UNLOCK_DUELS,
    }

@app.post("/api/challenge/{code}/on-chain-confirmed")
async def on_chain_confirmed(code: str, body: dict):
    code           = code.upper()
    creator_wallet = body.get("creatorWallet", "").lower()
    tx_hash        = body.get("txHash", "")

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]

    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Not the creator")
    # ── Pre-badge single-encounter check (invite-only challenges) ────────────
    invite_wallet = challenge.get("inviteWallet", "").lower() if challenge.get("inviteWallet") else ""
    if invite_wallet and invite_wallet != creator_wallet:
        block_reason = await check_pre_badge_rules(
            pool, creator_wallet, invite_wallet, challenge.get("stakeAmount", MIN_STAKE_DROPS)
        )
        if block_reason:
            raise HTTPException(status_code=403, detail=block_reason)
        
    # Must be pending — not already confirmed or cancelled
    if challenge["status"] != "pending":
        if challenge["status"] == "waiting":
            return {"success": True, "alreadyConfirmed": True}
        raise HTTPException(status_code=400, detail=f"Cannot confirm — status is '{challenge['status']}'")

    # Flip to joinable
    challenge["status"]           = "waiting"
    challenge["onChainConfirmed"] = True
    challenge["txHash"]           = tx_hash

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_games SET status='waiting', tx_hash=$1 WHERE code=$2",
            tx_hash, code,
        )

    # Send notifications now that it's live — pull fresh identity, don't trust frozen field
    creator_profile = await _get_live_profile(creator_wallet)
    challenge["creatorName"] = creator_profile["username"]  # refresh the in-memory cache too

    if challenge.get("isPublic"):
        # Rich popup for recently-active players (unchanged)
        asyncio.create_task(
            notif.notify_public_challenge(
                code,
                challenge["topic"],
                creator_wallet,
                challenge["stakeAmount"],
                challenge["token"],
                creator_username=creator_profile["username"],
            )
        )
        # Lightweight toast for EVERYONE currently in the app
        asyncio.create_task(
            broadcast_public_quiz(
                code,
                challenge["topic"],
                creator_wallet,
                creator_profile["username"],
                challenge["stakeAmount"],
                challenge["token"],
            )
        )
    elif challenge.get("inviteWallet"):
        asyncio.create_task(
            notif.notify_friend_invite(
                code,
                challenge["topic"],
                creator_profile["username"],
                creator_wallet,
                challenge["inviteWallet"],
                challenge["stakeAmount"],
                challenge["token"],
            )
        )
        # Drop a duel card into their private thread
        asyncio.create_task(
            _dm_send(
                creator_wallet,
                challenge["inviteWallet"],
                f'Duel invite — "{challenge["topic"]}"',
                kind="duel_invite",
                meta={
                    "code":  code,
                    "topic": challenge["topic"],
                    "stake": challenge["stakeAmount"],
                    "token": challenge["token"],
                },
            )
        )

    return {"success": True}



# ─── /api/drops/balance/{wallet} ─────────────────────────────────────────────

@app.get("/api/drops/balance/{wallet}")
async def get_drops_balance(wallet: str, chainId: int | None = Query(default=None)):
    wallet   = wallet.lower()
    chain_id = chainId or CELO_CHAIN_ID
    cfg      = get_chain(chain_id)

    async with pool.acquire() as conn:
        # Ensure player exists in global table
        row = await conn.fetchrow(
            "SELECT total_duels, rematch_badge FROM challenge_players WHERE wallet_address=$1",
            wallet,
        )
        if not row:
            generated_username = f"User{wallet[-4:].upper()}"
            await conn.execute(
                """INSERT INTO challenge_players (wallet_address, username)
                   VALUES ($1, $2) ON CONFLICT (wallet_address) DO NOTHING""",
                wallet, generated_username,
            )
            row = await conn.fetchrow(
                "SELECT total_duels, rematch_badge FROM challenge_players WHERE wallet_address=$1",
                wallet,
            )

        # Fetch per-chain balance
        from drops_service import _ensure_chain_balance_row
        await _ensure_chain_balance_row(conn, wallet, chain_id)
        bal_row = await conn.fetchrow(
            """SELECT game_drops, reward_drops,
                      COALESCE(redeem_badge, false)  AS redeem_badge,
                      COALESCE(qualifying_duels, 0)   AS qualifying_duels
               FROM challenge_player_balances
               WHERE wallet_address=$1 AND chain_id=$2""",
            wallet, chain_id,
        )

    from drops_service import (
        get_tier, BADGE_UNLOCK_DUELS, PRE_BADGE_MAX_STAKE,
        REDEEM_BADGE_QUALIFYING_GAMES,
    )
    total_duels       = int(row["total_duels"] or 0)
    tier_name, apy_pct = get_tier(total_duels)
    games_until_badge = max(0, BADGE_UNLOCK_DUELS - total_duels)
    qualifying_duels  = int(bal_row["qualifying_duels"] or 0)

    return {
        "success":         True,
        "chainId":         chain_id,
        "chain":           cfg.name,
        "gameDrops":       float(bal_row["game_drops"] or 0),
        "rewardDrops":     float(bal_row["reward_drops"] or 0),
        "tier":            tier_name,
        "apyPct":          apy_pct,
        "rematchBadge":    row["rematch_badge"] or False,
        "totalDuels":      total_duels,
        "gamesUntilBadge": games_until_badge,
        "maxStake":        PRE_BADGE_MAX_STAKE if not row["rematch_badge"] else None,
        "redeemBadge":            bal_row["redeem_badge"] or False,
        "qualifyingDuels":        qualifying_duels,
        "gamesUntilRedeemBadge":  max(0, REDEEM_BADGE_QUALIFYING_GAMES - qualifying_duels),
    }


# ─── /api/players/register ────────────────────────────────────────────────────

@app.post("/api/players/register")
async def register_player(wallet: str, username: str, chainId: int | None = None):
    wallet   = wallet.lower()
    chain_id = chainId or CELO_CHAIN_ID

    existing = supabase.table("user_profiles").select("*").eq("wallet_address", wallet).execute()
    final_username = username
    avatar_url = ""
    bio = ""

    if not existing.data:
        username_check = supabase.table("user_profiles").select("username").eq("username", username).execute()
        final_username = username if not username_check.data else f"{username}_{wallet[-4:]}"
        supabase.table("user_profiles").insert({
            "wallet_address": wallet, "username": final_username,
            "avatar_url": avatar_url, "bio": bio,
        }).execute()
    else:
        profile = existing.data[0]
        final_username = profile.get("username") or final_username
        avatar_url = profile.get("avatar_url") or ""
        bio = profile.get("bio") or ""

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_players (wallet_address, username, avatar_url, bio, droplist_last_synced_at)
               VALUES ($1, $2, $3, $4, NOW())
               ON CONFLICT (wallet_address) DO UPDATE
               SET username=EXCLUDED.username, avatar_url=EXCLUDED.avatar_url, bio=EXCLUDED.bio""",
            wallet, final_username, avatar_url, bio,
        )

    # NOTE: no on-chain mint here. Welcome DROPS are minted client-side via
    # DropsToken.welcome(). This endpoint just returns current pouch state;
    # the actual credit happens once via /api/players/confirm-welcome.
    from drops_service import _ensure_chain_balance_row
    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, chain_id)
        bal = await conn.fetchrow(
            "SELECT game_drops, reward_drops, drops_minted FROM challenge_player_balances WHERE wallet_address=$1 AND chain_id=$2",
            wallet, chain_id,
        )
        cp = await conn.fetchrow(
            "SELECT tier, rematch_badge FROM challenge_players WHERE wallet_address=$1",
            wallet,
        )

    return {
        "success":       True,
        "username":      final_username,
        "avatar_url":    avatar_url,
        "bio":           bio,
        "chainId":       chain_id,
        "gameDrops":     float(bal["game_drops"]) if bal else 0.0,
        "rewardDrops":   float(bal["reward_drops"]) if bal else 0.0,
        "alreadyMinted": bal["drops_minted"] if bal else False,
        "tier":          cp["tier"] if cp else "Droplet",
        "rematchBadge":  cp["rematch_badge"] if cp else False,
    }
    
    

# ─── /api/drops/stakes/{wallet} ───────────────────────────────────────────────

@app.get("/api/drops/stakes/{wallet}")
async def get_player_stakes(wallet: str, chainId: int | None = Query(default=None)):
    from drops_service import get_player_stakes_db
    chain_id = chainId or CELO_CHAIN_ID
    stakes   = await get_player_stakes_db(pool, wallet, chain_id=chain_id)
    return {"success": True, "stakes": stakes, "chainId": chain_id}


# ─── /api/drops/claim-stake ───────────────────────────────────────────────────

@app.post("/api/drops/claim-stake")
async def claim_stake(body: ClaimStakeRequest):
    wallet   = body.walletAddress.lower()
    chain_id = body.chainId or CELO_CHAIN_ID
    try:
        result = await claim_stake_db(pool, wallet, body.stakeId, chain_id=chain_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ─── /api/players/sync-droplist/{wallet} — credit per-chain row ──────────────

@app.get("/api/players/sync-droplist/{wallet}")
async def sync_droplist_daily(wallet: str, chainId: int | None = Query(default=None)):
    """
    Called on page load.  Credits 10 DROPS to the per-chain balance row
    if the player claimed from the droplist within the last 24 hours.
    """
    from drops_service import _ensure_chain_balance_row
    wallet   = wallet.lower().strip()
    chain_id = chainId or CELO_CHAIN_ID
    now      = datetime.utcnow()

    droplist_res = (
        supabase.table("droplist_balances")
        .select("last_claim_at")
        .eq("wallet_address", wallet)
        .execute()
    )
    droplist_row = droplist_res.data[0] if droplist_res.data else None

    async with pool.acquire() as conn:
        await _ensure_chain_balance_row(conn, wallet, chain_id)
        player_row = await conn.fetchrow(
            """SELECT game_drops, reward_drops, total_duels, rematch_badge,
                      droplist_last_synced_at
               FROM challenge_players
               WHERE wallet_address=$1""",
            wallet,
        )
        bal_row = await conn.fetchrow(
            "SELECT game_drops FROM challenge_player_balances WHERE wallet_address=$1 AND chain_id=$2",
            wallet, chain_id,
        )

    if not player_row:
        raise HTTPException(status_code=404, detail="Player not found. Register first.")

    credited      = 0.0
    credit_reason = "no_action"

    if droplist_row and droplist_row.get("last_claim_at"):
        try:
            from datetime import timezone
            lca_str = str(droplist_row["last_claim_at"]).replace("Z", "+00:00")
            lca_dt  = datetime.fromisoformat(lca_str).astimezone(timezone.utc)
            now_utc = now.replace(tzinfo=timezone.utc)
            hours_since_claim = (now_utc - lca_dt).total_seconds() / 3600
        except Exception as e:
            logger.warning("sync-droplist: bad last_claim_at for %s: %s", wallet, e)
            hours_since_claim = 999

        if hours_since_claim > 24:
            credit_reason = "claim_older_than_24h"
        else:
            last_synced      = player_row["droplist_last_synced_at"]
            already_credited = False
            if last_synced:
                try:
                    ls_dt = last_synced.replace(tzinfo=timezone.utc) \
                            if last_synced.tzinfo is None \
                            else last_synced.astimezone(timezone.utc)
                    already_credited = ls_dt >= lca_dt
                except Exception:
                    already_credited = False

            if already_credited:
                credit_reason = "already_credited_today"
            else:
                DAILY_CLAIM_DROPS = 10.0
                async with pool.acquire() as conn:
                    # Credit the per-chain row
                    await conn.execute(
                        """UPDATE challenge_player_balances
                           SET game_drops=game_drops+$1, updated_at=NOW()
                           WHERE wallet_address=$2 AND chain_id=$3""",
                        DAILY_CLAIM_DROPS, wallet, chain_id,
                    )
                    # Update global sync timestamp
                    await conn.execute(
                        """UPDATE challenge_players
                           SET game_drops=game_drops+$1,
                               droplist_last_synced_at=$2
                           WHERE wallet_address=$3""",
                        DAILY_CLAIM_DROPS, now, wallet,
                    )
                credited      = DAILY_CLAIM_DROPS
                credit_reason = "daily_claim_credited"
    else:
        credit_reason = "no_droplist_claim_found"

    # Re-fetch updated chain balance
    from drops_service import (
        get_tier, BADGE_UNLOCK_DUELS, PRE_BADGE_MAX_STAKE,
        REDEEM_BADGE_QUALIFYING_GAMES,
    )
    async with pool.acquire() as conn:
        bal_row = await conn.fetchrow(
            """SELECT game_drops, reward_drops,
                      COALESCE(redeem_badge, false)  AS redeem_badge,
                      COALESCE(qualifying_duels, 0)   AS qualifying_duels
               FROM challenge_player_balances WHERE wallet_address=$1 AND chain_id=$2""",
            wallet, chain_id,
        )
        pr = await conn.fetchrow(
            "SELECT total_duels, rematch_badge FROM challenge_players WHERE wallet_address=$1",
            wallet,
        )

    total_duels      = int(pr["total_duels"] or 0) if pr else 0
    tier_name, apy   = get_tier(total_duels)
    qualifying_duels = int(bal_row["qualifying_duels"] or 0) if bal_row else 0
    redeem_badge     = bool(bal_row["redeem_badge"]) if bal_row else False
    stats = await _get_player_stats(wallet, chain_id)

    return {
        "success":         True,
        "chainId":         chain_id,
        "credited":        credited,
        "creditReason":    credit_reason,
        "gameDrops":       stats["game_drops"],
        "rewardDrops":     stats["reward_drops"],
        "tier":            stats["tier"],
        "apyPct":          stats["apy_pct"],
        "rematchBadge":    stats["rematch_badge"],
        "totalDuels":      stats["total_duels"],
        "gamesUntilBadge": stats["games_until_badge"],
        "maxStake":        stats["max_stake"],
        "redeemBadge":            stats["redeem_badge"],
        "qualifyingDuels":        stats["qualifying_duels"],
        "gamesUntilRedeemBadge":  stats["games_until_redeem_badge"],
    }
    

@app.post("/api/challenge/{code}/counter")
async def send_targeted_counter(code: str, body: dict):
    code           = code.upper()
    creator_wallet = body.get("creatorWallet", "").lower()
    creator_name   = body.get("creatorName", "")
    target_wallet  = body.get("targetWallet", "").lower()
    amount         = float(body.get("amount", 0))

    if amount < MIN_STAKE_DROPS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum stake is {MIN_STAKE_DROPS} DROPS."
        )

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]

    async with pool.acquire() as conn:
        creator_row = await conn.fetchrow(
            "SELECT total_duels FROM challenge_players WHERE wallet_address=$1", creator_wallet
        )
        target_row = await conn.fetchrow(
            "SELECT total_duels FROM challenge_players WHERE wallet_address=$1", target_wallet
        )

    creator_duels = int((creator_row or {}).get("total_duels") or 0)
    target_duels  = int((target_row  or {}).get("total_duels") or 0)

    if creator_duels < BADGE_UNLOCK_DUELS or target_duels < BADGE_UNLOCK_DUELS:
        raise HTTPException(
            status_code=400,
            detail=f"Stake negotiation is locked until both players have played {BADGE_UNLOCK_DUELS} games."
        )

    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Only the creator can send counter-offers")
    if target_wallet == creator_wallet:
        raise HTTPException(status_code=400, detail="Cannot counter your own wallet")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    block_reason = await check_pre_badge_rules(pool, creator_wallet, target_wallet, amount,chain_id=challenge.get("chainId"))
    if block_reason:
        raise HTTPException(status_code=403, detail=block_reason)
    
    last_counter_key = f"last_counter_{creator_wallet}_{target_wallet}"
    last_sent = challenge.get(last_counter_key)
    if last_sent:
        elapsed = (datetime.utcnow() - last_sent).total_seconds()
        if elapsed < 10:
            raise HTTPException(
                status_code=429,
                detail=f"Wait {int(10 - elapsed)}s before sending another counter to this player",
            )
    challenge[last_counter_key] = datetime.utcnow()

    if not creator_name:
        creator_name = (
            challenge.get("creatorName")
            or challenge["players"].get(creator_wallet, {}).get("username")
            or creator_wallet[:8]
        )

    await broadcast(code, {
        "type":         "pre_lobby_counter",
        "fromWallet":   creator_wallet,
        "fromName":     creator_name,
        "amount":       amount,
        "sentAt":       datetime.utcnow().isoformat(),
        "targetWallet": target_wallet,
    })

    return {"success": True, "amount": amount, "targetWallet": target_wallet}


@app.post("/api/challenge/{code}/pre-lobby-accept")
async def pre_lobby_accept(code: str, body: dict):
    code              = code.upper()
    creator_wallet    = body.get("creatorWallet", "").lower()
    challenger_wallet = body.get("challengerWallet", "").lower()
    amount            = float(body.get("amount", 0))

    if amount < MIN_STAKE_DROPS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum stake is {MIN_STAKE_DROPS} DROPS."
        )

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]
    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Only the creator can accept")
    if challenge.get("agreedStake") is not None:
        raise HTTPException(status_code=400, detail="Stake already agreed")

    # ── Pre-badge: stake-limit + already-played check ─────────────────────
    block_reason = await check_pre_badge_rules(pool, creator_wallet, challenger_wallet, amount, chain_id=challenge.get("chainId"))
    if block_reason:
        raise HTTPException(status_code=403, detail=block_reason)

    challenge["stakeAmount"] = amount
    challenge["agreedStake"] = amount
    await _persist_agreed_stake(code, amount)

    asyncio.create_task(
        _call_register_quiz_on_chain(code, creator_wallet, challenger_wallet, amount, challenge.get("chainId"))
    )

    await broadcast(code, {
        "type":       "pre_lobby_accepted",
        "amount":     amount,
        "winner":     challenger_wallet,
        "challenger": challenger_wallet,
        "by":         creator_wallet,
    })
    await broadcast(code, {
        "type":     "burn_now",
        "message":  f"Stake agreed: {amount} DROPS each. Please burn your DROPS now.",
        "amount":   amount,
        "quizCode": code,
    })
    return {"success": True, "amount": amount}

async def _persist_agreed_stake(code: str, amount: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_games SET stake_amount=$1, token_symbol='DROPS' WHERE code=$2",
            amount, code,
        )

# ─── Confirm Burn ─────────────────────────────────────────────────────────────

@app.post("/api/challenge/{code}/confirm-burn")
async def confirm_burn(code: str, body: dict):
    code              = code.upper()
    wallet            = body.get("walletAddress", "").lower()
    tx_hash           = body.get("txHash", "")
    success           = body.get("success", False)
    body_chain_id     = body.get("chainId")

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge          = challenges[code]
    effective_chain_id = body_chain_id or challenge.get("chainId")

    if wallet not in challenge["players"]:
        raise HTTPException(status_code=403, detail="Not a player in this challenge")

    if challenge["players"][wallet].get("txVerified"):
        return {"success": True, "alreadyVerified": True}

    if not success:
        raise HTTPException(status_code=400, detail="Frontend reported stake transaction as failed.")

    stake_amount = float(challenge.get("agreedStake") or challenge.get("stakeAmount", 0))
    try:
        await deduct_game_drops(pool, wallet, stake_amount,
                                reason=f"stake:game:{code}",
                                chain_id=effective_chain_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    challenge["players"][wallet]["txVerified"] = True
    challenge["players"][wallet]["txHash"]     = tx_hash

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_game_players SET tx_hash=$1, tx_verified=TRUE
               WHERE challenge_id=(SELECT id FROM challenge_games WHERE code=$2)
                 AND wallet_address=$3""",
            tx_hash, code, wallet,
        )

    await broadcast(code, {"type": "stake_verified", "wallet": wallet})

    # resolver calls confirmBurn for whoever just staked (creator in SP, both in PvP)
    asyncio.create_task(_call_confirm_burn_on_chain(code, wallet, effective_chain_id))

    # For single-player, also mark creator ready after their stake confirms
    if challenge.get("is_single_player") and wallet == challenge.get("creator"):
        challenge["players"][wallet]["ready"] = True
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_game_players SET ready=TRUE
                   WHERE challenge_id=(SELECT id FROM challenge_games WHERE code=$1)
                     AND wallet_address=$2""",
                code, wallet,
            )
        await broadcast(code, {"type": "player_ready", "wallet": wallet})
        await _maybe_auto_start(code)

    return {"success": True}

# ─── Join Challenge ───────────────────────────────────────────────────────────

@app.post("/api/challenge/{code}/join")
async def join_challenge(code: str, body: JoinChallengeRequest):
    code   = code.upper()
    joiner = body.walletAddress.lower()

    if code not in challenges:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM challenge_games WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")

        d           = dict(row)
        rounds_data = d.get("rounds_data") or {}
        if isinstance(rounds_data, str):
            rounds_data = json.loads(rounds_data)

        async with pool.acquire() as conn:
            player_rows = await conn.fetch(
                "SELECT wallet_address, username, tx_verified, ready FROM challenge_game_players WHERE challenge_id=$1",
                d["id"],
            )

        players_dict = {
            r["wallet_address"]: {
                "username":   r["username"],
                "points":     0,
                "ready":      r["ready"],
                "txVerified": r["tx_verified"],
            }
            for r in player_rows
        }

        challenges[code] = {
            "id":          str(d["id"]),
            "code":        code,
            "topic":       d["topic"],
            "creator":     d["creator_address"],
            "creatorName": d.get("creator_name", ""),
            "stakeAmount": float(d["stake_amount"]),
            "agreedStake": float(d["stake_amount"]),
            "token":       d["token_symbol"],
            "chainId":     d["chain_id"],
            "rounds":      rounds_data.get("rounds", []),
            "status":      d["status"],
            "isPublic":    d["is_public"],
            "players":     players_dict,
            "player_offers": {},
        }
        game_state[code] = {"answers": {}}

    challenge = challenges[code]

    if challenge["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Challenge is not open")
    if len(challenge["players"]) >= 2:
        raise HTTPException(status_code=400, detail="Challenge is already full")
    creator = challenge["creator"].lower()
    if joiner != creator:
        block_reason = await check_pre_badge_rules(
            pool, creator, joiner,
            challenge.get("agreedStake") or challenge.get("stakeAmount", MIN_STAKE_DROPS),
            chain_id=challenge.get("chainId"),  # ← add
        )
        if block_reason:
            raise HTTPException(status_code=403, detail=block_reason)
        
    if joiner in challenge["players"]:
        return {"success": True, "challenge": _safe_challenge(challenge)}

    FAKE_HASHES = {"pre-lobby-agreed", "sync-recovery", "auto-sync", "", None}
    tx_verified = body.txHash not in FAKE_HASHES

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_players (wallet_address, username, last_seen_at)
               VALUES ($1, $2, NOW())
               ON CONFLICT (wallet_address)
               DO UPDATE SET last_seen_at=NOW()""",
            joiner, body.username,
        )
        await conn.execute(
            """INSERT INTO challenge_game_players
                (challenge_id, wallet_address, username, tx_hash, tx_verified)
               VALUES ((SELECT id FROM challenge_games WHERE code=$1), $2, $3, $4, $5)
               ON CONFLICT (challenge_id, wallet_address)
               DO UPDATE SET tx_hash=$4, tx_verified=$5""",
            code, joiner, body.username, body.txHash, tx_verified,
        )
    joiner_profile = await _get_live_profile(joiner)

    challenge["players"][joiner] = {
        "username":   joiner_profile["username"],
        "avatar_url": joiner_profile["avatar_url"],   # ← add
        "points":     0,
        "ready":      False,
        "txVerified": tx_verified,
    }

    asyncio.create_task(
        notif.notify_player_joined(code, body.username, challenge["creator"])
    )

    await broadcast(code, {
        "type":   "player_joined",
        "player": {
            "walletAddress": joiner,
            "username":      joiner_profile["username"],
            "avatar_url":    joiner_profile["avatar_url"],   # ← add
        },
    })
    await _maybe_auto_start(code)
    return {"success": True, "challenge": _safe_challenge(challenge)}

# ─── Sync Stake ───────────────────────────────────────────────────────────────

@app.post("/api/challenge/{code}/sync-stake")
async def sync_stake(code: str, body: dict):
    code   = code.upper()
    wallet = body.get("walletAddress", "").lower()

    if not wallet:
        raise HTTPException(status_code=400, detail="walletAddress required")

    # Only hydrate if truly missing — never overwrite an already-live challenge
    if code not in challenges:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM challenge_games WHERE code=$1", code
            )
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")

        d           = dict(row)
        rounds_data = d.get("rounds_data") or {}
        if isinstance(rounds_data, str):
            rounds_data = json.loads(rounds_data)

        async with pool.acquire() as conn:
            player_rows = await conn.fetch(
                """SELECT wallet_address, username, tx_verified, ready
                   FROM challenge_game_players WHERE challenge_id=$1""",
                d["id"],
            )

        challenges[code] = {
            "id":          str(d["id"]),
            "code":        code,
            "topic":       d["topic"],
            "creator":     d["creator_address"],
            "stakeAmount": float(d["stake_amount"]),
            "agreedStake": float(d["stake_amount"]),
            "token":       d["token_symbol"],
            "chainId":     d["chain_id"],
            "rounds":      rounds_data.get("rounds", []),
            "status":      d["status"],
            "isPublic":    d["is_public"],
            "players": {
                r["wallet_address"]: {
                    "username":   r["username"],
                    "points":     0,
                    "ready":      r["ready"],
                    "txVerified": r["tx_verified"],
                }
                for r in player_rows
            },
            "player_offers": {},
        }
        game_state[code] = {"answers": {}}
    else:
        # Challenge already in memory — just make sure this wallet is listed.
        # Re-fetch their individual DB row to pick up any txVerified written
        # by a concurrent confirm-burn, without clobbering the other player.
        challenge = challenges[code]
        if wallet not in challenge["players"]:
            async with pool.acquire() as conn:
                prow = await conn.fetchrow(
                    """SELECT wallet_address, username, tx_verified, ready
                       FROM challenge_game_players
                       WHERE challenge_id = (SELECT id FROM challenge_games WHERE code=$1)
                         AND wallet_address = $2""",
                    code, wallet,
                )
            if prow:
                challenge["players"][wallet] = {
                    "username":   prow["username"],
                    "points":     0,
                    "ready":      prow["ready"],
                    "txVerified": prow["tx_verified"],
                }
        else:
            # Player exists in memory — sync their txVerified from DB
            # without touching other players' state
            async with pool.acquire() as conn:
                prow = await conn.fetchrow(
                    """SELECT tx_verified, ready FROM challenge_game_players
                       WHERE challenge_id = (SELECT id FROM challenge_games WHERE code=$1)
                         AND wallet_address = $2""",
                    code, wallet,
                )
            if prow:
                # Merge: keep True if already set in memory (from confirm-burn)
                challenges[code]["players"][wallet]["txVerified"] = (
                    challenges[code]["players"][wallet].get("txVerified")
                    or prow["tx_verified"]
                )
                challenges[code]["players"][wallet]["ready"] = (
                    challenges[code]["players"][wallet].get("ready")
                    or prow["ready"]
                )

    challenge = challenges[code]

    if wallet not in challenge["players"]:
        raise HTTPException(status_code=403, detail="You are not in this challenge")

    if challenge["players"][wallet].get("txVerified"):
        return {"success": True, "alreadyVerified": True}

    challenge_chain_id = challenges[code].get("chainId") or CELO_CHAIN_ID
    cfg_for_sync       = get_chain(challenge_chain_id)

    # ── Try 1: QuizHub contract flags ────────────────────────────────────────
    contract_verified = False
    try:
        GET_QUIZ_ABI = [{
            "inputs": [{"internalType": "bytes32", "name": "quizId", "type": "bytes32"}],
            "name":   "getQuiz",
            "outputs": [{
                "components": [
                    {"internalType": "bytes32",  "name": "id",               "type": "bytes32"},
                    {"internalType": "address",  "name": "creator",          "type": "address"},
                    {"internalType": "address",  "name": "player1",          "type": "address"},
                    {"internalType": "address",  "name": "player2",          "type": "address"},
                    {"internalType": "uint256",  "name": "stakePerPlayer",   "type": "uint256"},
                    {"internalType": "uint8",    "name": "status",           "type": "uint8"},
                    {"internalType": "address",  "name": "winner",           "type": "address"},
                    {"internalType": "bool",     "name": "p1BurnConfirmed",  "type": "bool"},
                    {"internalType": "bool",     "name": "p2BurnConfirmed",  "type": "bool"},
                    {"internalType": "uint256",  "name": "createdAt",        "type": "uint256"},
                    {"internalType": "uint256",  "name": "registeredAt",     "type": "uint256"},
                    {"internalType": "uint256",  "name": "startedAt",        "type": "uint256"},
                    {"internalType": "uint256",  "name": "finishedAt",       "type": "uint256"},
                ],
                "internalType": "struct QuizHub.Quiz",
                "name": "",
                "type": "tuple",
            }],
            "stateMutability": "view",
            "type": "function",
        }]

        # Use build_w3_for_chain so POA middleware (Botchain) is always injected.
        w3, _ = build_w3_for_chain(cfg_for_sync.id)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(cfg_for_sync.quiz_hub),
            abi=GET_QUIZ_ABI,
        )
        quiz_id_bytes = Web3.keccak(text=code)
        quiz          = contract.functions.getQuiz(quiz_id_bytes).call()
        player1       = quiz[2].lower()
        player2       = quiz[3].lower()

        if wallet == player1:
            contract_verified = quiz[7]   # p1BurnConfirmed
        elif wallet == player2:
            contract_verified = quiz[8]   # p2BurnConfirmed

    except Exception as e:
        logger.warning("sync-stake contract check failed chain=%s: %s", cfg_for_sync.name, e)

    if contract_verified:
        logger.info("sync-stake: contract confirmed  code=%s  wallet=%s  chain=%s", code, wallet, cfg_for_sync.name)
        await _mark_verified_and_broadcast(code, wallet, tx_hash=None, pool=pool, chain_id=challenge_chain_id)
        return {"success": True, "verified": True, "source": "contract"}

    # ── Try 2: Scan DROPS token logs ─────────────────────────────────────────
    verified, burned_amount = await _verify_redeem_tx(
        tx_hash="",
        expected_wallet=wallet,
        expected_code=code,
        chain_id=challenge_chain_id,
        scan_mode=True,
    )

    if not verified:
        return {
            "success": False,
            "verified": False,
            "message": "No DROPS redeem found on-chain for this wallet and quiz code.",
        }

    logger.info(
        "sync-stake: log scan confirmed  code=%s  wallet=%s  burned=%s  chain=%s",
        code, wallet, burned_amount, cfg_for_sync.name,
    )
    await _mark_verified_and_broadcast(code, wallet, tx_hash=None, pool=pool, chain_id=challenge_chain_id)
    asyncio.create_task(_call_confirm_burn_on_chain(code, wallet, challenge_chain_id))
    return {"success": True, "verified": True, "source": "log_scan"}

async def _mark_verified_and_broadcast(
    code: str, wallet: str, tx_hash: str | None, pool, chain_id: int | None = None
) -> None:
    # Deduct stake if not already deducted (sync-stake path)
    challenge    = challenges.get(code, {})
    effective_chain_id = chain_id or challenge.get("chainId") or CELO_CHAIN_ID
    stake_amount = float(challenge.get("agreedStake") or challenge.get("stakeAmount", 0))
    if stake_amount > 0:
        try:
            await deduct_game_drops(pool, wallet, stake_amount,
                                    reason=f"stake:sync:{code}",
                                    chain_id=effective_chain_id)
        except ValueError as e:
            # Already deducted or insufficient — log and continue
            # (confirm-burn may have already done it)
            logger.warning(
                "_mark_verified_and_broadcast deduct skipped  wallet=%s  code=%s  reason=%s",
                wallet, code, e,
            )

    challenges[code]["players"][wallet]["txVerified"] = True
    if tx_hash:
        challenges[code]["players"][wallet]["txHash"] = tx_hash

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_game_players
               SET tx_verified = TRUE
               {tx_clause}
               WHERE challenge_id = (SELECT id FROM challenge_games WHERE code=$2)
                 AND wallet_address = $3""".format(
                tx_clause=", tx_hash=$1" if tx_hash else ""
            ),
            *([tx_hash, code, wallet] if tx_hash else [code, wallet]),
        )

    await broadcast(code, {"type": "stake_verified", "wallet": wallet})
    
# ─── Challenge Read Endpoints ─────────────────────────────────────────────────

@app.get("/api/challenge/lobby")
async def get_lobby(
    limit:  int = Query(default=20, le=50),
    offset: int = Query(default=0),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM challenge_public_lobby LIMIT $1 OFFSET $2", limit, offset,
        )
    return {"success": True, "challenges": [dict(r) for r in rows]}


@app.get("/api/challenge/{code}")
async def get_challenge(code: str):
    code = code.upper()
    if code in challenges:
        c = challenges[code]
        creator = c.get("creator", "")
        if creator:
            profile = await _get_live_profile(creator)
            c["creatorName"]   = profile["username"]
            c["creatorAvatar"] = profile["avatar_url"]
            # Also refresh creator's avatar in players dict
            if creator in c["players"]:
                c["players"][creator]["avatar_url"] = profile["avatar_url"]  # ← add
        return {"success": True, "challenge": _safe_challenge(c)}

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM challenge_games WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")

        d = dict(row)
        d["stakeAmount"]    = float(d.get("stake_amount", 0))
        d["stake"]          = d["stakeAmount"]
        d["agreedStake"]    = d["stakeAmount"]
        d["token"]          = d.get("token_symbol", "")
        d["chainId"]        = d.get("chain_id")
        d["isPublic"]       = d.get("is_public")
        d["creator"]        = d.get("creator_address", "")
        d["winner_address"] = d.get("winner_address")

        creator_profile = supabase.table("user_profiles").select("username, avatar_url").eq("wallet_address", d["creator"]).execute()
        if creator_profile.data:
            d["creatorName"]  = creator_profile.data[0].get("username") or d["creator"][:8]
            d["creatorAvatar"] = creator_profile.data[0].get("avatar_url") or ""
        else:
            creator_row = await conn.fetchrow(
                "SELECT username FROM challenge_players WHERE wallet_address=$1", d["creator"]
            )
            d["creatorName"]  = creator_row["username"] if creator_row else d["creator"][:8]
            d["creatorAvatar"] = ""
        
        creator_duels_row = await conn.fetchrow(
            "SELECT total_duels FROM challenge_players WHERE wallet_address=$1", d["creator"]
        )
        creator_duels = int((creator_duels_row or {}).get("total_duels") or 0)
        d["negotiationLocked"] = creator_duels < BADGE_UNLOCK_DUELS
        
        player_rows = await conn.fetch(
            """SELECT wallet_address, username, tx_verified, ready,
                    COALESCE(final_points, 0) AS points
            FROM challenge_game_players WHERE challenge_id=$1""",
            d["id"],
        )

        # Batch-fetch all player profiles from user_profiles
        player_wallets = [r["wallet_address"] for r in player_rows]
        profiles_res = supabase.table("user_profiles")\
            .select("wallet_address, username, avatar_url, bio")\
            .in_("wallet_address", player_wallets)\
            .execute()
        profiles_map = {p["wallet_address"]: p for p in (profiles_res.data or [])}

        d["players"] = {
            r["wallet_address"]: {
                "username":   profiles_map.get(r["wallet_address"], {}).get("username") or r["username"],
                "avatar_url": profiles_map.get(r["wallet_address"], {}).get("avatar_url") or "",
                "bio":        profiles_map.get(r["wallet_address"], {}).get("bio") or "",
                "points":     r["points"],
                "ready":      r["ready"],
                "txVerified": r["tx_verified"],
            }
            for r in player_rows
        }
    return {"success": True, "challenge": d}

# ─── Drops Endpoints ─────────────────────────────────────────────────────────



@app.post("/api/players/confirm-welcome")
async def confirm_welcome(body: dict):
    wallet   = body.get("walletAddress", "").lower()
    tx_hash  = body.get("txHash", "")
    chain_id = body.get("chainId") or CELO_CHAIN_ID

    if not wallet or not tx_hash:
        raise HTTPException(status_code=400, detail="walletAddress and txHash required")

    from drops_service import credit_welcome_bonus
    try:
        result = await credit_welcome_bonus(pool, wallet, tx_hash, chain_id=chain_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("welcome confirmed  wallet=%s  chain=%s  tx=%s", wallet, chain_id, tx_hash)
    return {"success": True, **result}

DAILY_CLAIM_DROPS = 10.0   # fixed daily reward amount
 

@app.post("/api/drops/redeem")
async def redeem_drops(body: RedeemRequest):
    if body.dropsAmount <= 0:
        raise HTTPException(status_code=400, detail="dropsAmount must be > 0")
    try:
        result = await redeem_reward_drops(
            pool,
            body.walletAddress.lower(),
            body.dropsAmount,
            player_g=body.playerG,
            fee_g=body.feeG,
            staked_drops=body.stakedDrops,
            apy_pct=body.apyPct,
            g_price_usd=body.gPriceUsd,
            chain_id=body.chainId,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    

@app.get("/api/drops/redeem-history/{wallet}")
async def redeem_history(wallet: str, limit: int = Query(default=20, le=50)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, drops_burned, g_price_usd, player_g, fee_g, staked_g,
                      stake_pool_id, tx_hash, created_at
               FROM challenge_redeem_history
               WHERE wallet_address=$1
               ORDER BY created_at DESC LIMIT $2""",
            wallet.lower(), limit,
        )
    results = []
    for r in rows:
        d = dict(r)
        d["id"]            = str(d["id"])
        d["stake_pool_id"] = str(d["stake_pool_id"]) if d["stake_pool_id"] else None
        d["created_at"]    = d["created_at"].isoformat()
        results.append(d)
    return {"success": True, "history": results}


@app.get("/api/drops/preview-redeem")
async def preview_redeem(wallet: str, drops: float = Query(gt=0)):
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_duels, reward_drops FROM challenge_players WHERE wallet_address=$1", wallet
        )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")

    try:
        g_price_usd = await _fetch_g_price_usd()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=f"Price feed unavailable: {e}")

    _, apy_pct       = get_tier(int(row["total_duels"]))
    usd_value        = drops / 100.0
    player_usd       = usd_value * 0.75
    stake_usd        = usd_value * 0.25
    fee_usd          = player_usd * 0.10
    stake_earned_usd = stake_usd * (apy_pct / 100.0)

    return {
        "success":         True,
        "dropsToRedeem":   drops,
        "availableReward": float(row["reward_drops"]),
        "gPriceUsd":       g_price_usd,
        "playerG":         player_usd / g_price_usd,
        "feeG":            fee_usd    / g_price_usd,
        "stakedG":         stake_usd  / g_price_usd,
        "stakeEarnedG":    stake_earned_usd / g_price_usd,
        "apyPct":          apy_pct,
        "sufficient":      float(row["reward_drops"]) >= drops,
    }
# ─── Claim (winner minting) ───────────────────────────────────────────────────
 
@app.get("/api/challenge/{code}/claim-status/{wallet}")
async def claim_status(code: str, wallet: str):
    """
    Returns the pending claim state for a player in a finished game.
 
    Response:
        {
          "hasClaim": true,
          "claimed": false,
          "dropAmount": 20.0,
          "reason": "tie_refund",   // or "game_win"
          "txHash": null            // set once claimed
        }
 
    If no claim row exists (e.g. player wasn't in the game):
        { "hasClaim": false }
    """
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT drops_amount, reason, claimed, tx_hash
               FROM challenge_pending_claims
               WHERE wallet_address = $1
                 AND game_code      = $2""",
            wallet, code,
        )
 
    if not row:
        return {"hasClaim": False}
 
    return {
        "hasClaim":   True,
        "claimed":    row["claimed"],
        "dropAmount": float(row["drops_amount"]),
        "reason":     row["reason"],
        "txHash":     row["tx_hash"],
    }
 
@app.post("/api/challenge/claim")
async def claim_reward(body: ClaimRequest):
    logger.info("claim_reward body: %s", body.model_dump())
    wallet = (body.wallet or body.walletAddress or "").strip()
    if not wallet:
        raise HTTPException(status_code=400, detail="wallet or walletAddress is required")
    try:
        return await claim_pending_drops(
            pool   = pool,
            wallet = wallet,
            code   = body.code,
            notif  = notif,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/challenge/{wallet}/pending-claims")
async def get_pending_claims(wallet: str):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT pc.game_code   AS code,
                      g.topic,
                      pc.drops_amount AS win_amount,
                      g.token_symbol,
                      g.chain_id
               FROM challenge_pending_claims pc
               JOIN challenge_games g ON g.code = pc.game_code
               WHERE pc.wallet_address = $1
                 AND pc.claimed = FALSE""",
            wallet.lower(),
        )
    return {"success": True, "claims": [dict(r) for r in rows]}


@app.get("/api/challenge/{wallet}/history")
async def challenge_history(
    wallet:   str,
    limit:    int = Query(default=50, le=200),
    chain_id: int | None = Query(default=None, description="Omit for all chains"),
    chainId:  int | None = Query(default=None, description="camelCase alias"),
):
    """Match history for one wallet. Chain-filtered so it lines up with
    /api/ranks and /api/players/{wallet}/balances."""
    cid = chain_id if chain_id is not None else chainId

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT g.code, g.topic, g.stake_amount, g.token_symbol,
                      g.status, g.winner_address, g.chain_id,
                      g.created_at, g.finished_at,
                      COALESCE(g.is_single_player, FALSE) AS is_single_player,
                      opp.wallet_address                  AS opponent_wallet,
                      COALESCE(up.username, opp.username) AS opponent_username
               FROM challenge_games g
               JOIN challenge_game_players me
                    ON me.challenge_id = g.id
                   AND LOWER(me.wallet_address) = LOWER($1)
               LEFT JOIN challenge_game_players opp
                    ON opp.challenge_id = g.id
                   AND LOWER(opp.wallet_address) <> LOWER($1)
               LEFT JOIN user_profiles up
                    ON LOWER(up.wallet_address) = LOWER(opp.wallet_address)
               WHERE ($2::int IS NULL OR g.chain_id = $2)
               ORDER BY COALESCE(g.finished_at, g.created_at) DESC
               LIMIT $3""",
            wallet, cid, limit,
        )

    history = []
    for r in rows:
        d = dict(r)
        d["stake_amount"] = float(d["stake_amount"] or 0)
        history.append(d)

    finished = [h for h in history if h["status"] == "finished"]
    wins     = [h for h in finished
                if (h["winner_address"] or "").lower() == wallet.lower()]

    return {
        "success":  True,
        "chainId":  cid,
        "history":  history,
        "counts": {
            "finished": len(finished),
            "won":      len(wins),
        },
    }
    
# ─── Rematch Flow ─────────────────────────────────────────────────────────────

@app.post("/api/challenge/{code}/rematch-invite")
async def send_rematch_invite(code: str, body: dict):
    code      = code.upper()
    requester = body.get("requesterWallet", "").lower()

    if not requester:
        raise HTTPException(status_code=400, detail="requesterWallet required")

    if code in challenges:
        c       = challenges[code]
        topic   = c["topic"]
        stake   = c.get("stakeAmount", c.get("stake", 0))
        token   = c["token"]
        players = {w: p["username"] for w, p in c["players"].items()}
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM challenge_games WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")
        d = dict(row)
        topic = d["topic"]
        stake = float(d["stake_amount"])
        token = d["token_symbol"]
        async with pool.acquire() as conn:
            prows = await conn.fetch(
                "SELECT wallet_address, username FROM challenge_game_players WHERE challenge_id=$1",
                d["id"],
            )
        players = {r["wallet_address"]: r["username"] for r in prows}

    if requester not in players:
        raise HTTPException(status_code=403, detail="You were not part of this challenge")

    last_rematch_key = f"last_rematch_{requester}"
    last_sent = challenges.get(code, {}).get(last_rematch_key)
    if last_sent:
        elapsed = (datetime.utcnow() - last_sent).total_seconds()
        if elapsed < 30:
            raise HTTPException(
                status_code=429,
                detail=f"Wait {int(30 - elapsed)}s before sending another rematch invite",
            )
    if code in challenges:
        challenges[code][last_rematch_key] = datetime.utcnow()

    requester_name  = players[requester]
    opponent_wallet = next((w for w in players if w != requester), None)
    if not opponent_wallet:
        raise HTTPException(status_code=400, detail="Cannot find opponent")

    await broadcast(code, {
        "type":            "rematch_invite",
        "originalCode":    code,
        "topic":           topic,
        "stakeAmount":     stake,
        "tokenSymbol":     token,
        "requesterWallet": requester,
        "requesterName":   requester_name,
    })

    async def _rematch_timeout():
        await asyncio.sleep(30)
        already_rematched = any(
            challenges.get(c_code, {}).get("rematch_of") == code
            for c_code in list(challenges)
        )
        if not already_rematched:
            await broadcast(code, {
                "type":            "rematch_timeout",
                "requesterWallet": requester,
                "message":         "Rematch request expired — opponent did not respond.",
            })

    asyncio.create_task(_rematch_timeout())
    return {"success": True, "opponentWallet": opponent_wallet}


@app.post("/api/challenge/{code}/rematch-accept-invite")
async def accept_rematch_invite(code: str, body: dict):
    code             = code.upper()
    acceptor         = body.get("acceptorWallet",  "").lower()
    requester_wallet = body.get("requesterWallet", "").lower()

    if not acceptor or not requester_wallet:
        raise HTTPException(status_code=400, detail="acceptorWallet and requesterWallet required")

    # Load original challenge
    if code in challenges:
        c        = challenges[code]
        topic    = c["topic"]
        stake    = c.get("stakeAmount", c.get("stake", 0))
        token    = c["token"]
        chain_id = c["chainId"]
        orig_id  = c["id"]
        players  = {w: p["username"] for w, p in c["players"].items()}
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM challenge_games WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Original challenge not found")
        d        = dict(row)
        topic    = d["topic"]
        stake    = float(d["stake_amount"])
        token    = d["token_symbol"]
        chain_id = d["chain_id"]
        orig_id  = str(d["id"])
        async with pool.acquire() as conn:
            prows = await conn.fetch(
                "SELECT wallet_address, username FROM challenge_game_players WHERE challenge_id=$1",
                d["id"],
            )
        players = {r["wallet_address"]: r["username"] for r in prows}

    if requester_wallet not in players or acceptor not in players:
        raise HTTPException(status_code=403, detail="Not a participant of the original challenge")

    requester_username = players[requester_wallet]
    acceptor_name      = players[acceptor]

    questions_data = await generate_questions(topic)
    new_code       = make_code()
    new_id         = str(uuid.uuid4())

    # ── Create as "pending" — requester must sign on-chain before it opens ──
    new_challenge = {
        "id":               new_id,
        "code":             new_code,
        "topic":            topic,
        "creator":          requester_wallet,
        "creatorName":      requester_username,
        "stakeAmount":      stake,
        "agreedStake":      None,
        "token":            token,
        "chainId":          chain_id,
        "rounds":           questions_data["rounds"],
        "status":           "pending",   # ← pending until on-chain confirmed
        "isPublic":         False,
        "inviteWallet":     acceptor,
        "players": {
            requester_wallet: {
                "username":   requester_username,
                "points":     0,
                "ready":      False,
                "txVerified": False,
            }
        },
        "player_offers":    {},
        "onChainConfirmed": False,
        "rematch_of":       code,
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_games
               (id, code, creator_address, topic, stake_amount, token_symbol,
                chain_id, status, is_public, rounds_data, rematch_of)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'pending',FALSE,$8,$9)""",
            new_id, new_code, requester_wallet, topic, stake, token,
            chain_id, json.dumps(questions_data), orig_id,
        )
        await conn.execute(
            "INSERT INTO challenge_game_players (challenge_id, wallet_address, username) VALUES ($1,$2,$3)",
            new_id, requester_wallet, requester_username,
        )

    challenges[new_code] = new_challenge
    game_state[new_code] = {"answers": {}}

    # Tell both players a new code exists; requester needs to sign
    await broadcast(code, {
        "type":            "rematch_invite_accepted",
        "originalCode":    code,
        "newCode":         new_code,
        "acceptorWallet":  acceptor,
        "acceptorName":    acceptor_name,
        "requesterWallet": requester_wallet,
    })

    return {"success": True, "newCode": new_code}


@app.post("/api/challenge/{code}/rematch-on-chain-confirmed")
async def rematch_on_chain_confirmed(code: str, body: dict):
    """
    Called by the requester (creator) after signing createQuiz() on-chain.
    Flips the rematch challenge to 'waiting' and broadcasts rematch_ready
    so both players route to pre-lobby.
    """
    code           = code.upper()
    creator_wallet = body.get("creatorWallet", "").lower()
    tx_hash        = body.get("txHash", "")

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]

    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Not the creator")

    if challenge["status"] == "waiting":
        return {"success": True, "alreadyConfirmed": True}

    if challenge["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot confirm — status is '{challenge['status']}'"
        )

    challenge["status"]           = "waiting"
    challenge["onChainConfirmed"] = True
    challenge["txHash"]           = tx_hash

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_games SET status='waiting', tx_hash=$1 WHERE code=$2",
            tx_hash, code,
        )

    original_code = challenge.get("rematch_of", "")

    # Broadcast on BOTH the new code channel and the original code channel
    # so acceptor (still on the old page) also receives the signal
    payload = {
        "type":            "rematch_ready",
        "newCode":         code,
        "originalCode":    original_code,
        "requesterWallet": creator_wallet,
    }
    await broadcast(code, payload)
    if original_code:
        await broadcast(original_code, payload)

    return {"success": True, "newCode": code}

@app.post("/api/challenge/{code}/rematch")
async def request_rematch(code: str, body: RematachRequest):
    code      = code.upper()
    requester = body.requesterWallet.lower()

    if code in challenges:
        c        = challenges[code]
        topic    = c["topic"]
        stake    = c.get("stakeAmount", c.get("stake", 0))
        token    = c["token"]
        chain_id = c["chainId"]
        orig_id  = c["id"]
        players  = {w: p["username"] for w, p in c["players"].items()}
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM challenge_games WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Original challenge not found")
        d        = dict(row)
        topic    = d["topic"]
        stake    = float(d["stake_amount"])
        token    = d["token_symbol"]
        chain_id = d["chain_id"]
        orig_id  = str(d["id"])
        async with pool.acquire() as conn:
            prows = await conn.fetch(
                "SELECT wallet_address, username FROM challenge_game_players WHERE challenge_id=$1",
                d["id"],
            )
        players = {r["wallet_address"]: r["username"] for r in prows}

    if requester not in players:
        raise HTTPException(status_code=403, detail="You were not part of this challenge")

    requester_username = players[requester]
    opponent_wallet    = next((w for w in players if w != requester), None)
    if not opponent_wallet:
        raise HTTPException(status_code=400, detail="Cannot find opponent")
    async with pool.acquire() as conn:
        req_row = await conn.fetchrow(
            "SELECT rematch_badge FROM challenge_players WHERE wallet_address=$1", requester
        )
        opp_row = await conn.fetchrow(
            "SELECT rematch_badge FROM challenge_players WHERE wallet_address=$1", opponent_wallet
        )
    req_badge = bool((req_row or {}).get("rematch_badge"))
    opp_badge = bool((opp_row or {}).get("rematch_badge"))
    if not req_badge or not opp_badge:
        raise HTTPException(
            status_code=403,
            detail="Both players must earn the Rematch Badge (10 games) before rematching.",
        )

    questions_data = await generate_questions(topic)
    new_code       = make_code()
    new_id         = str(uuid.uuid4())

    new_challenge = {
        "id":           new_id,
        "code":         new_code,
        "topic":        topic,
        "creator":      requester,
        "creatorName":  requester_username,
        "stakeAmount":  stake,
        "agreedStake":  None,
        "token":        token,
        "chainId":      chain_id,
        "rounds":       questions_data["rounds"],
        "status":       "waiting",
        "isPublic":     False,
        "inviteWallet": opponent_wallet,
        "players": {
            requester: {
                "username":   requester_username,
                "points":     0,
                "ready":      False,
                "txVerified": False,
            }
        },
        "player_offers":    {},
        "onChainConfirmed": False,
        "rematch_of":       code,
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_games
               (id, code, creator_address, topic, stake_amount, token_symbol,
                chain_id, status, is_public, rounds_data, rematch_of)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'waiting',FALSE,$8,$9)""",
            new_id, new_code, requester, topic, stake, token,
            chain_id, json.dumps(questions_data), orig_id,
        )
        await conn.execute(
            "INSERT INTO challenge_game_players (challenge_id, wallet_address, username) VALUES ($1,$2,$3)",
            new_id, requester, requester_username,
        )

    challenges[new_code] = new_challenge
    game_state[new_code] = {"answers": {}}

    await broadcast(code, {
        "type":            "rematch_ready",
        "newCode":         new_code,
        "topic":           topic,
        "stakeAmount":     stake,
        "tokenSymbol":     token,
        "requesterWallet": requester,
        "requesterName":   requester_username,
    })
    return {
        "success":        True,
        "newCode":        new_code,
        "opponentWallet": opponent_wallet,
        "challenge":      _safe_challenge(new_challenge),
    }


@app.post("/api/challenge/{code}/rematch-decline")
async def decline_rematch_invite(code: str, body: dict):
    code     = code.upper()
    decliner = body.get("declinerWallet", "").lower()

    if not decliner:
        raise HTTPException(status_code=400, detail="declinerWallet required")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username FROM challenge_players WHERE wallet_address=$1", decliner
        )
    decliner_name = row["username"] if row else f"User{decliner[-4:].upper()}"

    await broadcast(code, {
        "type":           "rematch_declined",
        "declinerWallet": decliner,
        "declinerName":   decliner_name,
    })
    return {"success": True}

# ─── Profile Endpoints ────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")

def _validate_username(username: str) -> Optional[str]:
    if len(username) < 3:
        return "Username must be at least 3 characters"
    if len(username) > 24:
        return "Username must be 24 characters or fewer"
    if not _USERNAME_RE.match(username):
        return "Letters, numbers, and underscores only"
    return None

_PROFILE_COLS = """
    wallet_address,
    username,
    COALESCE(avatar_url, '') AS avatar_url,
    COALESCE(bio,        '') AS bio,
    COALESCE(email,      '') AS email,
    COALESCE(phone,      '') AS phone
"""


@app.get("/api/profile/{wallet}")
async def get_profile_by_wallet(wallet: str, chainId: int | None = Query(default=None)):
    wallet = wallet.lower()

    res = supabase.table("user_profiles").select("*").eq("wallet_address", wallet).execute()
    if not res.data:
        return {"success": True, "profile": None}

    profile = res.data[0]
    stats   = await _get_player_stats(wallet, chainId)   # None → all chains

    profile.update({
        "game_drops":    stats["game_drops"],
        "reward_drops":  stats["reward_drops"],
        "tier":          stats["tier"],
        "apy_pct":       stats["apy_pct"],
        "rematch_badge": stats["rematch_badge"],
        "redeem_badge":  stats["redeem_badge"],
        "total_duels":   stats["total_duels"],
        "total_wins":    stats["total_wins"],
        "total_ties":    stats["total_ties"],
        "total_earned":  stats["total_earned"],
        "win_rate":      stats["win_rate"],
        "chain_id":      chainId,
        "chains":        stats["chains"],
    })

    return {"success": True, "profile": profile}


@app.get("/api/profile/user/{username}")
async def get_profile_by_username(username: str, chainId: int | None = Query(default=None)):
    res = supabase.table("user_profiles").select("*").ilike("username", username).execute()
    if not res.data:
        return {"success": False, "profile": None}

    profile = res.data[0]
    stats   = await _get_player_stats(profile["wallet_address"], chainId)

    profile.update({
        "game_drops":    stats["game_drops"],
        "reward_drops":  stats["reward_drops"],
        "tier":          stats["tier"],
        "apy_pct":       stats["apy_pct"],
        "rematch_badge": stats["rematch_badge"],
        "redeem_badge":  stats["redeem_badge"],
        "total_duels":   stats["total_duels"],
        "total_wins":    stats["total_wins"],
        "total_ties":    stats["total_ties"],
        "total_earned":  stats["total_earned"],
        "win_rate":      stats["win_rate"],
        "chain_id":      chainId,
        "chains":        stats["chains"],
    })

    return {"success": True, "profile": profile}

@app.post("/api/profile/update")
async def update_profile(body: UpdateProfileRequest):
    wallet   = body.wallet_address.lower()
    username = body.username.strip()

    if not wallet:
        raise HTTPException(status_code=400, detail="wallet_address is required")
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    err = _validate_username(username)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Check uniqueness against user_profiles
    conflict = supabase.table("user_profiles").select("wallet_address").eq("username", username).neq("wallet_address", wallet).execute()
    if conflict.data:
        raise HTTPException(status_code=409, detail="Username is already taken")

    # Update shared user_profiles
    supabase.table("user_profiles").upsert({
        "wallet_address": wallet,
        "username":       username,
        "avatar_url":     body.avatar_url or "",
        "bio":            body.bio or "",
        "email":          body.email or "",
        "updated_at":     datetime.utcnow().isoformat(),
    }, on_conflict="wallet_address").execute()

    # Also keep challenge_players username in sync
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_players SET username=$1 WHERE wallet_address=$2",
            username, wallet,
        )

    profile = supabase.table("user_profiles").select("*").eq("wallet_address", wallet).execute().data[0]
    return {"success": True, "profile": profile}


@app.post("/api/profile/check-availability")
async def check_availability(body: CheckAvailabilityRequest):
    value  = body.value.strip()
    wallet = body.current_wallet.lower()

    if body.field.lower() != "username":
        return {"available": True, "message": "Available"}

    err = _validate_username(value)
    if err:
        return {"available": False, "message": err}

    # Check against shared user_profiles
    res = supabase.table("user_profiles").select("wallet_address").ilike("username", value).execute()
    if res.data and res.data[0]["wallet_address"].lower() != wallet:
        return {"available": False, "message": "Username is already taken"}
    return {"available": True, "message": "Available"}


@app.post("/api/profile/sync")
async def sync_profile(req: SyncProfileRequest):
    try:
        wallet = req.wallet_address.lower()
        
        existing = supabase.table("user_profiles").select("*").eq("wallet_address", wallet).execute()
        if existing.data:
            return {"success": True, "profile": existing.data[0]}

        username_check = supabase.table("user_profiles").select("username").eq("username", req.username).execute()
        final_username = req.username if not username_check.data else f"{req.username}_{wallet[-4:]}"

        new_profile = {
            "wallet_address": wallet,
            "username":       final_username,
            "avatar_url":     req.avatar_url or "",
            "email":          req.email or "",
        }
        insert_res = supabase.table("user_profiles").insert(new_profile).execute()
        return {"success": True, "profile": insert_res.data[0]}
    except Exception as e:
        return {"success": False, "error": str(e)}
# ─── Ranks ────────────────────────────────────────────────────────────────────

async def _take_rank_snapshot() -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT wallet_address FROM challenge_players
               WHERE COALESCE(total_duels, 0) > 0
               ORDER BY total_wins DESC, total_duels DESC"""
        )
        if not rows:
            return
        today = __import__("datetime").date.today()
        await conn.executemany(
            """INSERT INTO challenge_rank_snapshots (wallet_address, rank_position, snapshotted_at)
               VALUES ($1, $2, $3)
               ON CONFLICT (wallet_address, snapshotted_at) DO UPDATE
                   SET rank_position = EXCLUDED.rank_position""",
            [(row["wallet_address"], idx + 1, today) for idx, row in enumerate(rows)],
        )
    logger.info("Rank snapshot taken — %d players", len(rows))


async def _nightly_snapshot_loop() -> None:
    import datetime as dt
    while True:
        now      = dt.datetime.utcnow()
        tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())
        try:
            await _take_rank_snapshot()
        except Exception as e:
            logger.error("Nightly snapshot failed: %s", e)


@app.get("/api/ranks")
async def get_ranks(
    limit:    int  = Query(default=100, le=500),
    offset:   int  = Query(default=0,   ge=0),
    weekly:   bool = Query(default=False),
    chain_id: int  = Query(default=CELO_CHAIN_ID),
):
    import datetime as dt

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            """SELECT COUNT(*) FROM challenge_player_balances
               WHERE chain_id = $1 AND COALESCE(total_duels, 0) > 0""",
            chain_id,
        )
        if weekly:
            rows = await conn.fetch(
                """SELECT b.wallet_address,
                          COALESCE(b.weekly_wins,  0) AS weekly_wins,
                          COALESCE(b.weekly_duels, 0) AS weekly_duels
                   FROM challenge_player_balances b
                   WHERE b.chain_id = $1
                     AND COALESCE(b.weekly_duels, 0) > 0
                   ORDER BY b.weekly_wins DESC, b.weekly_duels DESC
                   LIMIT $2 OFFSET $3""",
                chain_id, limit, offset,
            )
        else:
            yesterday = dt.date.today() - dt.timedelta(days=1)
            rows = await conn.fetch(
                """SELECT b.wallet_address,
                          COALESCE(b.total_wins,   0)   AS total_wins,
                          COALESCE(b.total_duels,  0)   AS total_duels,
                          COALESCE(b.total_earned, 0.0) AS total_earned,
                          ys.rank_position               AS yesterday_rank
                   FROM challenge_player_balances b
                   LEFT JOIN challenge_rank_snapshots ys
                       ON ys.wallet_address = b.wallet_address
                      AND ys.chain_id       = b.chain_id
                      AND ys.snapshotted_at = $1
                   WHERE b.chain_id = $2
                     AND COALESCE(b.total_duels, 0) > 0
                   ORDER BY b.total_wins DESC, b.total_duels DESC
                   LIMIT $3 OFFSET $4""",
                yesterday, chain_id, limit, offset,
            )

    # ── Batch-fetch usernames / avatars from Supabase ─────────────────────────
    wallets = [r["wallet_address"] for r in rows]
    profiles_map: dict[str, dict] = {}
    if wallets:
        profiles_res = (
            supabase.table("user_profiles")
            .select("wallet_address, username, avatar_url")
            .in_("wallet_address", wallets)
            .execute()
        )
        profiles_map = {p["wallet_address"]: p for p in (profiles_res.data or [])}

    def _profile(wallet: str) -> tuple[str, str]:
        p = profiles_map.get(wallet, {})
        username   = p.get("username") or f"User{wallet[-4:].upper()}"
        avatar_url = p.get("avatar_url") or ""
        return username, avatar_url

    # Frontend (RanksPage) expects snake_case keys under "players":
    #   wallet_address, username, avatar_url, total_wins, total_duels,
    #   total_earned, rank_delta (signed int: positive = moved up since yesterday)
    players_out = []
    for i, r in enumerate(rows):
        username, avatar_url = _profile(r["wallet_address"])
        today_rank = offset + i + 1

        if weekly:
            total_wins   = r["weekly_wins"]
            total_duels  = r["weekly_duels"]
            total_earned = 0.0
            rank_delta   = 0
        else:
            total_wins   = r["total_wins"]
            total_duels  = r["total_duels"]
            total_earned = float(r["total_earned"])
            yesterday_rank = r["yesterday_rank"]
            rank_delta   = (yesterday_rank - today_rank) if yesterday_rank is not None else 0

        players_out.append({
            "wallet_address": r["wallet_address"],
            "username":       username,
            "avatar_url":     avatar_url,
            "total_wins":     total_wins,
            "total_duels":    total_duels,
            "total_earned":   total_earned,
            "rank_delta":     rank_delta,
        })

    return {
        "success":  True,
        "chainId":  chain_id,
        "weekly":   weekly,
        "total":    total,
        "limit":    limit,
        "offset":   offset,
        "players":  players_out,
    }


@app.get("/api/ranks/weekly-history")
async def get_weekly_leaderboard_history(
    limit:    int = Query(default=10, le=50),
    chain_id: int = Query(default=CELO_CHAIN_ID),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT wallet_address, rank_position, snapshotted_at
               FROM challenge_weekly_leaderboard
               WHERE chain_id = $1
               ORDER BY snapshotted_at DESC, rank_position ASC
               LIMIT $2""",
            chain_id, limit,
        )
    return {"success": True, "history": [dict(r) for r in rows]}


STATS_MIN_REFRESH_SECS = 30  # anyone can force-refresh, but at most once per chain per 30s


@app.get("/api/stats/onchain")
async def onchain_stats(
    chain_id: int = Query(default=CELO_CHAIN_ID, description="42220=Celo, 677=Botchain"),
    force:    bool = Query(default=False, description="Bypass the 10-min cache and re-scan now. Public — rate-limited to once per 30s per chain."),
):
    """
    Public on-chain activity stats for one chain.
 
    Reads QuizHub and DROPS token event logs from the chain's RPC and
    returns registered user counts, duel stats, DROPS claim/burn activity.
 
    Results are cached for 10 minutes to avoid hammering the RPC on every
    page load. Pass force=true (e.g. from a "Refresh" button) to re-scan
    immediately — this is public (no admin secret) but rate-limited to once
    per 30s per chain so it can't be used to hammer the RPC. This is a
    separate, lighter-weight scan from the /api/admin/sync-onchain-users
    background job (which still runs on its own 2-hour schedule and writes
    new wallets to Supabase) — refreshing here never touches that scheduler.
    """
    now    = _time.time()
    cached = _stats_cache.get(chain_id)
    if cached:
        age = now - cached["fetched_at"]
        if not force and age < STATS_CACHE_TTL_SECS:
            return cached["result"]
        if force and age < STATS_MIN_REFRESH_SECS:
            return {
                **cached["result"],
                "rateLimited":    True,
                "retryAfterSecs": round(STATS_MIN_REFRESH_SECS - age, 1),
            }
 
    specs  = _build_chain_specs()
    spec   = next((s for s in specs if s.chain_id == chain_id), None)
    if not spec:
        raise HTTPException(status_code=400, detail=f"chain_id {chain_id} is not configured or not enabled")
 
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from collections import defaultdict
 
        loop = asyncio.get_event_loop()
        w3   = await loop.run_in_executor(None, lambda: _make_w3_for_stats(spec))
        latest = await loop.run_in_executor(None, lambda: w3.eth.block_number)
 
        BLOCKS_PER_DAY   = 86_400    # Celo/Botchain ~1 block/sec
        BLOCKS_PER_MONTH = 30 * BLOCKS_PER_DAY
 
        month_cutoff = max(0, latest - BLOCKS_PER_MONTH)
        day_cutoff   = max(0, latest - BLOCKS_PER_DAY)
        from_block   = min(spec.quiz_hub_deploy_block, spec.drops_token_deploy_block)
 
        from onchain_sync import (
            QUIZ_HUB_EVENTS_ABI, DROPS_TOKEN_EVENTS_ABI, _fetch_logs_chunked,
        )
 
        hub_contract    = w3.eth.contract(
            address=Web3.to_checksum_address(spec.quiz_hub_addr),    abi=QUIZ_HUB_EVENTS_ABI
        )
        drops_contract  = w3.eth.contract(
            address=Web3.to_checksum_address(spec.drops_token_addr), abi=DROPS_TOKEN_EVENTS_ABI
        )
 
        infra: set[str] = {a.lower() for a in spec.infra_addrs if a and a.startswith("0x")}
        zero = "0x0000000000000000000000000000000000000000"
 
        # Activity: addr → set of block numbers it appeared in
        activity: dict[str, set[int]] = defaultdict(set)
 
        def record(addr: str, block: int) -> None:
            a = addr.lower() if addr else ""
            if a and a not in infra:
                activity[a].add(block)
 
        quizzes_created = duels_registered = duels_started = 0
        duels_completed = duels_cancelled  = 0
 
        for event_name, handler in [
            ("QuizCreated",    lambda log: (record(log["args"].get("creator",""), log["blockNumber"]),)),
            ("QuizRegistered", lambda log: (
                record(log["args"].get("player1",""), log["blockNumber"]),
                record(log["args"].get("player2",""), log["blockNumber"]),
            )),
            ("WinnerSet",      lambda log: (record(log["args"].get("winner",""), log["blockNumber"]),)),
            ("TieDeclared",    lambda log: (
                record(log["args"].get("player1",""), log["blockNumber"]),
                record(log["args"].get("player2",""), log["blockNumber"]),
            )),
        ]:
            logs = await _fetch_logs_chunked(loop, w3, hub_contract, event_name, from_block, latest)
            for log in logs:
                handler(log)
                if event_name == "QuizCreated":    quizzes_created += 1
                if event_name == "QuizRegistered": duels_registered += 1
                if event_name == "WinnerSet":      duels_completed  += 1
                if event_name == "TieDeclared":    duels_completed  += 1
 
        drops_claims = drops_redemptions = drops_burns = 0
        transfer_logs = await _fetch_logs_chunked(loop, w3, drops_contract, "Transfer", from_block, latest)
        for log in transfer_logs:
            fr = (log["args"].get("from") or "").lower()
            to = (log["args"].get("to")   or "").lower()
            if fr == zero and to and to not in infra:
                drops_claims += 1
                record(to, log["blockNumber"])
            elif to == zero and fr and fr not in infra:
                drops_burns += 1
                record(fr, log["blockNumber"])
 
        redeem_logs = await _fetch_logs_chunked(loop, w3, drops_contract, "PointsRedeemed", from_block, latest)
        for log in redeem_logs:
            user = (log["args"].get("user") or "").lower()
            drops_redemptions += 1
            record(user, log["blockNumber"])
 
        all_time = set(activity.keys())
        mau30d   = {a for a, blocks in activity.items() if any(b >= month_cutoff for b in blocks)}
        dau24h   = {a for a, blocks in activity.items() if any(b >= day_cutoff   for b in blocks)}
 
        result = {
            "success":          True,
            "source":           "onchain",
            "chain_id":         spec.chain_id,
            "chain":            spec.name,
            "contracts": {
                "quizHub":    spec.quiz_hub_addr,
                "dropsToken": spec.drops_token_addr,
            },
            "registeredUsers":  len(all_time),
            "mau30d":           len(mau30d),
            "dau24h":           len(dau24h),
            "quizzesCreated":   quizzes_created,
            "duelsRegistered":  duels_registered,
            "duelsCompleted":   duels_completed,
            "dropsClaims":      drops_claims,
            "dropsRedemptions": drops_redemptions,
            "dropsBurns":       drops_burns,
            "scannedFromBlock": str(from_block),
            "scannedToBlock":   str(latest),
            "updatedAt":        datetime.utcnow().isoformat() + "Z",
            "cachedUntil":      datetime.utcfromtimestamp(now + STATS_CACHE_TTL_SECS).isoformat() + "Z",
        }
 
        _stats_cache[chain_id] = {"result": result, "fetched_at": now}
        return result
 
    except Exception as exc:
        logger.error("onchain_stats scan failed chain=%d: %s", chain_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
 
 
def _make_w3_for_stats(spec) -> "Web3":
    """Thin wrapper so the stats endpoint can build a w3 without importing the full onchain_sync internals."""
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    w3 = Web3(Web3.HTTPProvider(spec.rpc_url, request_kwargs={"timeout": 30}))
    if spec.poa:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3
 

 
@app.post("/api/sync/onchain-users")
@app.post("/api/admin/sync-onchain-users")   # backwards-compat alias
async def sync_onchain_users(
    dry_run:        bool           = Query(False,  description="Scan only — don't write to DB"),
    force_full:     bool           = Query(False,  description="Rescan from deploy blocks. Idempotent (absolute overwrite) — safe to call publicly, but slow."),
    chain:          str            = Query("both", description="celo | botchain | both"),
    x_admin_secret: Optional[str]  = Header(None),
):
    """
    PUBLIC: trigger an on-chain sync. Scans QuizHub + DROPS token events and
    upserts every quiz participant seen on-chain (creators, both duel players,
    winners, tied players, mint/burn/redeem wallets) into:
 
      • user_profiles              — auto-username for wallets not seen before
      • challenge_players          — global duels / wins / ties
      • challenge_player_balances  — per-chain stats the /api/ranks leaderboard reads
 
    No admin secret required. To keep public callers from hammering the RPC/DB,
    each chain can be triggered at most once per 5 minutes (429 otherwise).
    A valid x-admin-secret header bypasses the cooldown only — it is optional.
 
    force_full=true rescans full history in ABSOLUTE mode: leaderboard stat
    columns are overwritten with authoritative on-chain totals (repeat calls
    are idempotent), and pouch balances of existing users are never touched.
    Use this to backfill participants that were missed before the checkpoint.
    """
    global _sync_running
 
    admin_secret = os.getenv("ADMIN_SECRET", "")
    is_admin     = bool(admin_secret) and x_admin_secret == admin_secret
 
    chain_filter: Optional[list[str]] = None
    if chain in ("celo", "botchain"):
        chain_filter = [chain]
    elif chain != "both":
        raise HTTPException(status_code=400, detail="chain must be celo, botchain, or both")
 
    chains_to_check = chain_filter or ["celo", "botchain"]
 
    # Cooldown is a rate limit, not an auth gate — everyone may call.
    if not is_admin:
        now = _time.time()
        blocked = []
        for c in chains_to_check:
            last = _public_sync_last_call.get(c, 0)
            wait = _PUBLIC_SYNC_COOLDOWN_SECS - (now - last)
            if wait > 0:
                blocked.append((c, round(wait, 1)))
        if blocked:
            chain_name, wait_secs = min(blocked, key=lambda b: b[1])
            raise HTTPException(
                status_code=429,
                detail=f"{chain_name} was synced recently — try again in {wait_secs}s",
            )
 
    if _sync_running or _sync_lock.locked():
        raise HTTPException(status_code=409, detail="Sync already in progress — try again shortly.")
 
    async with _sync_lock:
        _sync_running = True
        try:
            if not is_admin:
                now = _time.time()
                for c in chains_to_check:
                    _public_sync_last_call[c] = now
 
            if dry_run:
                specs = _build_chain_specs()
                if chain_filter:
                    specs = [s for s in specs if s.name.lower() in chain_filter]
 
                dry_results = []
                for spec in specs:
                    last       = await _get_last_block(pool, spec.sync_state_key)
                    from_block = (last + 1) if (last and not force_full) else min(
                        spec.quiz_hub_deploy_block, spec.drops_token_deploy_block
                    )
                    stats, latest = await scan_chain_events(spec, from_block)
                    dry_results.append({
                        "chain":                 spec.name,
                        "chain_id":              spec.chain_id,
                        "from_block":            from_block,
                        "to_block":              latest,
                        "wallets_found":         len(stats),
                        "total_duels_in_range":  sum(s.total_duels for s in stats.values()),
                        "total_wins_in_range":   sum(s.total_wins  for s in stats.values()),
                        "total_ties_in_range":   sum(s.total_ties  for s in stats.values()),
                        "drops_minted_in_range": round(sum(s.game_drops_wei / 1e18 for s in stats.values()), 4),
                        "sample_wallets":        list(stats.keys())[:10],
                    })
 
                return {"success": True, "dry_run": True, "chains": dry_results}
 
            result = await _run_onchain_sync(pool, supabase, force_full=force_full, chains=chain_filter)
            # Leaderboard (/api/ranks) reads challenge_player_balances live —
            # new participants and updated wins/duels rank immediately.
            return {"success": True, **result}
 
        except Exception as exc:
            logger.error("Manual onchain_sync failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            _sync_running = False

@app.get("/api/players/{wallet}/balances")
async def get_player_balances(
    wallet:   str,
    chain_id: int | None = Query(default=None),
    chainId:  int | None = Query(default=None),
):
    """Same table + chain default as /api/ranks, so profile == leaderboard."""
    cid = chain_id if chain_id is not None else (chainId or CELO_CHAIN_ID)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(total_wins,   0)   AS total_wins,
                      COALESCE(total_duels,  0)   AS total_duels,
                      COALESCE(total_earned, 0.0) AS total_earned
               FROM challenge_player_balances
               WHERE LOWER(wallet_address) = LOWER($1) AND chain_id = $2""",
            wallet, cid,
        )

    wins  = int(row["total_wins"])  if row else 0
    duels = int(row["total_duels"]) if row else 0

    return {
        "success": True,
        "chainId": cid,
        "balances": {
            "total_wins":   wins,
            "total_duels":  duels,
            "total_earned": float(row["total_earned"]) if row else 0.0,
            "win_rate":     round(wins / duels * 100) if duels else 0,
        },
    }
    
@app.get("/api/drops/balance/{wallet}")
async def get_drops_balance(wallet: str, chainId: int | None = Query(default=None)):
    from drops_service import _ensure_chain_balance_row

    wallet   = wallet.lower()
    chain_id = chainId or CELO_CHAIN_ID
    cfg      = get_chain(chain_id)

    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM challenge_players WHERE wallet_address=$1", wallet
        )
        if not exists:
            await conn.execute(
                """INSERT INTO challenge_players (wallet_address, username)
                   VALUES ($1, $2) ON CONFLICT (wallet_address) DO NOTHING""",
                wallet, f"User{wallet[-4:].upper()}",
            )
        await _ensure_chain_balance_row(conn, wallet, chain_id)

    stats = await _get_player_stats(wallet, chain_id)

    return {
        "success":         True,
        "chainId":         chain_id,
        "chain":           cfg.name,
        "gameDrops":       stats["game_drops"],
        "rewardDrops":     stats["reward_drops"],
        "tier":            stats["tier"],
        "apyPct":          stats["apy_pct"],
        "rematchBadge":    stats["rematch_badge"],
        "totalDuels":      stats["total_duels"],
        "totalWins":       stats["total_wins"],
        "gamesUntilBadge": stats["games_until_badge"],
        "maxStake":        stats["max_stake"],
        "redeemBadge":            stats["redeem_badge"],
        "qualifyingDuels":        stats["qualifying_duels"],
        "gamesUntilRedeemBadge":  stats["games_until_redeem_badge"],
    }
    
@app.get("/api/stats/db")
async def db_stats(
    chain_id: int = Query(default=CELO_CHAIN_ID, description="42220=Celo, 677=Botchain"),
):
    """
    PUBLIC: stats snapshot straight from the database — no on-chain scan.
 
    Sources
    ───────
    challenge_player_balances  → registered users, duels, wins, ties (cumulative;
                                 authoritative after an absolute-mode sync)
    player_activity_log        → MAU/DAU + claim/redeem/burn/quiz-created counts
                                 (approximate: reflects backend-recorded activity,
                                 'register' rows excluded so sync batches don't
                                 spike the active-user numbers)
    sync_state                 → last synced block + last run timestamp
    """
    specs = _build_chain_specs()
    spec  = next((s for s in specs if s.chain_id == chain_id), None)
    if not spec:
        raise HTTPException(status_code=400, detail=f"chain_id {chain_id} is not configured or not enabled")
 
    async with pool.acquire() as conn:
        # ── Cumulative per-chain aggregates from balances ─────────────────────
        bal = await conn.fetchrow(
            """
            SELECT COUNT(*)                          AS users,
                   COALESCE(SUM(total_duels), 0)     AS duels,
                   COALESCE(SUM(total_wins),  0)     AS wins,
                   COALESCE(SUM(total_ties),  0)     AS ties
            FROM challenge_player_balances
            WHERE chain_id = $1
            """,
            chain_id,
        )
 
        # ── Approximate activity counts from the activity log ─────────────────
        # created_at is when the BACKEND recorded the event, so for wallets
        # backfilled by onchain_sync these lag block time — fine for a snapshot
        # that gets replaced by the live scan seconds later.
        act = await conn.fetchrow(
            """
            SELECT
                COUNT(DISTINCT wallet_address) FILTER (
                    WHERE created_at > NOW() - INTERVAL '30 days'
                      AND event_type <> 'register'
                ) AS mau30d,
                COUNT(DISTINCT wallet_address) FILTER (
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                      AND event_type <> 'register'
                ) AS dau24h,
                COUNT(*) FILTER (
                    WHERE event_type IN ('welcome_mint','claim_win','claim_refund','game_reward','buy_drops')
                ) AS drops_claims,
                COUNT(*) FILTER (
                    WHERE event_type IN ('redeem_start','stake_created')
                ) AS drops_redemptions,
                COUNT(*) FILTER (
                    WHERE event_type = 'stake_deducted'
                ) AS drops_burns,
                COUNT(*) FILTER (
                    WHERE event_type = 'game_created'
                ) AS quizzes_created
            FROM player_activity_log
            WHERE chain_id = $1
            """,
            chain_id,
        )
 
        # ── Last sync checkpoint for the "scanned to block / updated" line ────
        sync_row = await conn.fetchrow(
            "SELECT last_block, last_run_at, meta FROM sync_state WHERE key = $1",
            spec.sync_state_key,
        )
 
    duels_registered = int(bal["duels"]) // 2                 # each duel counted on both sides
    duels_completed  = int(bal["wins"]) + int(bal["ties"]) // 2
 
    from_block = "0"
    to_block   = str(sync_row["last_block"]) if sync_row else "0"
    updated_at = (
        sync_row["last_run_at"].isoformat() + "Z"
        if sync_row and sync_row["last_run_at"] else
        datetime.utcnow().isoformat() + "Z"
    )
    try:
        meta = sync_row["meta"] if sync_row else None
        if isinstance(meta, str):
            import json as _json
            meta = _json.loads(meta)
        if meta and meta.get("from_block") is not None:
            from_block = str(meta["from_block"])
    except Exception:
        pass
 
    return {
        "success":          True,
        "source":           "database",
        "approximate":      True,
        "chain_id":         spec.chain_id,
        "chain":            spec.name,
        "contracts": {
            "quizHub":    spec.quiz_hub_addr,
            "dropsToken": spec.drops_token_addr,
        },
        "registeredUsers":  int(bal["users"]),
        "mau30d":           int(act["mau30d"] or 0),
        "dau24h":           int(act["dau24h"] or 0),
        "quizzesCreated":   int(act["quizzes_created"] or 0),
        "duelsRegistered":  duels_registered,
        "duelsCompleted":   duels_completed,
        "dropsClaims":      int(act["drops_claims"] or 0),
        "dropsRedemptions": int(act["drops_redemptions"] or 0),
        "dropsBurns":       int(act["drops_burns"] or 0),
        "scannedFromBlock": from_block,
        "scannedToBlock":   to_block,
        "updatedAt":        updated_at,
    }
    
@app.get("/api/sync/onchain-users/status")
@app.get("/api/admin/sync-onchain-users/status")   # backwards-compat alias
async def sync_status():
    """
    PUBLIC: running flag + per-chain last checkpoint block. Read-only —
    nothing sensitive here, so the old 403 admin gate is removed.
    """
    specs = _build_chain_specs()
    chain_statuses = []
 
    for spec in specs:
        last_block  = await _get_last_block(pool, spec.sync_state_key)
        last_run_at = None
        meta        = None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT last_run_at, meta FROM sync_state WHERE key = $1",
                    spec.sync_state_key,
                )
                if row:
                    last_run_at = row["last_run_at"].isoformat() if row["last_run_at"] else None
                    meta        = row["meta"]
        except Exception:
            pass
 
        chain_statuses.append({
            "chain":       spec.name,
            "chain_id":    spec.chain_id,
            "last_block":  last_block,
            "last_run_at": last_run_at,
            "meta":        meta,
        })
 
    return {
        "running": _sync_running,
        "chains":  chain_statuses,
    }
 
@app.post("/api/ranks/snapshot")
async def snapshot_ranks(chain_id: int = Query(default=CELO_CHAIN_ID)):
    import datetime as dt

    today = dt.date.today()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT wallet_address,
                      ROW_NUMBER() OVER (ORDER BY total_wins DESC, total_duels DESC) AS rank_position
               FROM challenge_player_balances
               WHERE chain_id = $1
                 AND COALESCE(total_duels, 0) > 0""",
            chain_id,
        )
        # Upsert so re-running is safe
        await conn.executemany(
            """INSERT INTO challenge_rank_snapshots
                   (wallet_address, chain_id, rank_position, snapshotted_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (wallet_address, chain_id, snapshotted_at)
               DO UPDATE SET rank_position = EXCLUDED.rank_position""",
            [(r["wallet_address"], chain_id, r["rank_position"], today) for r in rows],
        )
    return {"success": True, "snapshotted": len(rows), "chain_id": chain_id, "date": str(today)}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error(
        "422 validation error  path=%s  errors=%s  body=%s",
        request.url.path, exc.errors(), body.decode("utf-8", errors="replace"),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

@app.post("/api/challenge/single/reserve")
async def reserve_single_challenge(body: dict):
    """Returns a pre-generated code so frontend can call createQuiz(keccak256(code)) on-chain."""
    creator  = body.get("creatorAddress", "").lower()
    chain_id = body.get("chainId", CELO_CHAIN_ID)
    if not creator:
        raise HTTPException(status_code=400, detail="creatorAddress required")
    if not is_supported(chain_id):
        raise HTTPException(
            status_code=400,
            detail=f"Single-player is not available on chain {chain_id}. "
                   f"Supported chains: {[c for c, cfg in CHAINS.items() if cfg.enabled]}",
        )
    code = make_code()
    # Store temporarily so create endpoint can validate it
    # Simple in-memory set; expires on next restart (acceptable)
    _reserved_codes.add(code)
    return {"success": True, "code": code, "chainId": chain_id}

_reserved_codes: set[str] = set()

@app.post("/api/challenge/single/create")
async def create_single_challenge(body: CreateSingleChallengeRequest):
    reserved_code = body.reservedCode if hasattr(body, 'reservedCode') else None
    code = reserved_code if reserved_code and reserved_code in _reserved_codes else make_code()
    if reserved_code:
       _reserved_codes.discard(reserved_code)
    if body.difficulty not in DIFFICULTY_LEVELS:
        raise HTTPException(status_code=400, detail=f"difficulty must be one of {DIFFICULTY_LEVELS}")
    if not body.chainId:
        raise HTTPException(status_code=400, detail="chainId is required")
    # Solo mode works on any supported chain (Celo, Botchain, ...). Without
    # this check get_chain() would silently FALL BACK to Celo mid-flow for an
    # unknown/disabled chainId — the creator's stake and the bot's stake would
    # land on different chains.
    if not is_supported(body.chainId):
        raise HTTPException(
            status_code=400,
            detail=f"Single-player is not available on chain {body.chainId}. "
                   f"Supported chains: {[c for c, cfg in CHAINS.items() if cfg.enabled]}",
        )
    if not body.createTxHash:
        raise HTTPException(status_code=400, detail="createTxHash required — creator must call createQuiz() first")

    creator    = body.creatorAddress.lower()
    chain_id   = body.chainId
    bot_prof   = get_bot_profile(body.difficulty)
    stake      = bot_prof["stake"]
    bot_wallet = bot_prof["wallet"].lower()
    bot_pk     = bot_prof.get("private_key", "")

    if not bot_pk:
        raise HTTPException(status_code=500, detail=f"Bot PK not configured for difficulty {body.difficulty}")

    try:
        await check_game_drops_balance(pool, creator, stake, chain_id=chain_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    question_count = bot_prof["question_count"]
    questions      = await generate_questions(body.topic, question_count)

    async with pool.acquire() as conn:
        challenge_id = await conn.fetchval(
            """
            INSERT INTO challenge_games
                (code, creator_address, topic, stake_amount, token_symbol, chain_id,
                 status, is_public, rounds_data, is_single_player, bot_difficulty)
            VALUES ($1,$2,$3,$4,'DROPS',$5,'waiting',FALSE,$6,TRUE,$7)
            RETURNING id
            """,
            code, creator, body.topic, stake, chain_id,
            json.dumps(questions), body.difficulty,
        )
        await conn.execute(
            """INSERT INTO challenge_players (wallet_address, username)
               VALUES ($1,$2) ON CONFLICT (wallet_address) DO UPDATE SET username=EXCLUDED.username""",
            creator, body.creatorUsername,
        )
        await conn.execute(
            """INSERT INTO challenge_game_players (challenge_id, wallet_address, username, tx_hash)
               VALUES ($1,$2,$3,$4)""",
            challenge_id, creator, body.creatorUsername, body.createTxHash,
        )
        await conn.execute(
            """INSERT INTO challenge_players (wallet_address, username)
               VALUES ($1,$2) ON CONFLICT (wallet_address) DO NOTHING""",
            bot_wallet, bot_prof["username"],
        )
        await conn.execute(
            """INSERT INTO challenge_game_players (challenge_id, wallet_address, username)
               VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
            challenge_id, bot_wallet, bot_prof["username"],
        )

    # After the DB inserts, before building challenges[code]
    creator_profile = await _get_live_profile(creator)

    challenges[code] = {
        "id":               str(challenge_id),
        "code":             code,
        "topic":            body.topic,
        "creator":          creator,
        "creatorName":      creator_profile["username"],      # ← from profile
        "creatorAvatar":    creator_profile["avatar_url"],    # ← add this
        "stakeAmount":      stake,
        "agreedStake":      stake,
        "token":            "DROPS",
        "chainId":          chain_id,
        "status":           "waiting",
        "isPublic":         False,
        "is_single_player": True,
        "bot_difficulty":   body.difficulty,
        "bot_profile":      bot_prof,
        "inviteWallet":     None,
        "rounds":           questions["rounds"],
        "players": {
            creator: {
                "username":   creator_profile["username"],    # ← from profile
                "avatar_url": creator_profile["avatar_url"],  # ← add this
                "points":     0,
                "ready":      False,
                "txVerified": False,
            },
            bot_wallet: {
                "username":   bot_prof["username"],
                "avatar_url": bot_prof["avatar_url"],
                "points":     0,
                "ready":      False,
                "txVerified": False,
                "isBot":      True,
            },
        },
        "player_offers":     {},
        "onChainConfirmed":  True,
        "single_player_cfg": {
            "bot_wallet":     bot_wallet,
            "creator_wallet": creator,
            "difficulty":     body.difficulty,
        },
    }
    game_state[code] = {"answers": {}, "currentQuestion": None}

    # ── Bot stakes immediately in background ──────────────────────────────────
    async def _bot_stake_task():
        try:
            from sp_engine import bot_stake_onchain
            txs = await bot_stake_onchain(
                bot_private_key = bot_pk,
                bot_wallet      = bot_wallet,
                creator_wallet  = creator,
                code            = code,
                stake           = stake,
                chain_id        = chain_id,
            )

            if code in challenges:
                challenges[code]["players"][bot_wallet]["txVerified"] = True
                challenges[code]["players"][bot_wallet]["ready"]      = True
                challenges[code]["players"][bot_wallet]["txHash"]     = txs["bot_redeem_tx"]

            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE challenge_game_players
                       SET tx_verified=TRUE, ready=TRUE, tx_hash=$1
                       WHERE challenge_id=(SELECT id FROM challenge_games WHERE code=$2)
                         AND wallet_address=$3""",
                    txs["bot_redeem_tx"], code, bot_wallet,
                )

            await broadcast(code, {
                "type":   "player_joined",
                "player": {
                    "walletAddress": bot_wallet,
                    "username":      bot_prof["username"],
                    "avatar_url":    bot_prof["avatar_url"],
                    "isBot":         True,
                },
            })
            await broadcast(code, {"type": "stake_verified", "wallet": bot_wallet})
            await broadcast(code, {"type": "player_ready",   "wallet": bot_wallet})

            logger.info("Bot staked on-chain  code=%s  txs=%s", code, txs)
            await _maybe_auto_start(code)

        except Exception as e:
            logger.error("Bot on-chain stake FAILED  code=%s  error=%s", code, e)
            await broadcast(code, {
                "type":    "error",
                "message": "Bot failed to stake. Please try again.",
                "code":    code,
            })

    asyncio.create_task(_bot_stake_task())

    return {
        "success":    True,
        "code":       code,
        "token":      "DROPS",
        "stake":      stake,
        "difficulty": body.difficulty,
        "bot": {
            "wallet":   bot_wallet,
            "username": bot_prof["username"],
        },
    }
    
    
# ══════════════════════════════════════════════════════════════════════════════
#  NEW ENDPOINT 2: POST /api/challenge/{code}/single/bot-join
# ══════════════════════════════════════════════════════════════════════════════
 
@app.post("/api/challenge/{code}/single/bot-join")
async def single_bot_join(code: str, body: dict):
    """
    Called by the frontend after the creator's stake (confirm-burn) confirms.
 
    Actions performed:
    1. Verifies the creator is txVerified.
    2. Deducts the creator's game_drops (if confirm-burn hasn't done it yet).
    3. Marks the bot as txVerified + ready (simulated, no real tx).
    4. Marks the creator as ready.
    5. Persists both player rows in the DB.
    6. Calls _maybe_auto_start → triggers the game loop.
 
    The frontend should call this endpoint once, immediately after receiving
    the "stake_verified" broadcast for the creator's wallet.
    """
    code           = code.upper()
    creator_wallet = body.get("walletAddress", "").lower()
 
    if not creator_wallet:
        raise HTTPException(status_code=400, detail="walletAddress required")
 
    if code not in challenges:
        raise HTTPException(status_code=404, detail="Single-player challenge not found in memory")
 
    challenge = challenges[code]
 
    if not challenge.get("is_single_player"):
        raise HTTPException(status_code=400, detail="Not a single-player challenge")
 
    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Not the creator of this challenge")
 
    if challenge["status"] != "waiting":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start — challenge status is '{challenge['status']}'"
        )
 
    # Creator must have confirmed their stake first
    if not challenge["players"][creator_wallet].get("txVerified"):
        raise HTTPException(
            status_code=400,
            detail="Creator stake not yet verified. Complete confirm-burn first.",
        )
 
    bot_wallet = challenge["single_player_cfg"]["bot_wallet"]
    difficulty = challenge["single_player_cfg"]["difficulty"]
    stake      = float(challenge["stakeAmount"])
    chain_id   = challenge.get("chainId") or CELO_CHAIN_ID
 
    # ── Simulate bot stake (DB activity record only, no balance touch) ────────
    asyncio.create_task(
        record_bot_stake(pool, bot_wallet, stake, code, chain_id, difficulty)
    )
 
    # ── Mark bot as verified + ready ──────────────────────────────────────────
    challenge["players"][bot_wallet]["txVerified"] = True
    challenge["players"][bot_wallet]["ready"]      = True
 
    # ── Mark creator as ready ─────────────────────────────────────────────────
    challenge["players"][creator_wallet]["ready"] = True
 
    # ── Persist player states to DB ───────────────────────────────────────────
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_game_players
               SET tx_verified=TRUE, ready=TRUE
               WHERE challenge_id=(SELECT id FROM challenge_games WHERE code=$1)
                 AND wallet_address=$2""",
            code, bot_wallet,
        )
        await conn.execute(
            """UPDATE challenge_game_players
               SET ready=TRUE
               WHERE challenge_id=(SELECT id FROM challenge_games WHERE code=$1)
                 AND wallet_address=$2""",
            code, creator_wallet,
        )
 
    await broadcast(code, {
        "type":   "player_joined",
        "player": {
            "walletAddress": bot_wallet,
            "username":      challenge["players"][bot_wallet]["username"],
            "avatar_url":    challenge["players"][bot_wallet].get("avatar_url", ""),
            "isBot":         True,
        },
    })
    await broadcast(code, {"type": "stake_verified", "wallet": bot_wallet})
    await broadcast(code, {"type": "player_ready",   "wallet": bot_wallet})
    await broadcast(code, {"type": "player_ready",   "wallet": creator_wallet})
 
    # ── Trigger auto-start (both players are now verified + ready) ────────────
    await _maybe_auto_start(code)
 
    return {
        "success":    True,
        "gameStarting": True,
        "bot": {
            "wallet":   bot_wallet,
            "username": challenge["players"][bot_wallet]["username"],
        },
    }
 
 
# ══════════════════════════════════════════════════════════════════════════════
#  NEW ENDPOINT 3: GET /api/challenge/single/bots
# ══════════════════════════════════════════════════════════════════════════════
 
@app.get("/api/challenge/single/bots")
async def list_bot_opponents():
    """
    Returns the full difficulty table so the frontend can render
    the bot-selection UI without hardcoding stakes or names.
    """
    return {
        "success": True,
        "bots": [
            {
                "difficulty":    d,
                "username":      p["username"],
                "avatar_url":    p["avatar_url"],
                "stake":         p["stake"],
                "targetWinRate": p["target_win_rate"],
            }
            for d, p in sorted(BOT_PROFILES.items())
        ],
    }

# ─── Notification Endpoints ───────────────────────────────────────────────────

@app.get("/api/notifications/{wallet}")
async def get_notifications(wallet: str, limit: int = Query(default=20, le=50)):
    items = await notif.get_unread(wallet, limit)
    return {"success": True, "notifications": items}


@app.get("/api/notifications/{wallet}/count")
async def get_unread_count(wallet: str):
    count = await notif.unread_count(wallet)
    return {"success": True, "unread": count}


@app.post("/api/notifications/{wallet}/read/{notif_id}")
async def mark_read(wallet: str, notif_id: str):
    await notif.mark_read(wallet, notif_id)
    return {"success": True}


@app.post("/api/notifications/{wallet}/read-all")
async def mark_all_read(wallet: str):
    await notif.mark_all_read(wallet)
    return {"success": True}


# ─── Direct messages ──────────────────────────────────────────────────────────


async def _enrich_peers(items: list[dict], key: str = "peer") -> list[dict]:
    wallets = list({i[key] for i in items})
    pmap: dict[str, dict] = {}
    if wallets:
        res = (
            supabase.table("user_profiles")
            .select("wallet_address, username, avatar_url")
            .in_("wallet_address", wallets)
            .execute()
        )
        pmap = {p["wallet_address"]: p for p in (res.data or [])}
    for i in items:
        w = i[key]
        p = pmap.get(w, {})
        i["username"]   = p.get("username") or f"User{w[-4:].upper()}"
        i["avatar_url"] = p.get("avatar_url") or ""
        i["online"]     = _is_online(w)
    return items


@app.get("/api/dm/threads/{wallet}")
async def dm_threads(wallet: str):
    items = await dm_service.threads(pool, wallet)
    return {"success": True, "threads": await _enrich_peers(items)}


@app.get("/api/dm/unread/{wallet}")
async def dm_unread(wallet: str):
    return {"success": True, "count": await dm_service.unread_total(pool, wallet)}


@app.post("/api/dm/send")
async def dm_send(body: DMSendRequest):
    try:
        msg = await _dm_send(body.frm, body.to, body.body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "message": msg}


@app.get("/api/dm/{wallet}/{peer}")
async def dm_history(
    wallet: str,
    peer:   str,
    limit:  int = Query(default=50, le=100),
    before: int | None = Query(default=None),
):
    msgs    = await dm_service.history(pool, wallet, peer, limit, before)
    profile = await _get_live_profile(peer)
    return {
        "success":  True,
        "messages": msgs,
        "peer": {
            "wallet":     peer.lower(),
            "username":   profile["username"],
            "avatar_url": profile["avatar_url"],
            "online":     _is_online(peer),
        },
    }


@app.post("/api/dm/{wallet}/{peer}/read")
async def dm_mark_read(wallet: str, peer: str):
    await dm_service.mark_read(pool, wallet, peer)
    # Tell the other side their messages were seen.
    try:
        await notif._push(peer.lower(), {"type": "dm_read", "reader": wallet.lower()})
    except Exception:
        pass
    return {"success": True}
# ─── Employee Endpoints ───────────────────────────────────────────────────────

@app.post("/register", response_model=Employee)
async def register_employee(
    name:  str        = Form(...),
    role:  str        = Form(...),
    photo: UploadFile = File(None),
):
    emp_id      = generate_unique_id()
    issue_date  = datetime.now()
    expiry_date = issue_date + timedelta(days=365 * 7)
    photo_url   = None

    if photo and photo.filename:
        try:
            file_ext = photo.filename.split(".")[-1].lower()
            allowed  = {"jpg", "jpeg", "png", "webp", "gif"}
            if file_ext not in allowed:
                raise HTTPException(status_code=400, detail="Invalid image format.")
            file_bytes   = await photo.read()
            storage_path = f"{emp_id}/{uuid.uuid4()}.{file_ext}"
            supabase.storage.from_(STORAGE_BUCKET).upload(
                storage_path, file_bytes,
                {"content-type": photo.content_type or f"image/{file_ext}"},
            )
            photo_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Photo upload failed: %s", e)

    employee_data = {
        "id":          emp_id,
        "name":        name,
        "role":        role,
        "issue_date":  issue_date.strftime("%d/%m/%Y"),
        "expiry_date": expiry_date.strftime("%d/%m/%Y"),
        "photo_url":   photo_url,
    }
    try:
        response = supabase.table("challenge_employees").insert(employee_data).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.post("/api/challenge/{code}/cancel")
async def cancel_challenge(code: str, body: dict):
    code           = code.upper()
    creator_wallet = body.get("creatorWallet", "").lower()
    reason         = body.get("reason", "cancelled")

    challenge = challenges.get(code)

    if not challenge:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT creator_address, status FROM challenge_games WHERE code=$1", code
            )
        if not row:
            return {"success": True}
        if row["creator_address"].lower() != creator_wallet:
            raise HTTPException(status_code=403, detail="Not the creator")
        if row["status"] not in ("pending",):
            return {"success": True, "message": "Not cancellable"}
    else:
        if challenge["creator"].lower() != creator_wallet:
            raise HTTPException(status_code=403, detail="Not the creator")
        if challenge["status"] != "pending":
            return {"success": True, "message": "Not cancellable"}
        challenges.pop(code, None)
        game_state.pop(code, None)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenge_games SET status='cancelled', cancel_reason=$1 WHERE code=$2",
            reason, code,
        )

    logger.info("Challenge cancelled  code=%s  reason=%s", code, reason)
    return {"success": True}

@app.post("/verify")
def verify_employee(qr_data: VerifyRequest):
    try:
        response = supabase.table("challenge_employees").select("*").eq("id", qr_data.id).execute()
        if not response.data:
            return {"valid": False, "employee": None}
        emp_record = response.data[0]
        is_valid = (emp_record["name"] == qr_data.name and emp_record["role"] == qr_data.role)
        return {"valid": is_valid, "employee": emp_record if is_valid else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

# ─── WebSocket: Game ──────────────────────────────────────────────────────────


@app.websocket("/ws/challenge/{code}")
async def challenge_socket(ws: WebSocket, code: str):
    import time as _time
 
    code = code.upper()
    await ws.accept()
    connections.setdefault(code, []).append(ws)
    connected_wallet = None
 
    if code in challenges:
        await ws.send_json({
            "type":      "state_sync",
            "challenge": _safe_challenge(challenges[code]),
        })
 
        # FIX 5: snapshot existing pre-lobby offers to the newly connected client
        # so they don't need to wait for the next offer broadcast to see who's waiting.
        challenge = challenges[code]
        if challenge.get("status") == "waiting" and challenge.get("player_offers"):
            snapshot_offers = []
            for wallet, amount in challenge["player_offers"].items():
                player_info = challenge["players"].get(wallet, {})
                snapshot_offers.append({
                    "wallet":   wallet,
                    "username": player_info.get("username", wallet[:8]),
                    "amount":   amount,
                    "sentAt":   challenge.get(f"offer_time_{wallet}",
                                              __import__("datetime").datetime.utcnow().isoformat()),
                })
            # Sort descending by amount to match frontend sort order
            snapshot_offers.sort(key=lambda o: o["amount"], reverse=True)
            await ws.send_json({
                "type":   "pre_lobby_offers_snapshot",
                "offers": snapshot_offers,
            })
 
    try:
        async for msg in ws.iter_json():
            m_type = msg.get("type")
            wallet = msg.get("walletAddress", "").lower()
 
            if wallet and not connected_wallet:
                connected_wallet = wallet
 
            if wallet and wallet in (challenges.get(code, {}).get("players") or {}):
                if game_state.get(code):
                    game_state[code].pop(f"disconnected_{wallet}", None)
                    game_state[code][f"reconnected_{wallet}"] = True
 
            if m_type == "rejoin":
                if code in challenges and code in game_state:
                    challenge = challenges[code]
                    gs        = game_state[code]
                    if challenge.get("status") == "active":
                        current_q = gs.get("currentQuestion")
                        if current_q:
                            elapsed_ms = int(_time.time() * 1000) - current_q["startedAt"]
                            time_limit = current_q["data"]["timeLimit"]
                            time_left  = max(0, time_limit - elapsed_ms / 1000)
                            if time_left > 0:
                                await ws.send_json({
                                    "type":           "question",
                                    "roundIndex":     current_q["roundIndex"],
                                    "questionIndex":  current_q["questionIndex"],
                                    "totalQuestions": current_q["totalQuestions"],
                                    "round":          current_q["roundName"],
                                    "startedAt":      current_q["startedAt"],
                                    "data": {
                                        "question":  current_q["data"]["question"],
                                        "options":   current_q["data"]["options"],
                                        "timeLimit": time_limit,
                                    },
                                })
                            else:
                                await ws.send_json({
                                    "type":      "state_sync",
                                    "challenge": _safe_challenge(challenge),
                                    "totalScores": {
                                        w: p["points"]
                                        for w, p in challenge["players"].items()
                                    },
                                })
                        else:
                            await ws.send_json({
                                "type":      "state_sync",
                                "challenge": _safe_challenge(challenge),
                                "totalScores": {
                                    w: p["points"]
                                    for w, p in challenge["players"].items()
                                },
                            })
 
            elif m_type == "stake_confirmed":
                await _handle_stake_confirmed(code, wallet, msg.get("txHash", ""))
            elif m_type == "ready":
                await _handle_ready(code, wallet)
            elif m_type == "submit_answer":
                _handle_submit_answer(code, wallet, msg)
            elif m_type == "chat":
                await broadcast(code, {
                    "type":      "chat",
                    "sender":    msg.get("username"),
                    "wallet":    wallet,
                    "text":      msg.get("text"),
                    "timestamp": asyncio.get_event_loop().time(),
                })
 
    except WebSocketDisconnect:
        sockets = connections.get(code, [])
        if ws in sockets:
            sockets.remove(ws)
 
        if connected_wallet and code in challenges:
            challenge = challenges[code]
            player    = challenge["players"].get(connected_wallet, {})
            username  = player.get("username") or f"User{connected_wallet[-4:].upper()}"
 
            await broadcast(code, {
                "type":         "player_left",
                "wallet":       connected_wallet,
                "username":     username,
                "message":      f"{username} has left the game.",
                "isActiveGame": challenge.get("status") == "active",
            })
 
            if challenge.get("status") == "active":
                asyncio.create_task(
                    _disconnect_grace(code, connected_wallet, username)
                )
 
 
# ─── Also update submit_offer to record the offer timestamp ──────────────────
# This gives the snapshot something to read for sentAt.
# Replace the existing submit_offer endpoint body with this version:
 
@app.post("/api/challenge/{code}/offer")
async def submit_offer(code: str, body: StakeOfferRequest):
    code   = code.upper()
    wallet = body.walletAddress.lower()
    amount = round(float(body.amount), 6)
 
    if amount < MIN_STAKE_DROPS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum stake is {MIN_STAKE_DROPS} DROPS. Offered {amount} DROPS."
        )
 
    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")
 
    challenge = challenges[code]
 
    if "player_offers" not in challenge:
        challenge["player_offers"] = {}
 
    challenge["player_offers"][wallet] = amount
 
    # FIX 5 (companion): store the offer timestamp so the snapshot can echo it
    sent_at = datetime.utcnow().isoformat()
    challenge[f"offer_time_{wallet}"] = sent_at
 
    username = (
        challenge["players"].get(wallet, {}).get("username")
        or getattr(body, "username", None)
        or "Anon"
    )
 
    await broadcast(code, {
        "type":      "pre_lobby_offer",
        "wallet":    wallet,
        "amount":    amount,
        "username":  username,
        "sentAt":    sent_at,
        "isCreator": wallet == challenge["creator"].lower(),
    })
 
    return {"success": True, "amount": amount}
 
# ─── Disconnect Grace ─────────────────────────────────────────────────────────

GRACE_SECONDS = 60

async def _disconnect_grace(code: str, wallet: str, username: str) -> None:
    """
    Grace period for disconnected players.
 
    For single-player games: the bot never reconnects, so if the creator
    disconnects the game is simply forfeited — no on-chain winner call and
    no pending claim minted to the bot address.
    """
    if game_state.get(code):
        game_state[code][f"disconnected_{wallet}"] = True
 
    for elapsed in range(GRACE_SECONDS):
        await asyncio.sleep(1)
 
        if game_state.get(code, {}).get(f"reconnected_{wallet}"):
            game_state[code].pop(f"reconnected_{wallet}", None)
            game_state[code].pop(f"disconnected_{wallet}", None)
            await broadcast(code, {"type": "player_rejoined", "wallet": wallet, "username": username})
            return
 
        if code not in challenges or challenges[code].get("status") != "active":
            return
 
        seconds_left = GRACE_SECONDS - elapsed - 1
        if seconds_left % 10 == 0 or seconds_left <= 5:
            await broadcast(code, {
                "type":        "reconnect_countdown",
                "wallet":      wallet,
                "username":    username,
                "secondsLeft": seconds_left,
            })
 
    if code not in challenges or challenges[code].get("status") != "active":
        return
 
    challenge = challenges[code]
    players   = challenge["players"]
    winner    = next((w for w in players if w.lower() != wallet.lower()), None)
    if not winner:
        return
 
    challenge["status"] = "finished"
    challenge["winner"] = winner
    await _mark_finished(pool, code, winner)
 
    stake = float(challenge.get("stakeAmount", challenge.get("stake", 0)))
 
    # ── Single-player forfeit: no on-chain call, no pending claim for bot ──
    is_sp = challenge.get("is_single_player", False)
    if is_sp:
        # Creator disconnected (forfeited) — bot "wins" but we don't mint anything.
        # The creator's already-burned stake is gone; no further action needed.
        logger.info("SP forfeit: creator=%s disconnected  code=%s", wallet, code)
    else:
        asyncio.create_task(_set_winner_on_chain(code, winner, challenge.get("chainId")))
        await notif.notify_game_over(code, winner, wallet, stake, "DROPS")
 
    await broadcast(code, {
        "type":        "game_over",
        "outcome":     "winner",
        "winner":      winner,
        "reason":      "forfeit",
        "forfeitedBy": wallet,
        "singlePlayer": is_sp,
        "finalScores": {
            w: {"username": p["username"], "points": p["points"]}
            for w, p in players.items()
        },
        "canRematch": not is_sp,
    })
    logger.info("Forfeit: %s disconnected, %s wins  code=%s", wallet, winner, code)
    
    
# ─── WS Helpers ───────────────────────────────────────────────────────────────

async def _handle_stake_confirmed(code: str, wallet: str, tx_hash: str) -> None:
    if code not in challenges:
        return

    challenge = challenges[code]

    if wallet not in challenge["players"]:
        return

    if challenge["players"][wallet].get("txVerified"):
        await broadcast(code, {"type": "stake_verified", "wallet": wallet})
        return

    FAKE_HASHES = {"auto-sync", "sync-recovery", "pre-lobby-agreed", "", None}
    if tx_hash in FAKE_HASHES:
        logger.info("Ignoring fake tx_hash '%s' for %s — use /sync-stake", tx_hash, wallet)
        return

    logger.info(
        "stake_confirmed WS received for %s code=%s — full verify via /confirm-burn",
        wallet, code
    )


async def _handle_ready(code: str, wallet: str) -> None:
    if code not in challenges or wallet not in challenges[code]["players"]:
        return
    challenge = challenges[code]
    if not challenge["players"][wallet].get("txVerified"):
        return
    challenge["players"][wallet]["ready"] = True
    await broadcast(code, {"type": "player_ready", "wallet": wallet})
    await _maybe_auto_start(code)


def _handle_submit_answer(code: str, wallet: str, msg: dict) -> None:
    if code not in game_state:
        return
    r_idx = msg.get("roundIndex")
    q_idx = msg.get("questionIndex")
    key   = f"{r_idx}_{q_idx}"
    state = game_state[code]

    if key not in state["answers"]:
        state["answers"][key] = {}

    state["answers"][key][wallet] = {
        "answerId":  msg.get("answerId"),
        "timeTaken": msg.get("timeTaken", 0),
    }

async def _run_game_loop_safe(code: str) -> None:
    """
    Wrapper around run_game_loop.  Forwards single_player_cfg when the
    challenge was created in single-player mode.
    """
    try:
        sp_cfg = challenges.get(code, {}).get("single_player_cfg")   # None for PvP
        await run_game_loop(
            code, challenges, game_state, pool, broadcast, notif,
            single_player_cfg=sp_cfg,
        )
    except Exception as e:
        logger.error("run_game_loop CRASHED  code=%s  error=%s", code, e, exc_info=True)
        await broadcast(code, {"type": "error", "message": "Game loop crashed. Please refresh."})
 

async def _maybe_auto_start(code: str) -> None:
    if code not in challenges:
        return
    c = challenges[code]
    if (
        len(c["players"]) == 2
        and all(p.get("txVerified") for p in c["players"].values())
        and all(p.get("ready")      for p in c["players"].values())
        and c["status"] == "waiting"
    ):
        
        logger.info(
            "auto-start triggered  code=%s  players=%s",
            code, list(c["players"].keys()),
        )
        wallets = list(c["players"].keys())
        asyncio.create_task(notif.notify_game_starting(code, wallets))
        asyncio.create_task(
            _run_game_loop_safe(code)  # wrapped version below
        )


# ─── WebSocket: Notifications ─────────────────────────────────────────────────

@app.websocket("/ws/notify/{wallet}")
async def notify_socket(ws: WebSocket, wallet: str):
    wallet = wallet.lower()
    await ws.accept()
    notify_conn.setdefault(wallet, []).append(ws)

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE challenge_players SET last_seen_at=NOW() WHERE wallet_address=$1", wallet,
            )
    except Exception as e:
        logger.warning("Could not update last_seen_at for %s: %s", wallet, e)

    try:
        count = await notif.unread_count(wallet)
        await ws.send_json({"type": "unread_count", "count": count})
    except Exception:
        sockets = notify_conn.get(wallet, [])
        if ws in sockets:
            sockets.remove(ws)
        return

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets = notify_conn.get(wallet, [])
        if ws in sockets:
            sockets.remove(ws)
        if not sockets:
            notify_conn.pop(wallet, None)

# ─── Presence ─────────────────────────────────────────────────────────────────
presence_wallets: Dict[str, dict] = {}   # wallet -> {"username": ..., "avatar_url": ...}
presence_sockets: list[WebSocket] = []

async def _broadcast_presence() -> None:
    online = await _online_list()
    dead = []
    for ws in presence_sockets:
        try:
            await ws.send_json({"type": "presence", "online": online})
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in presence_sockets:
            presence_sockets.remove(ws)

async def broadcast_global(payload: dict) -> None:
    """Fan a payload out to every connected presence socket — i.e. everyone
    currently in the app, wallet-connected or not. Used for lightweight
    app-wide toasts that don't need DB persistence."""
    dead = []
    for ws in presence_sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in presence_sockets:
            presence_sockets.remove(ws)


async def broadcast_public_quiz(
    code: str, topic: str, creator: str, creator_name: str,
    stake: float, token: str,
) -> None:
    await broadcast_global({
        "type":        "public_quiz",
        "code":        code,
        "topic":       topic,
        "creator":     creator.lower(),
        "creatorName": creator_name,
        "stake":       stake,
        "token":       token,
    })


async def _dm_send(frm: str, to: str, body: str, kind: str = "text", meta: dict | None = None) -> dict:
    """Persist a DM and live-push it to the recipient's /ws/notify socket."""
    msg = await dm_service.send(pool, frm, to, body, kind=kind, meta=meta)
    profile = await _get_live_profile(frm)
    payload = {
        "type":       "dm_message",
        "message":    msg,
        "peer":       msg["from"],
        "peerName":   profile["username"],
        "peerAvatar": profile["avatar_url"],
    }
    live = bool(notify_conn.get(to.lower()))
    msg["delivered"] = live

    try:
        await notif._push(to.lower(), payload)
    except Exception as e:
        logger.warning("dm push failed to=%s: %s", to, e)
        msg["delivered"] = False

    return msg   # ← THIS LINE WAS MISSING

@app.websocket("/ws/presence")
async def presence_socket(ws: WebSocket):
    await ws.accept()
    presence_sockets.append(ws)
    await ws.send_json({"type": "presence", "online": await _online_list()})
    online = [
        {"wallet": w, "username": info["username"], "avatar_url": info["avatar_url"]}
        for w, info in presence_wallets.items()
    ]
    await ws.send_json({"type": "presence", "online": online})

    wallet = None
    try:
        async for msg in ws.iter_json():
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})

            elif msg.get("type") == "hello" and msg.get("wallet"):
                wallet  = msg["wallet"].lower()
                profile = await _get_live_profile(wallet)   # reads user_profiles by wallet
                presence_wallets[wallet] = {
                    "username":   profile["username"],
                    "avatar_url": profile["avatar_url"],
                }
                await _broadcast_presence()

    except WebSocketDisconnect:
        pass
    finally:
        if ws in presence_sockets:
            presence_sockets.remove(ws)
        if wallet and wallet in presence_wallets:
            presence_wallets.pop(wallet, None)
            await _broadcast_presence()

@app.on_event("startup")
async def _startup_onchain_sync():
    """Start the 2-hour background sync scheduler when the server boots."""
    # pool and supabase must already be initialised above this point in main.py
    app.state.sync_task = start_background_scheduler(pool, supabase)
 
 
@app.on_event("shutdown")
async def _shutdown_onchain_sync():
    stop_background_scheduler()
 

# ─── Internal helpers ─────────────────────────────────────────────────────────

def _safe_challenge(c: dict) -> dict:
    import copy, datetime
    safe = copy.deepcopy(c)
    safe["player_offers"] = c.get("player_offers", {})
    safe_rounds = strip_answers({"rounds": safe.get("rounds", [])})
    safe["rounds"] = safe_rounds["rounds"]
    if "agreedStake" not in safe:
        safe["agreedStake"] = None
    if "stakeAmount" not in safe and "stake" in safe:
        safe["stakeAmount"] = safe["stake"]
    if "stake" not in safe:
        safe["stake"] = safe.get("stakeAmount") or 0
    safe.setdefault("negotiationLocked", c.get("negotiationLocked", False))
    safe.setdefault("creatorAvatar", c.get("creatorAvatar", ""))

    def _serialize(obj):
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_serialize(i) for i in obj]
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return obj

    return _serialize(safe)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=True)