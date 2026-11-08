import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv()
Account.enable_unaudited_hdwallet_features()

MNEMONIC = os.environ["BOT_SEED_PHRASE"]
DESTINATION = Web3.to_checksum_address(os.environ["DESTINATION"])
START = int(os.getenv("START_INDEX",600))
END = int(os.getenv("END_INDEX", 699))  # exclusive: 800..999 = 200 wallets
WORKERS = int(os.getenv("WORKERS", 20))
DRY_RUN = "--dry" in sys.argv

w3 = Web3(Web3.HTTPProvider(os.environ["BOTCHAIN_RPC_URL"], request_kwargs={"timeout": 30}))

gas_price = w3.eth.gas_price
gas_limit = 21000
gas_cost = gas_price * gas_limit
chain_id = w3.eth.chain_id


def sweep_one(i):
    acct = Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{i}")
    try:
        balance = w3.eth.get_balance(acct.address)
        if balance <= gas_cost:
            return i, 0, f"[{i}] {acct.address} skip ({w3.from_wei(balance, 'ether')})"

        value = balance - gas_cost

        if DRY_RUN:
            return i, value, f"[{i}] {acct.address} would send {w3.from_wei(value, 'ether')}"

        tx = {
            "to": DESTINATION,
            "value": value,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
            "chainId": chain_id,
        }
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        return i, value, f"[{i}] sent {w3.from_wei(value, 'ether')} | {tx_hash.hex()}"
    except Exception as e:
        return i, 0, f"[{i}] {acct.address} failed: {e}"


total = 0
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = [pool.submit(sweep_one, i) for i in range(START, END)]
    for f in as_completed(futures):
        _, value, msg = f.result()
        total += value
        print(msg)

print(f"\nTotal {'to sweep' if DRY_RUN else 'swept'}: {w3.from_wei(total, 'ether')} BOT")