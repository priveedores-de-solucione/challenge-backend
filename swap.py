# redistribute_vote.py
import os
import sys
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

load_dotenv()
Account.enable_unaudited_hdwallet_features()

RPC_URL         = "https://rpc.botchain.ai"
HEATVOTE_ADDR   = Web3.to_checksum_address("0xF5b22756CbD9193CE6F349bbD1835c2e5a6Cb181")
VOTE_PROJECT_ID = 392
DERIVATION_BASE = "m/44'/60'/0'/0"
DERIVE_UP_TO    = 6300
GAS_PRICE_GWEI  = 20
VOTE_GAS        = 80_000
TRANSFER_GAS    = 21_000
GAS_BUFFER      = 1.2
MIN_BOT_TO_VOTE = Web3.to_wei(0.0007, "ether")
CHECKPOINT_FILE = "redistribute_checkpoint.json"
WORKERS         = 20

HEATVOTE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "projectID", "type": "uint256"}],
        "name": "vote",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

cp_lock = threading.Lock()

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"scanned": False, "senders": [], "receivers": [], "can_vote": [],
            "transferred": [], "voted": [], "already_voted": [], "failed": []}

def save_checkpoint(cp):
    with cp_lock:
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump(cp, f, indent=2)

def scan_wallet(args):
    i, mnemonic, w3, gas_price = args
    path = f"{DERIVATION_BASE}/{i}"
    acct = Account.from_mnemonic(mnemonic, account_path=path)
    addr = acct.address

    try:
        bot_bal = w3.eth.get_balance(addr)
    except Exception as e:
        print(f"[{i}] {addr} — balance error: {e}")
        return None

    vote_cost     = int(VOTE_GAS * gas_price * GAS_BUFFER)
    transfer_cost = TRANSFER_GAS * gas_price

    if bot_bal >= vote_cost:
        return {"type": "can_vote", "index": i, "address": addr, "bot": bot_bal}

    if bot_bal >= (vote_cost + transfer_cost):
        # has enough to vote AND send surplus to someone else
        return {"type": "sender", "index": i, "address": addr, "bot": bot_bal,
                "surplus": bot_bal - transfer_cost}

    if MIN_BOT_TO_VOTE <= bot_bal < vote_cost:
        return {"type": "receiver", "index": i, "address": addr, "bot": bot_bal}

    if bot_bal < MIN_BOT_TO_VOTE:
        return {"type": "receiver", "index": i, "address": addr, "bot": bot_bal}

    return None

def try_vote(i, mnemonic, w3, heatvote, gas_price, chain_id):
    """
    Returns:
      'voted'        — tx succeeded
      'already_voted'— tx reverted (contract rejected, likely already voted)
      'failed'       — other error
    """
    path = f"{DERIVATION_BASE}/{i}"
    acct = Account.from_mnemonic(mnemonic, account_path=path)
    addr = acct.address
    try:
        nonce   = w3.eth.get_transaction_count(addr)
        tx      = heatvote.functions.vote(VOTE_PROJECT_ID).build_transaction({
            "from": addr, "nonce": nonce,
            "gas": VOTE_GAS, "gasPrice": gas_price, "chainId": chain_id,
        })
        signed  = w3.eth.account.sign_transaction(tx, acct.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            print(f"[{i}] ✓ Voted — tx: {tx_hash.hex()}")
            return "voted", tx_hash.hex(), addr
        else:
            print(f"[{i}] already voted (reverted) — tx: {tx_hash.hex()}")
            return "already_voted", tx_hash.hex(), addr

    except Exception as e:
        err = str(e)
        if "revert" in err.lower() or "execution reverted" in err.lower():
            print(f"[{i}] already voted (revert): {err}")
            return "already_voted", None, addr
        print(f"[{i}] ✗ Vote error: {err}")
        return "failed", None, addr

def main():
    mnemonic = os.getenv("BOT_SEED_PHRASE")
    if not mnemonic:
        print("ERROR: SEED_PHRASE not set")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("ERROR: Cannot connect to RPC")
        sys.exit(1)

    chain_id      = w3.eth.chain_id
    gas_price     = Web3.to_wei(GAS_PRICE_GWEI, "gwei")
    heatvote      = w3.eth.contract(address=HEATVOTE_ADDR, abi=HEATVOTE_ABI)
    vote_cost     = int(VOTE_GAS * gas_price * GAS_BUFFER)
    transfer_cost = TRANSFER_GAS * gas_price

    cp = load_checkpoint()
    cp.setdefault("can_vote", [])
    cp.setdefault("already_voted", [])

    # ── SCAN ─────────────────────────────────────────────────────────────────
    if not cp["scanned"]:
        print("=" * 50)
        print(f"SCANNING {DERIVE_UP_TO} wallets ({WORKERS} workers)...")
        print("=" * 50)

        senders, receivers, can_vote = [], [], []
        scan_args = [(i, mnemonic, w3, gas_price) for i in range(DERIVE_UP_TO)]

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(scan_wallet, a): a[0] for a in scan_args}
            for f in as_completed(futures):
                result = f.result()
                if result is None:
                    continue
                t = result["type"]
                if t == "sender":
                    senders.append(result)
                elif t == "receiver":
                    receivers.append(result)
                elif t == "can_vote":
                    can_vote.append(result)

        cp.update({"senders": senders, "receivers": receivers,
                   "can_vote": can_vote, "scanned": True})
        save_checkpoint(cp)
        print(f"Scan done — can_vote: {len(can_vote)}, senders: {len(senders)}, receivers: {len(receivers)}")
    else:
        senders  = cp["senders"]
        receivers = cp["receivers"]
        can_vote  = cp.get("can_vote", [])
        print(f"Loaded checkpoint — can_vote: {len(can_vote)}, senders: {len(senders)}, receivers: {len(receivers)}")

    done_addrs      = {v["address"] for v in cp["voted"]}
    done_addrs     |= {v["address"] for v in cp.get("already_voted", [])}
    already_transferred = {r["to"] for r in cp.get("transferred", [])}

    # ── PASS 1: try voting all wallets that have enough BOT ──────────────────
    print("\n" + "=" * 50)
    print(f"PASS 1 — Attempting votes on {len(can_vote) + len(senders)} wallets with enough BOT")
    print("=" * 50)

    for w_info in (can_vote + senders):
        addr = w_info["address"]
        if addr in done_addrs:
            continue
        status, tx_hash, addr = try_vote(w_info["index"], mnemonic, w3, heatvote, gas_price, chain_id)
        entry = {"index": w_info["index"], "address": addr, "tx": tx_hash}
        if status == "voted":
            cp["voted"].append(entry)
        elif status == "already_voted":
            cp["already_voted"].append(entry)
        done_addrs.add(addr)
        save_checkpoint(cp)

    # ── PASS 2: redistribute BOT from already_voted → receivers ─────────────
    print("\n" + "=" * 50)
    print(f"PASS 2 — Redistributing from already-voted senders to receivers")
    print("=" * 50)

    # senders = wallets that had enough BOT AND are now confirmed already_voted
    confirmed_senders = [
        s for s in (can_vote + senders)
        if s["address"] in {v["address"] for v in cp.get("already_voted", [])}
    ]
    pending_receivers = [
        r for r in receivers
        if r["address"] not in already_transferred and r["address"] not in done_addrs
    ]

    pairs = list(zip(confirmed_senders, pending_receivers))
    print(f"Confirmed senders: {len(confirmed_senders)}, receivers: {len(pending_receivers)}, pairs: {len(pairs)}")

    for sender, receiver in pairs:
        s_addr = sender["address"]
        r_addr = receiver["address"]
        s_idx  = sender["index"]
        r_idx  = receiver["index"]

        if r_addr in already_transferred:
            continue

        s_bal = w3.eth.get_balance(s_addr)
        send_amount = s_bal - transfer_cost
        if send_amount <= 0:
            print(f"[{s_idx}] sender exhausted, skipping")
            continue

        r_bal          = w3.eth.get_balance(r_addr)
        needed         = max(vote_cost - r_bal, 0)
        amount_to_send = min(send_amount, needed + transfer_cost)

        if amount_to_send <= 0:
            continue

        print(f"[{s_idx}→{r_idx}] Sending {Web3.from_wei(amount_to_send, 'ether')} BOT → {r_addr}")

        s_acct = Account.from_mnemonic(mnemonic, account_path=f"{DERIVATION_BASE}/{s_idx}")
        try:
            nonce   = w3.eth.get_transaction_count(s_addr)
            tx      = {
                "to": r_addr, "value": amount_to_send,
                "gas": TRANSFER_GAS, "gasPrice": gas_price,
                "nonce": nonce, "chainId": chain_id,
            }
            signed  = w3.eth.account.sign_transaction(tx, s_acct.key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status != 1:
                print(f"  ✗ Transfer reverted")
                cp["failed"].append({"from": s_addr, "to": r_addr, "reason": "transfer reverted"})
                save_checkpoint(cp)
                continue

            print(f"  ✓ Transferred — tx: {tx_hash.hex()}")
            cp["transferred"].append({"from": s_addr, "to": r_addr, "tx": tx_hash.hex()})
            already_transferred.add(r_addr)
            save_checkpoint(cp)

        except Exception as e:
            print(f"  ✗ Transfer error: {e}")
            cp["failed"].append({"from": s_addr, "to": r_addr, "error": str(e)})
            save_checkpoint(cp)
            continue

        status, tx_hash, _ = try_vote(r_idx, mnemonic, w3, heatvote, gas_price, chain_id)
        entry = {"index": r_idx, "address": r_addr, "tx": tx_hash}
        if status == "voted":
            cp["voted"].append(entry)
            done_addrs.add(r_addr)
        elif status == "already_voted":
            cp["already_voted"].append(entry)
            done_addrs.add(r_addr)
        else:
            cp["failed"].append({"voter": r_addr, "reason": "vote failed after transfer"})
        save_checkpoint(cp)

    # ── SUMMARY ──────────────────────────────────────────────────────────────
    print("\n========== RESULTS ==========")
    print(f"Voted fresh    : {len(cp['voted'])}")
    print(f"Already voted  : {len(cp.get('already_voted', []))}")
    print(f"Transferred    : {len(cp['transferred'])}")
    print(f"Failed         : {len(cp['failed'])}")

    if cp["voted"]:
        print("\nFresh votes:")
        for v in cp["voted"]:
            print(f"  [{v['index']}] {v['address']} — tx: {v['tx']}")

    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("\nCheckpoint cleared.")

if __name__ == "__main__":
    main()