"""
chain_config.py
───────────────
Single source of truth for per-chain contract addresses and RPC URLs.
Mirror of the frontend lib/chain-config.ts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from web3 import Web3
    from eth_account.signers.local import LocalAccount


@dataclass(frozen=True)
class ChainConfig:
    id: int
    name: str
    rpc_url: str
    explorer_url: str
    drops_token: str
    quiz_hub: str
    drops_redeem_pool: Optional[str]
    g_token: Optional[str]        # only Celo has $G for now
    enabled: bool = True


CELO_CHAIN_ID     = 42220
BOTCHAIN_CHAIN_ID = 677  # update when Botchain assigns final ID

CHAINS: dict[int, ChainConfig] = {
    CELO_CHAIN_ID: ChainConfig(
        id                = CELO_CHAIN_ID,
        name              = "Celo Mainnet",
        rpc_url           = os.getenv("CELO_RPC_URL", "https://forno.celo.org"),
        explorer_url      = "https://celoscan.io",
        drops_token       = os.getenv(
            "DROPS_TOKEN_CONTRACT_CELO",
            os.getenv("DROPS_TOKEN_CONTRACT", ""),   # fallback to old single-chain var
        ),
        quiz_hub          = os.getenv(
            "QUIZ_HUB_CONTRACT_CELO",
            os.getenv("QUIZ_HUB_CONTRACT", ""),
        ),
        drops_redeem_pool = os.getenv(
            "DROPS_REDEEM_POOL_CELO",
            os.getenv("DROPS_REDEEM_POOL", ""),
        ),
        g_token           = os.getenv("G_TOKEN_CONTRACT", ""),
    ),
    BOTCHAIN_CHAIN_ID: ChainConfig(
        id                = BOTCHAIN_CHAIN_ID,
        name              = "Botchain",
        rpc_url           = os.getenv("BOTCHAIN_RPC_URL", "https://rpc.botchain.ai"),
        explorer_url      = os.getenv("BOTCHAIN_EXPLORER_URL", "https://scan.botchain.ai"),
        drops_token       = os.getenv("DROPS_TOKEN_CONTRACT_BOTCHAIN", "0xBAd791F200f1F8Fb639d83125FcF732F5f6eCD03"),
        quiz_hub          = os.getenv("QUIZ_HUB_CONTRACT_BOTCHAIN", "0xE7F217A8447087600C1CBFb63192586edFa619fe"),
        drops_redeem_pool = os.getenv("DROPS_REDEEM_POOL_BOTCHAIN", "0x9825670865B896738CF8E6c98d093aD5b40F0A11"),   # None = not deployed yet
        g_token           = os.getenv("G_TOKEN_CONTRACT_BOTCHAIN", "0xD5452816194a3784dBa983426cCe7c122F4abd30"),    # $G is Celo-only
        enabled           = bool(os.getenv("BOTCHAIN_RPC_URL")),  # only enabled when RPC is configured
    ),
}

DEFAULT_CHAIN_ID = CELO_CHAIN_ID


def get_chain(chain_id: int | None) -> ChainConfig:
    """Return chain config, falling back to Celo if unknown or disabled."""
    if chain_id and chain_id in CHAINS and CHAINS[chain_id].enabled:
        return CHAINS[chain_id]
    return CHAINS[DEFAULT_CHAIN_ID]


def is_supported(chain_id: int | None) -> bool:
    return bool(chain_id and chain_id in CHAINS and CHAINS[chain_id].enabled)


def build_w3_for_chain(chain_id: int | None) -> tuple["Web3", "LocalAccount"]:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    from eth_account import Account

    cfg = get_chain(chain_id)
    if not cfg.rpc_url:
        raise RuntimeError(f"No RPC URL configured for chain {chain_id} ({cfg.name})")

    w3      = Web3(Web3.HTTPProvider(cfg.rpc_url))
    account = Account.from_key(os.getenv("RESOLVER_PRIVATE_KEY", ""))

    # Botchain is a POA chain — extraData in block headers exceeds the standard
    # 32 bytes, which Web3.py rejects without this middleware.
    if cfg.id == BOTCHAIN_CHAIN_ID:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    return w3, account