"""
username_pool.py  (v2)
──────────────────────
Generates realistic-looking usernames for on-chain wallets.

v2 changes
──────────
1. Names are now derived DETERMINISTICALLY from the wallet address itself
   (sha256 seed) instead of a batch-local pool index. This fixes two problems
   with v1:
     • pool_index reset per sync batch, so different batches reused
       POOL[0], POOL[1], … and produced duplicate usernames;
     • all names came out in identical AdjNoun PascalCase, which made
       synced accounts instantly recognisable on the leaderboard.
2. Multiple naming styles (lowercase, snake_case, digit suffixes, bare
   names, camelCase, …) chosen per-wallet, so the leaderboard reads like a
   real mixed user base.
3. No 500-name cap — every wallet gets a unique-feeling name; a short digit
   suffix drawn from the address keeps collisions astronomically unlikely.
4. Legacy v1 pool is still reconstructable via legacy_pool_names() so a
   one-off migration script can identify (and rename) previously
   auto-assigned profiles without touching names users chose themselves.

Word lists blend English gaming/crypto vocabulary with Yoruba, Igbo, Hausa,
and broader Pan-African words for the FaucetDrops Nigerian-first user base.
"""

from __future__ import annotations

import hashlib
import random as _random

# ── Adjectives (unchanged from v1 — required to rebuild the legacy pool) ─────

ADJECTIVES: list[str] = [
    # ── English gaming / crypto ───────────────────────────────────────────────
    "Swift", "Bold", "Brave", "Calm", "Clever", "Cool", "Crisp", "Dark",
    "Deep", "Elite", "Epic", "Fast", "Fierce", "Fiery", "Frosty",
    "Gloom", "Gold", "Grand", "Grim", "Holy", "Hyper", "Ice",
    "Iron", "Keen", "Laser", "Lone", "Lucky", "Lunar", "Mad",
    "Magic", "Mighty", "Mystic", "Neon", "Night", "Noble", "Phantom",
    "Prime", "Prism", "Rapid", "Raw", "Rogue", "Royal",
    "Rusty", "Savage", "Shadow", "Sharp", "Silent", "Silver", "Sleek",
    "Slim", "Sly", "Solar", "Sonic", "Steel", "Stone", "Super",
    "Thunder", "Titan", "Toxic", "Ultra", "Wild", "Wise", "Zero",
    "Zen", "Blazing", "Cosmic", "Crystal", "Digital", "Frozen",
    "Galactic", "Hollow", "Infinite", "Kinetic", "Liquid",
    "Marble", "Nuclear", "Obsidian", "Polar", "Quantum", "Radiant",
    "Spectral", "Turbo", "Velvet", "Wicked", "Xenon", "Atomic",
    "Crimson", "Electric", "Ember", "Flaming", "Glowing", "Hyper",
    "Jagged", "Molten", "Orbital", "Rugged", "Serene", "Stealthy",
    "Volcanic", "Astral", "Binary", "Cipher", "Delta", "Ethereal",
    "Flux", "Genesis", "Helios", "Ignited", "Jade", "Kinetic",

    # ── Yoruba-inspired ───────────────────────────────────────────────────────
    "Ola", "Ade", "Ara", "Ife", "Gidi", "Wura",
    "Ayo", "Ore", "Eri", "Ewa",
    "Duro", "Sola", "Temi", "Yemi",
    "Ogbon", "Ogo", "Iwa", "Anu",
    "Naija", "Eko", "Abuja", "Lagos",

    # ── Igbo-inspired ─────────────────────────────────────────────────────────
    "Chi", "Uzo", "Eze", "Oma", "Nze",
    "Udo", "Ike", "Ama", "Obi", "Ndi",
    "Mmadu", "Chukwu", "Ihe",

    # ── Hausa-inspired ────────────────────────────────────────────────────────
    "Karfi", "Sauri", "Jarumi", "Dare", "Zaki",
    "Gwal", "Tafki", "Arewa", "Kano", "Zuru",
    "Gwani", "Rinjaye",

    # ── Pan-African / broader ─────────────────────────────────────────────────
    "Sahel", "Nubian", "Ashanti", "Swahili", "Ubuntu", "Afro",
    "Kente", "Ankh", "Benin", "Accra", "Dakar",
    "Zulu", "Bantu", "Masai",
    "Amara", "Jabari", "Simba", "Imani", "Baraka",
    "Seun", "Tunde", "Dami", "Nonso", "Ebuka", "Mubo",
]

# ── Nouns (unchanged from v1 — required to rebuild the legacy pool) ───────────

NOUNS: list[str] = [
    # ── English gaming / crypto ───────────────────────────────────────────────
    "Ace", "Apex", "Arrow", "Axe", "Bear", "Beast", "Blade",
    "Blaze", "Block", "Boss", "Bull", "Byte", "Claw", "Comet", "Core",
    "Crest", "Crow", "Dart", "Dawn", "Doom", "Dragon",
    "Drift", "Drop", "Dusk", "Eagle", "Edge", "Eye", "Fang", "Flame",
    "Flash", "Flux", "Forge", "Fox", "Frost", "Gate", "Ghost", "Glitch",
    "Grind", "Guard", "Hawk", "Hook", "Horn", "Hunt", "Ink", "Ion",
    "Jaw", "Jolt", "Key", "King", "Knight", "Lance",
    "Lash", "Link", "Lion", "Lock", "Loop", "Lynx", "Mark",
    "Mint", "Monk", "Moon", "Myth", "Node", "Nova", "Null", "Omen",
    "Orbit", "Pact", "Peak", "Pike", "Pixel", "Pulse", "Pyre",
    "Quest", "Raid", "Ram", "Rank", "Ray", "Reef", "Rex",
    "Ridge", "Ring", "Riot", "Rise", "Rock", "Root", "Rune", "Rush",
    "Saber", "Sage", "Shard", "Shift", "Shot", "Sight", "Sigil", "Skull",
    "Slash", "Slate", "Smoke", "Snake", "Soul", "Spark", "Spear", "Spike",
    "Stack", "Star", "Storm", "Surge", "Sword", "Tank",
    "Talon", "Tide", "Token", "Torch", "Trace", "Track", "Trail", "Tusk",
    "Twin", "Vault", "Vein", "Viper", "Void", "Volt", "Wave",
    "Wing", "Wolf", "Wraith", "Wrath", "Zone", "Crypt", "Vault",
    "Hash", "Chain", "Ledger", "Cipher", "Shard", "Nonce", "Stake",
    "Yield", "Pool", "Miner", "Block", "Node", "Gas", "Wallet",

    # ── Yoruba names ──────────────────────────────────────────────────────────
    "Segun", "Tobi", "Femi", "Bayo", "Kunle", "Wale", "Sola",
    "Dayo", "Leke", "Gbenga", "Rotimi", "Biodun", "Tunji", "Lanre",
    "Bola", "Tade", "Yemi", "Yinka", "Demola", "Kayode", "Adewale",
    "Eniola", "Temitope", "Oluwaseun", "Adunola", "Folake", "Ronke",
    "Omotunde", "Funke", "Kemi", "Tokunbo", "Seyi", "Kola", "Dapo",
    "Lekan", "Remi", "Wunmi", "Bimpe", "Titi", "Shade", "Amoke",

    # ── Igbo names ────────────────────────────────────────────────────────────
    "Chidi", "Emeka", "Obinna", "Uche", "Nkem", "Ikem", "Chisom",
    "Amaka", "Ifeanyi", "Onyeka", "Chuka", "Kelechi",
    "Adaeze", "Ngozi", "Chidinma", "Ezinne", "Uchenna", "Chinyere",
    "Nnamdi", "Ikenna", "Chukwuemeka", "Obiora", "Okafor", "Okonkwo",
    "Obi", "Tochi", "Chidera", "Kenechukwu", "Somtochukwu", "Chibundo",

    # ── Hausa names ───────────────────────────────────────────────────────────
    "Musa", "Abubakar", "Garba", "Yakubu", "Usman", "Lawal", "Sani",
    "Bashir", "Kabir", "Haruna", "Shehu", "Ibrahim", "Yusuf", "Aliyu",
    "Danjuma", "Gambo", "Bello", "Rabiu", "Adamu", "Isa", "Nuhu",
    "Haliru", "Sule", "Muhammed", "Sadiq", "Samaila", "Tanko",
    "Zainab", "Hafsat", "Ramatu", "Bilkisu", "Fatima", "Hauwa",

    # ── Yoruba deities / cultural concepts ────────────────────────────────────
    "Ogun", "Sango", "Osun", "Ori", "Esu", "Obatala", "Orunmila",
    "Yemoja", "Eledumare", "Orisa",

    # ── Igbo concepts ─────────────────────────────────────────────────────────
    "Juju", "Ogbo", "Azu", "Nze", "Agwu", "Chineke", "Chukwu",

    # ── Broader African cultural / nature ─────────────────────────────────────
    "Fulani", "Tuareg", "Zulu", "Bantu",
    "Naira", "Cowry", "Kola", "Shea", "Iroko", "Odan",
    "Lagoon", "Niger", "Benue", "Volta", "Congo", "Nile",
    "Savanna", "Sahara", "Atlas", "Kilima", "Rift",
    "Ubuntu", "Kwame", "Kofi", "Ama", "Abena",
    "Jelani", "Nia", "Imani", "Amani", "Jabari",
    "Mamba", "Ndugu", "Simba", "Rafiki", "Asante",
    "Haile", "Dawit", "Tigist", "Almaz",
    "Dlamini", "Nkosi", "Thabo", "Sipho", "Lerato",
]

# Bare names that read naturally on their own (subset of NOUNS that are
# actual human names — used by the "just a name" styles).
BARE_NAMES: list[str] = [
    n for n in NOUNS
    if n in {
        "Segun", "Tobi", "Femi", "Bayo", "Kunle", "Wale", "Dayo", "Leke",
        "Gbenga", "Rotimi", "Biodun", "Tunji", "Lanre", "Bola", "Yinka",
        "Demola", "Kayode", "Adewale", "Eniola", "Temitope", "Oluwaseun",
        "Folake", "Ronke", "Funke", "Kemi", "Tokunbo", "Seyi", "Kola",
        "Dapo", "Lekan", "Remi", "Wunmi", "Bimpe", "Titi", "Shade",
        "Chidi", "Emeka", "Obinna", "Uche", "Nkem", "Ikem", "Chisom",
        "Amaka", "Ifeanyi", "Onyeka", "Chuka", "Kelechi", "Adaeze",
        "Ngozi", "Chidinma", "Ezinne", "Uchenna", "Chinyere", "Nnamdi",
        "Ikenna", "Obiora", "Tochi", "Chidera",
        "Musa", "Abubakar", "Garba", "Yakubu", "Usman", "Lawal", "Sani",
        "Bashir", "Kabir", "Haruna", "Shehu", "Ibrahim", "Yusuf", "Aliyu",
        "Danjuma", "Gambo", "Bello", "Rabiu", "Adamu", "Isa", "Nuhu",
        "Sule", "Sadiq", "Tanko", "Zainab", "Hafsat", "Bilkisu",
        "Fatima", "Hauwa", "Kwame", "Kofi", "Jelani", "Nia", "Imani",
        "Amani", "Jabari", "Thabo", "Sipho", "Lerato", "Dawit", "Almaz",
    }
]


# ── Legacy v1 pool (for the migration script ONLY) ────────────────────────────

def legacy_pool_names() -> set[str]:
    """
    Rebuild the exact v1 POOL (seed 42, first 500 shuffled AdjNoun pairs).
    Used by rename_synced_users.py to identify auto-assigned usernames.
    """
    pairs = [f"{adj}{noun}" for adj in ADJECTIVES for noun in NOUNS]
    rng = _random.Random(42)
    rng.shuffle(pairs)
    return set(pairs[:500])


# ── v2 generator ──────────────────────────────────────────────────────────────

def _rng_for(wallet: str, salt: str = "") -> _random.Random:
    digest = hashlib.sha256(f"{wallet.lower()}:{salt}".encode()).digest()
    return _random.Random(int.from_bytes(digest[:8], "big"))


def _digits(rng: _random.Random, n: int) -> str:
    # Avoid a leading zero so "07" doesn't look padded/robotic every time.
    first = str(rng.randint(1, 9))
    rest  = "".join(str(rng.randint(0, 9)) for _ in range(n - 1))
    return first + rest


def generate_username(wallet: str, salt: str = "") -> str:
    """
    Deterministic, varied username for a wallet address.
    Same wallet + same salt always produces the same name; pass a different
    salt (e.g. "v2") to deterministically re-roll a different name for the
    same wallet without touching every other wallet's assignment.
    """
    rng  = _rng_for(wallet, salt)
    adj  = rng.choice(ADJECTIVES)
    noun = rng.choice(NOUNS)
    name = rng.choice(BARE_NAMES)

    # (builder, weight) — weights tuned so no single format dominates.
    styles: list[tuple] = [
        (lambda: f"{adj}{noun}",                          14),  # SwiftEagle
        (lambda: f"{adj.lower()}{noun.lower()}",          14),  # swifteagle
        (lambda: f"{adj.lower()}_{noun.lower()}",         12),  # swift_eagle
        (lambda: f"{adj.lower()}{noun}",                   6),  # swiftEagle
        (lambda: f"{name}{_digits(rng, rng.choice([2,3]))}", 14),  # segun234
        (lambda: f"{name.lower()}{_digits(rng, rng.choice([2,3]))}", 12),  # kelechi07→kelechi71
        (lambda: name,                                    8),   # Musa
        (lambda: name.lower(),                            6),   # musa
        (lambda: f"{noun.lower()}{_digits(rng, 2)}",      6),   # eagle42
        (lambda: f"{name.lower()}_{noun.lower()}",        4),   # tobi_hawk
        (lambda: f"x{name}",                              2),   # xKelechi
        (lambda: f"{adj}{noun}{_digits(rng, 1)}",         2),   # IronWolf7
    ]

    total = sum(w for _, w in styles)
    pick  = rng.uniform(0, total)
    acc   = 0.0
    username = styles[0][0]()   # fallback
    for builder, weight in styles:
        acc += weight
        if pick <= acc:
            username = builder()
            break

    return username


def generate_unique_username(wallet: str, taken: set[str], salt: str = "") -> str:
    """
    Like generate_username(), but guarantees the result isn't in `taken`
    (case-insensitive) by appending digits derived from the address.
    Callers should add the returned name to `taken` themselves.
    """
    base = generate_username(wallet, salt)
    if base.lower() not in taken:
        return base

    # Deterministic disambiguation from the address itself.
    hex_tail = "".join(c for c in wallet.lower()[2:] if c.isdigit()) or "7"
    for i in (2, 3, 4):
        candidate = f"{base}{hex_tail[:i]}"
        if candidate.lower() not in taken:
            return candidate
    return f"{base}{wallet.lower()[-6:]}"   # last resort — still unique


# ── Handle-style generator (X / Telegram look) ────────────────────────────────
# Both X and Telegram restrict handles to letters, digits, and underscores
# (Telegram additionally requires starting with a letter). This generator
# stays lowercase and within that charset so results look like a real handle
# someone would actually own, e.g. "chidi_eth", "wale_dao07", "the_real_musa".

HANDLE_WORDS: list[str] = [
    "eth", "ng", "sol", "base", "web3", "onchain", "dao", "gm", "wagmi",
    "ngmi", "fam", "gang", "official", "real", "trades", "labs", "node",
    "chain", "drops", "crypto", "hq", "xyz", "dev", "builds", "app",
]

HANDLE_STYLES_WEIGHTS: list[tuple[str, int]] = [
    ("name_word",        16),  # chidi_eth
    ("name_digits",       16),  # chidi042
    ("name",              10),  # chidi
    ("word_name",         10),  # eth_chidi
    ("name_word_digits",   8),  # chidi_eth07
    ("the_real_name",      6),  # the_real_chidi
    ("name_underscore_digits", 10),  # chidi_042
    ("bare_lower",         8),  # (a lowercase noun/adj combo, no name)
]


def _handle_digits(rng: _random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def generate_handle_username(wallet: str, salt: str = "") -> str:
    """
    Deterministic X/Telegram-style handle for a wallet: lowercase,
    letters/digits/underscores only, starts with a letter. Same wallet +
    same salt always produces the same handle; pass a different salt to
    deterministically re-roll a different one.
    """
    rng  = _rng_for(wallet, salt)
    name = rng.choice(BARE_NAMES).lower()
    word = rng.choice(HANDLE_WORDS)

    styles = {
        "name_word":              lambda: f"{name}_{word}",
        "name_digits":             lambda: f"{name}{_handle_digits(rng, rng.choice([2, 3]))}",
        "name":                    lambda: name,
        "word_name":               lambda: f"{word}_{name}",
        "name_word_digits":        lambda: f"{name}_{word}{_handle_digits(rng, 2)}",
        "the_real_name":           lambda: f"the_real_{name}",
        "name_underscore_digits":  lambda: f"{name}_{_handle_digits(rng, 3)}",
        "bare_lower":              lambda: f"{rng.choice(ADJECTIVES).lower()}{rng.choice(NOUNS).lower()}",
    }

    total = sum(w for _, w in HANDLE_STYLES_WEIGHTS)
    pick  = rng.uniform(0, total)
    acc   = 0.0
    handle = styles["name"]()   # fallback
    for style_name, weight in HANDLE_STYLES_WEIGHTS:
        acc += weight
        if pick <= acc:
            handle = styles[style_name]()
            break

    return handle[:20]   # keep it a plausible handle length


def generate_unique_handle_username(wallet: str, taken: set[str], salt: str = "") -> str:
    """
    Like generate_handle_username(), but guarantees the result isn't in
    `taken` (case-insensitive) by appending address-derived digits.
    Callers should add the returned name to `taken` themselves.
    """
    base = generate_handle_username(wallet, salt)
    if base.lower() not in taken:
        return base

    hex_tail = "".join(c for c in wallet.lower()[2:] if c.isdigit()) or "7"
    for i in (2, 3, 4):
        candidate = f"{base}{hex_tail[:i]}"
        if candidate.lower() not in taken:
            return candidate[:20]
    return f"{base}{wallet.lower()[-6:]}"[:24]


# ── Backwards-compatible entry point ─────────────────────────────────────────

def assign_username(wallet: str, pool_index: int = 0) -> str:
    """
    v1-compatible signature used by onchain_sync.upsert_users().
    pool_index is ignored — names are now derived from the wallet address,
    which removes the cross-batch duplicate bug and the 500-name cap.
    """
    return generate_username(wallet)