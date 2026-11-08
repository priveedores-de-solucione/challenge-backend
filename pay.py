import os
import time
import logging
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()
Account.enable_unaudited_hdwallet_features()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

START_INDEX    = int(os.getenv("START_INDEX", "1"))
END_INDEX      = int(os.getenv("END_INDEX", "1350"))
RPC_URL        = "https://rpc.botchain.ai"
CONTRACT_ADDR  = "0xF5b22756CbD9193CE6F349bbD1835c2e5a6Cb181"
SEED_PHRASE    = os.getenv("BOT_SEED_PHRASE_2")
PROJECT_ID     = int(os.getenv("PROJECT_ID", "19")) #497,392,39,173,324
DERIVE_PATH    = "m/44'/60'/0'/0/{}"
GAS_LIMIT_VOTE = 80000
MIN_GAS_BAL    = Web3.to_wei(0.00075, "ether")
WORKERS        = 50

VOTE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "projectID", "type": "uint256"}],
        "name": "vote",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "voter", "type": "address"}],
        "name": "nextVoteAt",
        "outputs": [{"internalType": "uint256", "name": "timestamp", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), "Cannot connect to RPC"

contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDR), abi=VOTE_ABI)


def derive_account(index):
    return Account.from_mnemonic(SEED_PHRASE, account_path=DERIVE_PATH.format(index))


def get_next_vote_at(addr):
    return contract.functions.nextVoteAt(Web3.to_checksum_address(addr)).call()


def do_vote(account):
    nonce = w3.eth.get_transaction_count(account.address)
    
    # We remove the hardcoded "gas" parameter here.
    # web3.py will automatically estimate the exact gas needed.
    tx = contract.functions.vote(PROJECT_ID).build_transaction({
        "from": account.address,
        "gasPrice": w3.eth.gas_price,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
    })
    
    signed = account.sign_transaction(tx)
    return w3.eth.send_raw_transaction(signed.raw_transaction).hex()

def process_wallet(index, acct, now):
    try:
        next_vote_at = get_next_vote_at(acct.address)
        if next_vote_at > now:
            return ("cooldown", index, next_vote_at)

        balance = w3.eth.get_balance(acct.address)
        if balance < MIN_GAS_BAL:
            log.warning(f"[{index}] {acct.address} — low gas ({w3.from_wei(balance, 'ether')} BOT), skip")
            return ("skip", index, None)

        tx_hash = do_vote(acct)
        return ("sent", index, tx_hash)

    except Exception as e:
        log.error(f"[{index}] Error: {e}")
        return ("error", index, None)


def confirm_batch(sent_items):
    def confirm(item):
        index, tx_hash = item
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                log.info(f"[{index}] ✓ confirmed: {tx_hash}")
            else:
                log.error(f"[{index}] ✗ failed: {tx_hash}")
        except Exception as e:
            log.error(f"[{index}] Confirm error: {e}")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(confirm, item) for item in sent_items]
        for f in as_completed(futures):
            f.result()


def main():
    wallets = [(i, derive_account(i)) for i in range(START_INDEX, END_INDEX + 1)]
    log.info(f"Loaded {len(wallets)} wallets ({START_INDEX}–{END_INDEX})")

    while True:
        now = int(time.time())
        next_wake = None
        sent = []

        # Phase 1: check cooldown + send all votes concurrently
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(process_wallet, i, acct, now): i for i, acct in wallets}
            for future in as_completed(futures):
                status, index, value = future.result()
                if status == "cooldown":
                    if next_wake is None or value < next_wake:
                        next_wake = value
                elif status == "sent":
                    sent.append((index, value))

        log.info(f"Sent {len(sent)} votes — confirming...")

        # Phase 2: confirm all concurrently
        if sent:
            confirm_batch(sent)

        now = int(time.time())
        if next_wake and next_wake > now:
            wait_secs = next_wake - now + 5
            wake_time = time.strftime("%H:%M:%S", time.localtime(next_wake))
            log.info(f"All on cooldown. Sleeping {wait_secs}s until {wake_time}...")
            time.sleep(wait_secs)
        else:
            time.sleep(2)


if __name__ == "__main__":
    main()