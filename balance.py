import os
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()
Account.enable_unaudited_hdwallet_features()

# Configuration
RPC_URL                 = "https://rpc.botchain.ai"
SEED_PHRASE             = os.getenv("BOT_SEED_PHRASE_2")
DERIVE_PATH             = "m/44'/60'/0'/0/{}"
START_INDEX             = int(os.getenv("START_INDEX", "1"))
END_INDEX               = int(os.getenv("END_INDEX", "1350"))

# The estimated gas cost of a single vote
ESTIMATED_VOTE_COST_BOT = 0.0008 
WORKERS                 = 50

# Web3 connection
w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), "Cannot connect to RPC"

vote_cost_wei = Web3.to_wei(ESTIMATED_VOTE_COST_BOT, "ether")

def get_wallet_vote_capacity(index: int):
    try:
        acct = Account.from_mnemonic(SEED_PHRASE, account_path=DERIVE_PATH.format(index))
        bal_wei = w3.eth.get_balance(acct.address)
        
        # Floor division (//) calculates exactly how many full votes the wallet can afford
        possible_votes = bal_wei // vote_cost_wei
        bal_bot = float(w3.from_wei(bal_wei, "ether"))
        
        return {
            "index": index, 
            "address": acct.address, 
            "balance": bal_bot, 
            "votes": possible_votes
        }
    except Exception as e:
        print(f"Error checking index {index}: {e}")
        return None

def main():
    if not SEED_PHRASE:
        print("ERROR: BOT_SEED_PHRASE not found in .env")
        return

    print(f"Scanning indices {START_INDEX} to {END_INDEX}...")
    print(f"Estimated cost per vote: {ESTIMATED_VOTE_COST_BOT} BOT")
    print("-" * 50)

    total_possible_votes = 0
    wallets_with_votes = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(get_wallet_vote_capacity, i) for i in range(START_INDEX, END_INDEX + 1)]
        
        for future in as_completed(futures):
            res = future.result()
            if res:
                if res["votes"] > 0:
                    wallets_with_votes += 1
                    total_possible_votes += res["votes"]
                    # Optional: Un-comment the line below if you want to see every wallet that has votes
                    # print(f"[{res['index']}] {res['address']} - Bal: {res['balance']:.6f} BOT -> {res['votes']} votes")

    print("=" * 50)
    print("SCAN COMPLETE")
    print("=" * 50)
    print(f"Total Wallets with enough gas: {wallets_with_votes}")
    print(f"TOTAL POSSIBLE VOTES:        {total_possible_votes}")
    print("=" * 50)

if __name__ == "__main__":
    main()