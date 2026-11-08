"""
bot_config.py
─────────────
Static configuration for FaucetDrops single-player bot opponents.

Five difficulty tiers (1 = easiest, 5 = hardest).
Stake, base accuracy, target win-rate, and question count all scale with
difficulty.  Bot-side transactions are 100 % simulated — no private keys,
no gas.

Difficulty table
────────────────
  Level  Tier name   Bot username        Stake   Questions  Bot accuracy  Bot win-rate
  ─────  ──────────  ──────────────────  ──────  ─────────  ────────────  ────────────
    1    Droplet     Droplet 💧           10 DROPS    15        ~35 %          ~25 %
    2    Drizzle     Drizzle 🌦            20 DROPS    18        ~50 %          ~42 %
    3    Downpour    Downpour 🌧           30 DROPS    21        ~63 %          ~58 %
    4    Torrent     Torrent ⛈            40 DROPS    24        ~76 %          ~72 %
    5    Flood       Flood 🌊             50 DROPS    30        ~90 %          ~87 %

Notes
─────
• base_accuracy is the raw probability the bot picks the correct option.
  A small score-state nudge (±0.08) is applied mid-game to keep outcomes
  feeling non-deterministic without departing too far from the target.
• Bots at higher difficulties also answer faster, earning more speed-bonus
  points per correct answer — compounding the difficulty felt by the creator.
• question_count is fixed per difficulty and is the authoritative source used
  by the game creation endpoint — callers must NOT override it.
• BOT_WALLETS is a set used throughout the codebase to detect bot players
  and skip on-chain / reward logic for them.
"""
from __future__ import annotations

# ── Bot profiles ──────────────────────────────────────────────────────────────
import os

BOT_PROFILES: dict[int, dict] = {
    1: {
        "wallet":          os.getenv("BOT_1_ADDRESS", "0x892e7bbD58C35eE9Bd5A9DE15a8B82207341542f"),
        "private_key":     os.getenv("BOT_1_PRIVATE_KEY", ""),
        "username":        "Droplet 💧",
        "tier_name":       "Droplet",
        "avatar_url":      "https://app.faucetdrops.io/bots/droplet.png",
        "stake":           10.0,
        "question_count":  15,
        "base_accuracy":   0.35,
        "target_win_rate": 0.25,
        "speed_max_frac":  0.75,
    },
    2: {
        "wallet":          os.getenv("BOT_2_ADDRESS", "0x1d900C986cC48609183969D9f1065bF126eEc320"),
        "private_key":     os.getenv("BOT_2_PRIVATE_KEY", ""),
        "username":        "Drizzle 🌦",
        "tier_name":       "Drizzle",
        "avatar_url":      "https://app.faucetdrops.io/bots/drizzle.png",
        "stake":           20.0,
        "question_count":  18,
        "base_accuracy":   0.50,
        "target_win_rate": 0.42,
        "speed_max_frac":  0.65,
    },
    3: {
        "wallet":          os.getenv("BOT_3_ADDRESS", "0xD73c0cC06A6c601B461899208C0cF3E0b4E8485A"),
        "private_key":     os.getenv("BOT_3_PRIVATE_KEY", ""),
        "username":        "Downpour 🌧",
        "tier_name":       "Downpour",
        "avatar_url":      "https://app.faucetdrops.io/bots/downpour.png",
        "stake":           30.0,
        "question_count":  21,
        "base_accuracy":   0.63,
        "target_win_rate": 0.58,
        "speed_max_frac":  0.55,
    },
    4: {
        "wallet":          os.getenv("BOT_4_ADDRESS", "0xC6a63c7B2401a4D94304CDA281DD688A23a52aE0"),
        "private_key":     os.getenv("BOT_4_PRIVATE_KEY", ""),
        "username":        "Torrent ⛈",
        "tier_name":       "Torrent",
        "avatar_url":      "https://app.faucetdrops.io/bots/torrent.png",
        "stake":           40.0,
        "question_count":  24,
        "base_accuracy":   0.76,
        "target_win_rate": 0.72,
        "speed_max_frac":  0.45,
    },
    5: {
        "wallet":          os.getenv("BOT_5_ADDRESS", "0x53F2948aF4a6589b7e6F50d4846Ce0a686f5880B"),
        "private_key":     os.getenv("BOT_5_PRIVATE_KEY", ""),
        "username":        "Flood 🌊",
        "tier_name":       "Flood",
        "avatar_url":      "https://app.faucetdrops.io/bots/flood.png",
        "stake":           50.0,
        "question_count":  30,
        "base_accuracy":   0.90,
        "target_win_rate": 0.87,
        "speed_max_frac":  0.35,
    },
}
# ── Thresholds ────────────────────────────────────────────────────────────────
# Difficulty at or above which:
#   • a solo WIN pays into the REWARD pouch instead of the game pouch
#   • a solo game counts as a "qualifying game" toward the Redeem Badge
# (3 = Downpour, 4 = Torrent, 5 = Flood)
HIGH_DIFFICULTY_MIN = 3

# Convenience set used by the rest of the codebase to detect bot players.
BOT_WALLETS: set[str] = {p["wallet"].lower() for p in BOT_PROFILES.values()}

DIFFICULTY_LEVELS: list[int] = sorted(BOT_PROFILES.keys())   # [1, 2, 3, 4, 5]


def get_bot_profile(difficulty: int) -> dict:
    """Return the profile for *difficulty*, raising ValueError for invalid inputs."""
    if difficulty not in BOT_PROFILES:
        raise ValueError(
            f"Invalid difficulty {difficulty!r}. "
            f"Must be one of: {DIFFICULTY_LEVELS}"
        )
    return BOT_PROFILES[difficulty]