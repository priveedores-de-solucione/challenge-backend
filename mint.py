"""
mint_holders.py  —  Mint migrated DROPS balances to new contract
Run: RESOLVER_PRIVATE_KEY=0x... python mint_holders.py
"""

import os, json, time, logging
from web3 import Web3
from dotenv import load_dotenv
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
CELO_RPC      = os.getenv("CELO_RPC_URL", "https://rpc.ankr.com/celo")
PRIVATE_KEY   = os.getenv("RESOLVER_PRIVATE_KEY", "")
NEW_CONTRACT  = "0x213DF7A728E545BdAff8ff8c4BF9cFD7359Def0B"
PROGRESS_FILE = Path("mint_progress.json")
MINT_GAS      = 120_000

# ── Holders extracted from Celoscan CSV export ────────────────────────────────
# { address: amount_in_wei }
HOLDERS = {
    "0xedb7c8903bd013ec59959192b23099c47f9cabaf": 500000000000000000000,
    "0x89e7c6c96f35310ec7a4d0ea6ba5cc48a5d19a65": 450000000000000000000,
    "0x870a63d0d21800365257eadf4cc87c9671ec0f89": 330000000000000000000,
    "0x5ddcbd1e5422230d12902b7efcf4a641b1f2d195": 310000000000000000000,
    "0xf1db023ebce20305808930881598128d01b03a51": 280000000000000000000,
    "0xe5f0939984009ab5fc0a905cd424f009f34502d3": 230000000000000000000,
    "0xdf4793f38712862e21095b35392cf2326dfc962b": 170000000000000000000,
    "0xf8a83ff7803652dbe76222b88d41aefb40df88ac": 160000000000000000000,
    "0x59fecf2c999fe43e021884691f85b3682dbce372": 150000000000000000000,
    "0x2e6bb9bd26f4c0c841f23e5d65b4375364493100": 140000000000000000000,
    "0xe7edf84cede0a3b20e02a3b540312716ebe1a744": 130000000000000000000,
    "0x08d0d1572a8a714d90d670ea344dd23b1df565dd": 120000000000000000000,
    "0x9fbc2a0de6e5c5fd96e8d11541608f5f328c0785": 120000000000000000000,
    "0x7d24ce2686a734419483e171ab3634cb113f92a1":  90000000000000000000,
    "0xa4d0349ddeffee42afb019105cb55912f7b8e848":  80000000000000000000,
    "0x9d22beccb8fbb4325ce5c0ed94542a7fb8682f6e":  70000000000000000000,
    "0xa3f37aa708571d5844a2fc0ab8358112e6169842":  70000000000000000000,
    "0x7811d69e8f3a0e02370a339a0e119399a2f05a1d":  60000000000000000000,
    "0x0d616bab8bf99af9179ea36246e98db057df6c2a":  40000000000000000000,
    "0x3dee68fe24ac1c06aecc35eba816ce8efc85ba14":  30000000000000000000,
    "0x41a36d39004b87710adcdac19522095ee7855534":  30000000000000000000,
    "0x5bcb395c2a0afdd0049b65182fe5330a43150b19":  30000000000000000000,
    "0x95739c851b97f0b3e501635746ef407191b67fec":  30000000000000000000,
    "0xc459b6da6b375f2270413f1496f0eb5b4e2ab204":  30000000000000000000,
    "0xd59b83de618561c8ff4e98fc29a1b96abcbfb18a":  30000000000000000000,
    "0xb59666bd890011166b6c01dec293e3f7623ae738":  25000000000000000000,
    "0x3207d4728c32391405c7122e59ccb115a4af31ea":  20000000000000000000,
    "0x363878ddc13bbff36100b16335727b44cc2c3390":  20000000000000000000,
    "0xd37089671516b5cf37075910a9681b5a02223258":  20000000000000000000,
    "0x4ad271c54a88aeb0fbb5ddc57c1f63ed99d24fc1":  10000000000000000000,
    "0x595e0522379713e00572e7394fedecf5b549ef51":  10000000000000000000,
    "0x6cac76f9e8d6f55b3823d8aeadead970a5441b67":  10000000000000000000,
    "0x8b2d53b155d3553c031bdbb8d46557b5c78a8be1":  10000000000000000000,
    "0x9e73f63db077753466689025a42a0c0d43aee108":  10000000000000000000,
    "0xb13bb8b53d4b70748158f3770d46da70a228f5e7":  10000000000000000000,
    "0x807d1f1d46f4184a2c99d32ce84f7e1151e3acbc":  10000000000000000000,
    "0xe961066d859d4922b51269801cd26a2351c4f1e3":  10000000000000000000,
    "0x70ae80d576083f9c7a8d0b5f3a6a9c783b412db5":  10000000000000000000,
    "0x08cc7af66c296b6cb5972e53fddcc131f166471c":  10000000000000000000,
}

# ── ABI ───────────────────────────────────────────────────────────────────────
NEW_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to",     "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "mintTo",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ── Setup ─────────────────────────────────────────────────────────────────────
w3      = Web3(Web3.HTTPProvider(CELO_RPC))
account = w3.eth.account.from_key(PRIVATE_KEY)
new     = w3.eth.contract(address=Web3.to_checksum_address(NEW_CONTRACT), abi=NEW_ABI)

log.info("Connected : %s", w3.is_connected())
log.info("Signer    : %s", account.address)
log.info("Contract  : %s", NEW_CONTRACT)
log.info("Holders   : %d  |  Total: %.0f DROPS", len(HOLDERS), sum(HOLDERS.values()) / 1e18)

# ── Progress ──────────────────────────────────────────────────────────────────
def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        done = set(json.loads(PROGRESS_FILE.read_text()).get("minted", []))
        log.info("Resuming — %d already minted", len(done))
        return done
    return set()

def save_progress(minted: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps({"minted": list(minted)}, indent=2))

# ── Mint loop ─────────────────────────────────────────────────────────────────
def mint_all() -> None:
    minted   = load_progress()
    pending  = {Web3.to_checksum_address(a): b for a, b in HOLDERS.items()
                if a.lower() not in {x.lower() for x in minted}}
    total    = len(HOLDERS)
    skipped  = total - len(pending)
    failed: list[str] = []

    def get_gas_price() -> int:
        """Fetch current base fee and add 20% buffer to ensure acceptance."""
        base_fee = w3.eth.get_block("latest").get("baseFeePerGas", w3.to_wei(25, "gwei"))
        price    = int(base_fee * 1.2)
        log.info("Gas price: %.2f Gwei  (base %.2f + 20%%)", price / 1e9, base_fee / 1e9)
        return price

    log.info("To mint: %d  |  Already done: %d  |  Remaining: %d", total, skipped, len(pending))

    for i, (addr, amount_wei) in enumerate(pending.items(), 1):
        human = amount_wei / 1e18

        # Idempotency check
        try:
            if new.functions.balanceOf(addr).call() >= amount_wei:
                log.info("[%d/%d] SKIP %s — already funded", i + skipped, total, addr)
                minted.add(addr)
                save_progress(minted)
                continue
        except Exception:
            pass

        log.info("[%d/%d] mintTo(%s, %.0f DROPS)", i + skipped, total, addr, human)

        success   = False
        gas_price = get_gas_price()   # fresh price per address
        for attempt in range(5):
            try:
                nonce  = w3.eth.get_transaction_count(account.address, "pending")
                tx     = new.functions.mintTo(addr, amount_wei).build_transaction({
                    "from": account.address, "nonce": nonce,
                    "gas": MINT_GAS, "gasPrice": gas_price,
                })
                signed  = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

                if receipt["status"] != 1:
                    raise RuntimeError(f"reverted: {tx_hash.hex()}")

                log.info("  ✓ tx=%s", tx_hash.hex())
                minted.add(addr)
                save_progress(minted)
                success = True
                break

            except Exception as e:
                msg = str(e).lower()
                if "underpriced" in msg or "fee cap" in msg or "nonce" in msg:
                    gas_price = int(gas_price * 1.3)
                    log.warning("  gas/nonce issue (attempt %d) — bumping to %.2f Gwei",
                                attempt + 1, gas_price / 1e9)
                    time.sleep(2)
                    continue
                log.error("  ✗ attempt %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt)

        if not success:
            log.error("  GAVE UP on %s", addr)
            failed.append(addr)

        time.sleep(0.3)

    log.info("Done. Minted: %d  |  Failed: %d", len(minted) - skipped, len(failed))
    if failed:
        log.warning("Failed (re-run to retry): %s", failed)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not PRIVATE_KEY:
        raise SystemExit("Set RESOLVER_PRIVATE_KEY env var (or in .env file)")
    mint_all()