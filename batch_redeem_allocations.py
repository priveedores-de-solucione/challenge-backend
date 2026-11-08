"""
sequential_redeem.py  (v2 — 600-wallet edition)
────────────────────────────────────────────────
Walk every wallet in enriched_wallets.csv one at a time, check its
reward_drops balance on Botchain, and call the production redeem path
(drops_service.redeem_reward_drops).

Changes from v1
───────────────
• Reads enriched_wallets.csv produced by prepare_and_seed.py instead of
  allocations.csv.  That file contains per-wallet redeem_cap, source
  (focused / high_tx), and pre-computed reward_drops.
• Enforces the per-wallet redeem_cap: the amount sent to the on-chain call
  is min(db_reward_drops, wallet.redeem_cap).
  - 50 randomly-chosen wallets → cap 20 drops
  - all others                 → cap random [5, 15] drops
• Validates reward_drops ≤ game_drops before any on-chain call.
• Still writes to redeem_burn_queue on failure and mirrors stats to
  challenge_players on success.

Usage
─────
    python sequential_redeem.py --dry-run
    python sequential_redeem.py
    python sequential_redeem.py --resume
    python sequential_redeem.py --limit 100

Env (unchanged from v1):
  DATABASE_URL / SUPABASE_DB_URL
  RESOLVER_PRIVATE_KEY, BOTCHAIN_RPC_URL
  DROPS_REDEEM_POOL_BOTCHAIN, DROPS_TOKEN_CONTRACT_BOTCHAIN
  SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import random
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sequential_redeem.log"),
    ],
)
log = logging.getLogger("seq_redeem")

# ── Constants ─────────────────────────────────────────────────────────────────
PLAYER_SHARE_PCT  = 0.75
STAKE_SHARE_PCT   = 0.25
FEE_PCT           = 0.10
DROPS_PER_USD     = 100.0
BOTCHAIN_ID       = 677

JITTER_MIN_S = 40
JITTER_MAX_S = 100

# ── WBOT price ────────────────────────────────────────────────────────────────
BDEX_V2_FACTORY   = "0x117115f3B72C8d1989178089A67D0C26f8EE0AA3"
WBOT_ADDRESS      = "0xD5452816194a3784dBa983426cCe7c122F4abd30"
STABLE_ADDRESS    = "0xaBabc7Ddc03e501d190C676BF3d92ef0e6e87a3C"
STABLE_DECIMALS   = 6
WBOT_DECIMALS     = 18
WBOT_FALLBACK_USD = 9.2

_FACTORY_ABI = [{"inputs":[{"name":"tokenA","type":"address"},{"name":"tokenB","type":"address"}],"name":"getPair","outputs":[{"name":"pair","type":"address"}],"stateMutability":"view","type":"function"}]
_PAIR_ABI    = [{"inputs":[],"name":"getReserves","outputs":[{"name":"reserve0","type":"uint112"},{"name":"reserve1","type":"uint112"},{"name":"blockTimestampLast","type":"uint32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]

_wbot_cache: dict = {"price": None, "at": 0.0}
_WBOT_TTL = 120


def _fetch_wbot_sync() -> float:
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(os.getenv("BOTCHAIN_RPC_URL", "https://rpc.botchain.ai")))
    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        pass
    factory  = w3.eth.contract(address=Web3.to_checksum_address(BDEX_V2_FACTORY), abi=_FACTORY_ABI)
    pair_addr = factory.functions.getPair(
        Web3.to_checksum_address(WBOT_ADDRESS),
        Web3.to_checksum_address(STABLE_ADDRESS),
    ).call()
    zero = "0x" + "0" * 40
    if not pair_addr or pair_addr.lower() == zero:
        raise RuntimeError("WBOT/stable pair not found")
    pair     = w3.eth.contract(address=Web3.to_checksum_address(pair_addr), abi=_PAIR_ABI)
    reserves = pair.functions.getReserves().call()
    token0   = pair.functions.token0().call()
    is_wbot0 = token0.lower() == WBOT_ADDRESS.lower()
    r_wbot   = reserves[0] if is_wbot0 else reserves[1]
    r_stable = reserves[1] if is_wbot0 else reserves[0]
    wbot_n   = r_wbot   / (10 ** WBOT_DECIMALS)
    stable_n = r_stable / (10 ** STABLE_DECIMALS)
    if wbot_n == 0:
        raise RuntimeError("Zero WBOT reserve")
    price = stable_n / wbot_n
    if not (0 < price < 100_000):
        raise RuntimeError(f"Suspicious WBOT price: {price}")
    return price


async def fetch_wbot_price_usd() -> float:
    now = time.monotonic()
    if _wbot_cache["price"] is not None and (now - _wbot_cache["at"]) < _WBOT_TTL:
        return _wbot_cache["price"]
    loop = asyncio.get_event_loop()
    try:
        price = await loop.run_in_executor(None, _fetch_wbot_sync)
        _wbot_cache.update({"price": price, "at": now})
        log.info("WBOT price (BDEX V2): $%.4f", price)
        return price
    except Exception as exc:
        stale = _wbot_cache.get("price")
        if stale:
            log.warning("WBOT price fetch failed (%s) — using cached $%.4f", exc, stale)
            return stale
        log.warning("WBOT price fetch failed (%s) — using fallback $%.4f", exc, WBOT_FALLBACK_USD)
        return WBOT_FALLBACK_USD


# ── Shutdown ──────────────────────────────────────────────────────────────────

_SHUTDOWN = False

def _handle_signal(sig, _frame):
    global _SHUTDOWN
    log.info("Signal %s — finishing current wallet then stopping.", sig)
    _SHUTDOWN = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class HourlyRateLimiter:
    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self._ts: deque[float] = deque()

    def _trim(self):
        cutoff = time.monotonic() - 3600
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()

    def count(self) -> int:
        self._trim(); return len(self._ts)

    async def wait_if_needed(self):
        while True:
            self._trim()
            if len(self._ts) < self.max_per_hour:
                return
            sleep_s = (self._ts[0] + 3600) - time.monotonic() + 1
            log.info("Rate cap %d/%d/hr — sleeping %.0f s (%.1f min)…",
                     len(self._ts), self.max_per_hour, sleep_s, sleep_s / 60)
            await asyncio.sleep(sleep_s)

    def record(self):
        self._ts.append(time.monotonic())


# ── Stats ─────────────────────────────────────────────────────────────────────

@dataclass
class RunStats:
    total:          int   = 0
    succeeded:      int   = 0
    failed:         int   = 0
    queued:         int   = 0
    drops_redeemed: float = 0.0
    g_paid_out:     float = 0.0
    skip_reasons:   dict  = field(default_factory=dict)

    def skipped(self): return sum(self.skip_reasons.values())


# ── Queue table ───────────────────────────────────────────────────────────────

_CREATE_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS redeem_burn_queue (
    id              BIGSERIAL PRIMARY KEY,
    wallet_address  TEXT        NOT NULL,
    chain_id        INT         NOT NULL,
    reward_drops    NUMERIC     NOT NULL,
    player_g        NUMERIC     NOT NULL,
    fee_g           NUMERIC     NOT NULL,
    staked_drops    NUMERIC     NOT NULL,
    apy_pct         INT         NOT NULL,
    g_price_usd     NUMERIC     NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending',
    last_error      TEXT,
    attempt_count   INT         NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rbq_status ON redeem_burn_queue (status);
CREATE INDEX IF NOT EXISTS idx_rbq_wallet ON redeem_burn_queue (wallet_address);
"""

async def ensure_queue_table(conn: asyncpg.Connection) -> None:
    await conn.execute(_CREATE_QUEUE_TABLE)


async def queue_failed_wallet(
    pool: asyncpg.Pool, wallet: str, chain_id: int,
    reward_drops: float, player_g: float, fee_g: float,
    staked_drops: float, apy_pct: int, g_price_usd: float, error: str,
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO redeem_burn_queue
                (wallet_address, chain_id, reward_drops, player_g, fee_g,
                 staked_drops, apy_pct, g_price_usd, last_error)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT DO NOTHING RETURNING id
            """,
            wallet.lower(), chain_id, reward_drops, player_g, fee_g,
            staked_drops, apy_pct, g_price_usd, error[:500],
        )
        if not row:
            row = await conn.fetchrow(
                "SELECT id FROM redeem_burn_queue "
                "WHERE wallet_address=$1 AND chain_id=$2 AND status='pending'",
                wallet.lower(), chain_id,
            )
        return int(row["id"]) if row else -1


# ── CSV helpers ───────────────────────────────────────────────────────────────

REPORT_FIELDS = [
    "index", "wallet", "source", "redeem_cap", "status", "reward_drops",
    "game_drops", "player_g", "fee_g", "staked_drops", "apy_pct",
    "g_price_usd", "tx_hash", "stake_id", "queue_id", "error", "timestamp",
]


@dataclass
class EnrichedWallet:
    """One row from enriched_wallets.csv."""
    index:        int
    address:      str
    source:       str
    tx_count:     int
    game_drops:   float
    reward_drops: float   # pre-computed by prepare_and_seed
    redeem_cap:   int     # 20 or [5-15]
    total_wins:   int
    total_duels:  int
    total_earned: float


def load_enriched_wallets(csv_path: str) -> list[EnrichedWallet]:
    rows: list[EnrichedWallet] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            try:
                rows.append(EnrichedWallet(
                    index        = i,
                    address      = row["address"].strip().lower(),
                    source       = row.get("source", "unknown"),
                    tx_count     = int(row.get("tx_count", 0) or 0),
                    game_drops   = float(row.get("game_drops", 0) or 0),
                    reward_drops = float(row.get("reward_drops", 0) or 0),
                    redeem_cap   = int(row.get("redeem_cap", 15) or 15),
                    total_wins   = int(row.get("total_wins", 0) or 0),
                    total_duels  = int(row.get("total_duels", 0) or 0),
                    total_earned = float(row.get("total_earned", 0) or 0),
                ))
            except Exception as exc:
                log.warning("Skipping malformed row %d: %s — %s", i, row, exc)
    return rows


def load_resume_set(report_path: str) -> set[str]:
    done: set[str] = set()
    if not os.path.exists(report_path):
        return done
    with open(report_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") in ("success", "dry_run"):
                done.add((row.get("wallet") or "").lower())
    return done


def open_report(report_path: str, resume: bool):
    exists = os.path.exists(report_path)
    mode   = "a" if (resume and exists) else "w"
    fh     = open(report_path, mode, newline="")
    w      = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
    if mode == "w":
        w.writeheader(); fh.flush()
    return fh, w


# ── Per-wallet processor ──────────────────────────────────────────────────────

async def process_wallet(
    *,
    ew:           EnrichedWallet,
    db_pool:      asyncpg.Pool,
    chain_id:     int,
    min_drops:    float,
    dry_run:      bool,
    writer:       csv.DictWriter,
    fh,
    stats:        RunStats,
    rate_limiter: HourlyRateLimiter,
) -> None:
    import drops_service as ds

    row_out = {k: "" for k in REPORT_FIELDS}
    row_out.update({
        "index":      ew.index,
        "wallet":     ew.address,
        "source":     ew.source,
        "redeem_cap": ew.redeem_cap,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })

    def _flush(status: str, **kw):
        row_out["status"] = status
        row_out.update(kw)
        writer.writerow(row_out)
        fh.flush()

    # ── 1. Read live DB balance ───────────────────────────────────────────────
    try:
        async with db_pool.acquire() as conn:
            bal = await conn.fetchrow(
                """
                SELECT reward_drops, game_drops,
                       COALESCE(total_duels, 0) AS total_duels
                FROM challenge_player_balances
                WHERE wallet_address = $1 AND chain_id = $2
                """,
                ew.address, chain_id,
            )
    except Exception as exc:
        stats.failed += 1
        _flush("failed", error=f"DB read: {exc}")
        log.error("[%d] %s  DB read failed: %s", ew.index, ew.address, exc)
        return

    if not bal:
        # Row wasn't seeded — insert minimal record from enriched CSV data
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO challenge_player_balances
                        (wallet_address, chain_id, game_drops, reward_drops,
                         total_wins, total_duels, total_earned, redeem_badge)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,false)
                    ON CONFLICT DO NOTHING
                    """,
                    ew.address, chain_id, ew.game_drops, ew.reward_drops,
                    ew.total_wins, ew.total_duels, ew.total_earned,
                )
            reward_drops = ew.reward_drops
            game_drops   = ew.game_drops
            total_duels  = ew.total_duels
        except Exception as exc:
            stats.skip_reasons["no_row"] = stats.skip_reasons.get("no_row", 0) + 1
            _flush("skipped", error=f"no DB row and insert failed: {exc}")
            return
    else:
        reward_drops = float(bal["reward_drops"] or 0)
        game_drops   = float(bal["game_drops"]   or 0)
        total_duels  = int(bal["total_duels"]    or 0)

    # ── 2. Guard: reward must not exceed on-chain game balance ────────────────
    if game_drops > 0 and reward_drops > game_drops:
        log.warning(
            "[%d] %s  reward_drops %.2f > game_drops %.2f — clamping",
            ew.index, ew.address, reward_drops, game_drops,
        )
        reward_drops = game_drops

    # ── 3. Apply per-wallet redeem cap ────────────────────────────────────────
    reward_drops = min(reward_drops, float(ew.redeem_cap))

    if reward_drops < min_drops:
        stats.skip_reasons["below_min"] = stats.skip_reasons.get("below_min", 0) + 1
        _flush("skipped", reward_drops=round(reward_drops, 6),
               error=f"reward_drops {reward_drops:.4f} < {min_drops}")
        return

    row_out["reward_drops"] = round(reward_drops, 6)
    row_out["game_drops"]   = round(game_drops, 6)

    # ── 4. Compute payout split ───────────────────────────────────────────────
    g_price_usd = await fetch_wbot_price_usd()
    apy_pct     = ds.get_tier(total_duels)[1]

    usd          = reward_drops / DROPS_PER_USD
    player_usd   = usd * PLAYER_SHARE_PCT
    fee_usd      = player_usd * FEE_PCT
    player_g     = player_usd / g_price_usd
    fee_g        = fee_usd    / g_price_usd
    staked_drops = reward_drops * STAKE_SHARE_PCT

    row_out.update(
        player_g=round(player_g, 6), fee_g=round(fee_g, 6),
        staked_drops=round(staked_drops, 6), apy_pct=apy_pct,
        g_price_usd=g_price_usd,
    )

    if dry_run:
        stats.succeeded      += 1
        stats.drops_redeemed += reward_drops
        stats.g_paid_out     += player_g
        _flush("dry_run")
        log.info(
            "[%d] DRY-RUN  wallet=%s  src=%s  cap=%d  reward_drops=%.2f  "
            "game_drops=%.2f  player_g=%.6f",
            ew.index, ew.address, ew.source, ew.redeem_cap,
            reward_drops, game_drops, player_g,
        )
        return

    # ── 5. Wait for rate slot ─────────────────────────────────────────────────
    await rate_limiter.wait_if_needed()

    # ── 6. Ensure redeem badge ────────────────────────────────────────────────
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_player_balances
                   SET redeem_badge = true
                   WHERE wallet_address = $1 AND chain_id = $2
                     AND (redeem_badge IS NULL OR redeem_badge = false)""",
                ew.address, chain_id,
            )
    except Exception as exc:
        log.warning("[%d] badge ensure non-fatal: %s", ew.index, exc)

    # ── 7. On-chain redeem ────────────────────────────────────────────────────
    try:
        result = await ds.redeem_reward_drops(
            db_pool, ew.address, reward_drops,
            player_g=player_g, fee_g=fee_g, staked_drops=staked_drops,
            apy_pct=apy_pct, g_price_usd=g_price_usd, chain_id=chain_id,
        )

        stats.succeeded      += 1
        stats.drops_redeemed += reward_drops
        stats.g_paid_out     += player_g
        rate_limiter.record()

        _flush("success",
               tx_hash=result.get("txHash", ""), stake_id=result.get("stakeId", ""))
        log.info(
            "[%d] OK  wallet=%s  src=%s  cap=%d  drops=%.2f  player_g=%.6f  tx=%s",
            ew.index, ew.address, ew.source, ew.redeem_cap,
            reward_drops, player_g, result.get("txHash"),
        )

        # ── 8. Mirror to challenge_players ───────────────────────────────────
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO challenge_players
                        (wallet_address, total_duels, total_wins, total_earned)
                    SELECT wallet_address, total_duels, total_wins, total_earned
                    FROM   challenge_player_balances
                    WHERE  wallet_address = $1 AND chain_id = $2
                    ON CONFLICT (wallet_address) DO UPDATE
                        SET total_duels  = GREATEST(challenge_players.total_duels,  EXCLUDED.total_duels),
                            total_wins   = GREATEST(challenge_players.total_wins,   EXCLUDED.total_wins),
                            total_earned = GREATEST(challenge_players.total_earned, EXCLUDED.total_earned)
                    """,
                    ew.address, chain_id,
                )
        except Exception as exc:
            log.warning("[%d] challenge_players mirror non-fatal: %s", ew.index, exc)

    except Exception as exc:
        err_str = str(exc)[:400]
        stats.failed += 1
        try:
            qid = await queue_failed_wallet(
                db_pool, ew.address, chain_id, reward_drops,
                player_g, fee_g, staked_drops, apy_pct, g_price_usd, err_str,
            )
            stats.queued += 1
            _flush("failed_queued", queue_id=qid, error=err_str)
            log.error("[%d] FAILED → queued id=%d  wallet=%s  error=%s",
                      ew.index, qid, ew.address, err_str)
        except Exception as qe:
            _flush("failed", error=err_str)
            log.error("[%d] FAILED + queue insert failed  wallet=%s  redeem_err=%s  queue_err=%s",
                      ew.index, ew.address, err_str, qe)


# ── Post-batch sync ───────────────────────────────────────────────────────────

async def run_post_sync(db_url: str, supa_url: str, supa_key: str) -> None:
    from supabase import create_client
    import onchain_sync as osync
    supabase  = create_client(supa_url, supa_key)
    sync_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3, statement_cache_size=0)
    try:
        result = await osync.run_sync(sync_pool, supabase, chains=["botchain"])
        for cr in result.get("chains", []):
            log.info(
                "  sync %s  wallets=%s  duels=%s  wins=%s  redeemed=%.4f",
                cr.get("chain"), cr.get("wallets_found"),
                cr.get("total_duels_in_range"), cr.get("total_wins_in_range"),
                cr.get("drops_redeemed_in_range", 0.0),
            )
    finally:
        await sync_pool.close()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sequential 600-wallet redeem — per-wallet caps enforced"
    )
    parser.add_argument("--csv",        default="enriched_wallets.csv",
                        help="Enriched wallet list from prepare_and_seed.py")
    parser.add_argument("--chain-id",   type=int,   default=BOTCHAIN_ID)
    parser.add_argument("--min-drops",  type=float, default=1.0)
    parser.add_argument("--rate-limit", type=int,   default=50)
    parser.add_argument("--limit",      type=int,   default=0)
    parser.add_argument("--report",     default="seq_redeem_report.csv")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--resume",     action="store_true")
    parser.add_argument("--no-sync",    action="store_true")
    args = parser.parse_args()

    load_dotenv()
    db_url   = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    supa_url = os.getenv("NEXT_PUBLIC_SUPABASE_URL") or os.getenv("SUPABASE_URL")
    supa_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not db_url:
        raise SystemExit("Set DATABASE_URL in env / .env")

    wallets = load_enriched_wallets(args.csv)
    log.info("Loaded %d wallets from %s", len(wallets), args.csv)

    if args.resume:
        done = load_resume_set(args.report)
        log.info("Resume: skipping %d already-succeeded wallets", len(done))
        wallets = [w for w in wallets if w.address not in done]

    if args.limit:
        wallets = wallets[: args.limit]

    # Stats preview
    cap20_count = sum(1 for w in wallets if w.redeem_cap == 20)
    focused_n   = sum(1 for w in wallets if w.source == "focused")
    high_tx_n   = sum(1 for w in wallets if w.source == "high_tx")

    log.info(
        "Processing %d wallets  (focused=%d high_tx=%d)  "
        "cap20=%d  rate_limit=%d/hr  jitter=%d-%ds  dry_run=%s",
        len(wallets), focused_n, high_tx_n, cap20_count,
        args.rate_limit, JITTER_MIN_S, JITTER_MAX_S, args.dry_run,
    )

    db_pool = await asyncpg.create_pool(db_url, min_size=2, max_size=6, statement_cache_size=0)
    async with db_pool.acquire() as conn:
        await ensure_queue_table(conn)

    fh, writer   = open_report(args.report, args.resume)
    stats        = RunStats(total=len(wallets))
    rate_limiter = HourlyRateLimiter(args.rate_limit)
    start        = time.monotonic()

    try:
        for seq, ew in enumerate(wallets, 1):
            if _SHUTDOWN:
                log.info("Shutdown — stopping before wallet %d/%d.", seq, len(wallets))
                break

            log.info(
                "── [%d/%d] wallet=%s  src=%s  cap=%d  csv_reward=%.1f  success_hr=%d",
                seq, len(wallets), ew.address, ew.source, ew.redeem_cap,
                ew.reward_drops, rate_limiter.count(),
            )
            await process_wallet(
                ew=ew, db_pool=db_pool, chain_id=args.chain_id,
                min_drops=args.min_drops, dry_run=args.dry_run,
                writer=writer, fh=fh, stats=stats, rate_limiter=rate_limiter,
            )

            if seq < len(wallets) and not _SHUTDOWN and not args.dry_run:
                jitter = random.uniform(JITTER_MIN_S, JITTER_MAX_S)
                log.debug("Jitter sleep %.1f s", jitter)
                await asyncio.sleep(jitter)

    finally:
        elapsed = time.monotonic() - start
        fh.close()
        await db_pool.close()

    log.info("═" * 60)
    log.info("Done  %.0f s / %.1f min", elapsed, elapsed / 60)
    log.info("  checked     : %d", stats.total)
    log.info("  succeeded%s : %d", " (dry)" if args.dry_run else "", stats.succeeded)
    log.info("  failed      : %d  (queued: %d)", stats.failed, stats.queued)
    for reason, n in sorted(stats.skip_reasons.items()):
        log.info("  skipped (%s): %d", reason, n)
    log.info("  drops total : %.4f", stats.drops_redeemed)
    log.info("  $G total    : %.6f", stats.g_paid_out)
    log.info("  report      : %s", args.report)

    if not args.dry_run and not args.no_sync and stats.succeeded > 0:
        if supa_url and supa_key:
            log.info("Running onchain_sync…")
            try:
                await run_post_sync(db_url, supa_url, supa_key)
            except Exception as exc:
                log.error("onchain_sync failed (run manually): %s", exc)
        else:
            log.warning("SUPABASE env not set — skipping onchain_sync")
    elif args.dry_run:
        log.info("Dry-run — skipping sync.")


if __name__ == "__main__":
    asyncio.run(main())