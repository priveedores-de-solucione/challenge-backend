"""
chain_utils.py
──────────────
Serialised on-chain transaction helpers.

ALL on-chain sends from this backend must go through `send_tx()`.
This guarantees:
  • One transaction is broadcast at a time PER (CHAIN, SIGNING ADDRESS).
    Each signer (resolver, each bot wallet, etc.) gets its own lock and
    its own tracked nonce sequence — different signers on the same chain
    never share or corrupt each other's nonce counters.
  • Nonce is fetched from the node only once per (chain, address), then
    tracked locally.
  • On "replacement transaction underpriced" the tx is retried with a
    bumped gas price (×1.20) up to MAX_RETRIES times.
  • On a nonce-conflict the nonce is re-fetched and the tx is rebuilt.
  • EIP-1559 and legacy (gasPrice) transactions are both supported.

Usage
-----
    from chain_utils import send_tx

    receipt = await send_tx(
        build_fn   = lambda nonce: contract.functions.foo(arg).build_transaction({
                         "from":  account.address,
                         "nonce": nonce,
                         "gas":   150_000,
                     }),
        account    = account,
        w3         = w3,
        chain_id   = 42220,   # REQUIRED — used to isolate nonce + lock
        label      = "foo()",
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable

from web3 import Web3
from web3.types import TxReceipt
from eth_account.signers.local import LocalAccount

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MAX_RETRIES     = 4
GAS_PRICE_BUMP  = 1.20
TX_TIMEOUT_SECS = 90

# ── Per-(chain, address) state ────────────────────────────────────────────────
# Key: (chain_id, address_lower)
#
# IMPORTANT: keyed by (chain_id, address) — NOT just chain_id.  A single chain
# can have many independent signers (the resolver wallet, and each bot wallet
# with its own private key). Each signer has its own on-chain nonce sequence,
# so sharing a single counter per chain_id causes transactions from one
# account to be broadcast using another account's nonce — a tx that then
# never gets mined (stuck ahead of that account's real nonce gap).

_chain_locks: dict[tuple[int, str], asyncio.Lock] = {}
_chain_nonces: dict[tuple[int, str], int | None] = {}   # None = not yet fetched


def _get_lock(chain_id: int, address: str) -> asyncio.Lock:
    key = (chain_id, address.lower())
    if key not in _chain_locks:
        _chain_locks[key] = asyncio.Lock()
    return _chain_locks[key]


def _next_nonce(chain_id: int, w3: Web3, address: str, force_refresh: bool = False) -> int:
    key = (chain_id, address.lower())
    current = _chain_nonces.get(key)
    if current is None or force_refresh:
        current = w3.eth.get_transaction_count(address, "pending")
        _chain_nonces[key] = current
        logger.debug("chain=%d addr=%s nonce refreshed from node: %d", chain_id, address, current)
    return current


def _consume_nonce(chain_id: int, address: str) -> None:
    key = (chain_id, address.lower())
    if _chain_nonces.get(key) is not None:
        _chain_nonces[key] += 1   # type: ignore[operator]


def _invalidate_nonce(chain_id: int, address: str) -> None:
    _chain_nonces[(chain_id, address.lower())] = None


# ── Gas helpers ───────────────────────────────────────────────────────────────

def _apply_gas(
    tx: dict,
    w3: Web3,
    base_fee_per_gas: int | None,
    priority: int,
    bump_multiplier: float = 1.0,
) -> dict:
    tx.pop("gasPrice", None)
    tx.pop("maxFeePerGas", None)
    tx.pop("maxPriorityFeePerGas", None)

    if base_fee_per_gas is not None:
        bumped_priority = int(priority * bump_multiplier)
        tx["maxPriorityFeePerGas"] = bumped_priority
        tx["maxFeePerGas"]         = int(2 * base_fee_per_gas * bump_multiplier) + bumped_priority
    else:
        tx["gasPrice"] = int(w3.eth.gas_price * bump_multiplier)

    return tx


# ── Main helper ───────────────────────────────────────────────────────────────

async def send_tx(
    build_fn:      Callable[[int], dict],
    account:       LocalAccount,
    w3:            Web3,
    chain_id:      int,
    label:         str = "tx",
    gas_price_wei: int | None = None,
) -> TxReceipt:
    """
    Build, sign, broadcast, and wait for a transaction.

    Serialised per (chain, signing address): Celo and Botchain each have
    their own lock/nonce tracker per signer, so concurrent sends from
    different accounts (resolver vs. bot wallets) on the same chain never
    interfere with each other's nonce sequence, while sends from the SAME
    account are still safely serialised.

    Parameters
    ----------
    build_fn        Callable accepting a nonce (int), returns unsigned tx dict.
    account         The signing account.
    w3              A connected Web3 instance for the correct chain.
    chain_id        Chain ID — used to select the right lock and nonce slot.
    label           Human-readable name used in log messages.
    gas_price_wei   Legacy override; ignored for EIP-1559 chains.
    """
    lock = _get_lock(chain_id, account.address)

    async with lock:
        loop = asyncio.get_event_loop()

        def _send_sync() -> TxReceipt:
            try:
                latest_block = w3.eth.get_block("latest")
                base_fee     = latest_block.get("baseFeePerGas")
            except Exception as _block_err:
                # POA chains (e.g. Botchain) have an oversized extraData field
                # that Web3.py rejects unless ExtraDataToPOAMiddleware is active.
                # If the middleware hasn't taken effect yet for this call path,
                # fall back to legacy gasPrice — the tx will still succeed.
                logger.debug("get_block('latest') failed (%s) — using legacy gasPrice", _block_err)
                base_fee = None

            priority_wei = gas_price_wei or (
                w3.eth.max_priority_fee if base_fee is not None else w3.eth.gas_price
            )
            bump = 1.0
            last_err: Exception | None = None

            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    bump *= GAS_PRICE_BUMP
                    logger.info(
                        "chain=%d %s retry %d/%d bump=%.2f",
                        chain_id, label, attempt, MAX_RETRIES - 1, bump,
                    )

                force_refresh = (attempt > 0)
                nonce = _next_nonce(chain_id, w3, account.address, force_refresh=force_refresh)

                try:
                    tx = build_fn(nonce)

                    if attempt > 0 and base_fee is not None:
                        try:
                            latest_block = w3.eth.get_block("latest")
                            base_fee = latest_block.get("baseFeePerGas", base_fee)
                        except Exception as _rb_err:
                            logger.debug("retry get_block failed (%s) — keeping base_fee=None", _rb_err)
                            base_fee = None

                    _apply_gas(tx, w3, base_fee, priority_wei, bump_multiplier=bump)

                    logger.debug(
                        "chain=%d %s gas fields: %s",
                        chain_id, label,
                        {k: tx[k] for k in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas") if k in tx},
                    )

                    signed  = account.sign_transaction(tx)
                    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                    logger.info("chain=%d %s broadcast nonce=%d hash=%s", chain_id, label, nonce, tx_hash.hex())

                    _consume_nonce(chain_id, account.address)

                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT_SECS)
                    if receipt["status"] != 1:
                        raise RuntimeError(f"chain={chain_id} {label} reverted: {tx_hash.hex()}")

                    logger.info("chain=%d %s confirmed hash=%s", chain_id, label, tx_hash.hex())
                    return receipt

                except Exception as exc:
                    msg = str(exc).lower()
                    last_err = exc

                    if "replacement transaction underpriced" in msg or "underpriced" in msg:
                        logger.warning("chain=%d %s underpriced (attempt %d) — bumping gas", chain_id, label, attempt)
                        continue

                    if "nonce too low" in msg or "already known" in msg:
                        logger.warning("chain=%d %s nonce conflict (attempt %d) — refreshing", chain_id, label, attempt)
                        _invalidate_nonce(chain_id, account.address)
                        continue

                    raise

            raise RuntimeError(
                f"chain={chain_id} {label} failed after {MAX_RETRIES} attempts: {last_err}"
            ) from last_err

        return await loop.run_in_executor(None, _send_sync)


# ── Backwards-compat alias used by quiz_engine.build_w3() ────────────────────

def build_w3(chain_id: int | None = None):
    """
    Convenience wrapper kept for callers that still import build_w3 from
    chain_utils.  Delegates to chain_config.build_w3_for_chain().
    """
    from chain_config import build_w3_for_chain, DEFAULT_CHAIN_ID
    return build_w3_for_chain(chain_id or DEFAULT_CHAIN_ID)