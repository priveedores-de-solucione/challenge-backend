import os
import time
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()
Account.enable_unaudited_hdwallet_features()

# Configuration
RPC_URL        = "https://rpc.botchain.ai"
SEED_PHRASE    = os.getenv("BOT_SEED_PHRASE")
FUNDER_KEY     = os.getenv("RESOLVER_PRIVATE_KEY") # Private key of the wallet with funds
DERIVE_PATH    = "m/44'/60'/0'/0/{}"

START_INDEX    = 1
END_INDEX      = 300

THRESHOLD_BOT  = 0.0008
AMOUNT_TO_SEND = 0.0008
WORKERS        = 100 # High number of workers for extreme speed

w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), "Cannot connect to RPC"

threshold_wei = Web3.to_wei(THRESHOLD_BOT, "ether")
amount_wei = Web3.to_wei(AMOUNT_TO_SEND, "ether")

def check_wallet(index: int):
    """Derive account and check balance."""
    try:
        acct = Account.from_mnemonic(SEED_PHRASE, account_path=DERIVE_PATH.format(index))
        bal_wei = w3.eth.get_balance(acct.address)
        return {
            "index": index,
            "address": acct.address,
            "needs_funding": bal_wei < threshold_wei,
            "bal": bal_wei
        }
    except Exception as e:
        print(f"Error checking index {index}: {e}")
        return None

def send_funding_tx(funder_acct, target_address, nonce, gas_price, chain_id):
    """Sign and broadcast the transaction using a pre-assigned nonce."""
    tx = {
        'nonce': nonce,
        'to': target_address,
        'value': amount_wei,
        'gas': 21000, 
        'gasPrice': gas_price,
        'chainId': chain_id
    }
    
    try:
        signed_tx = funder_acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        return {"success": True, "address": target_address, "hash": tx_hash.hex(), "nonce": nonce}
    except Exception as e:
        return {"success": False, "address": target_address, "error": str(e), "nonce": nonce}

def main():
    if not FUNDER_KEY or not SEED_PHRASE:
        print("ERROR: Missing RESOLVER_PRIVATE_KEY or BOT_SEED_PHRASE_2 in .env")
        return

    funder = Account.from_key(FUNDER_KEY)
    print(f"Funding Wallet: {funder.address}")
    
    funder_bal = w3.eth.get_balance(funder.address)
    print(f"Funder Balance: {w3.from_wei(funder_bal, 'ether')} BOT\n")

    print(f"--- PHASE 1: Scanning {END_INDEX - START_INDEX + 1} wallets concurrently ---")
    start_time = time.time()
    
    wallets_to_fund = []
    
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(check_wallet, i) for i in range(START_INDEX, END_INDEX + 1)]
        
        for i, future in enumerate(as_completed(futures), 1):
            res = future.result()
            if res and res["needs_funding"]:
                wallets_to_fund.append(res)
            
            # Progress tracker
            if i % 1000 == 0:
                print(f"Scanned {i} / {END_INDEX - START_INDEX + 1} wallets...")

    print(f"Scan complete in {time.time() - start_time:.2f} seconds.")
    print(f"Total wallets needing funding: {len(wallets_to_fund)}\n")

    if not wallets_to_fund:
        print("No wallets need funding. Exiting.")
        return

    # Check if funder has enough balance
    total_needed = amount_wei * len(wallets_to_fund)
    if funder_bal < total_needed:
        print(f"WARNING: Funder balance ({w3.from_wei(funder_bal, 'ether')} BOT) is less than required ({w3.from_wei(total_needed, 'ether')} BOT + gas)!")
        return

    print("--- PHASE 2: Broadcasting Transactions ---")
    chain_id = w3.eth.chain_id
    gas_price = w3.eth.gas_price
    start_nonce = w3.eth.get_transaction_count(funder.address)
    
    # Pre-assign nonces to avoid thread collisions
    for i, wallet in enumerate(wallets_to_fund):
        wallet["assigned_nonce"] = start_nonce + i

    successful_txs = 0
    failed_txs = 0

    broadcast_start_time = time.time()

    # Blast transactions concurrently
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [
            executor.submit(send_funding_tx, funder, w["address"], w["assigned_nonce"], gas_price, chain_id)
            for w in wallets_to_fund
        ]
        
        for future in as_completed(futures):
            res = future.result()
            if res["success"]:
                print(f"[Nonce {res['nonce']}] ✓ Sent to {res['address']} (Hash: {res['hash']})")
                successful_txs += 1
            else:
                print(f"[Nonce {res['nonce']}] ✗ Failed for {res['address']}: {res['error']}")
                failed_txs += 1

    total_time = time.time() - start_time
    print("\n" + "="*50)
    print(f"DONE! Processed in {total_time:.2f} seconds.")
    print(f"Successfully funded: {successful_txs}")
    print(f"Failed transactions: {failed_txs}")
    print("="*50)

if __name__ == "__main__":
    main()