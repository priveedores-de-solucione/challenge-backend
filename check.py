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
DESTINATION_ADDRESS     = os.getenv("DESTINATION") # Define this in your .env
DERIVE_PATH             = "m/44'/60'/0'/0/{}"
START_INDEX             = int(os.getenv("START_INDEX", "1"))
END_INDEX               = int(os.getenv("END_INDEX", "1350"))

ESTIMATED_VOTE_COST_BOT = 0.0008 
WORKERS                 = 50
TRANSFER_GAS_LIMIT      = 21000

# Web3 connection
w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), "Cannot connect to RPC"

target_balance_wei = Web3.to_wei(ESTIMATED_VOTE_COST_BOT, "ether")

def get_wallet_info(index: int):
    try:
        acct = Account.from_mnemonic(SEED_PHRASE, account_path=DERIVE_PATH.format(index))
        bal_wei = w3.eth.get_balance(acct.address)
        return {
            "index": index,
            "account": acct,
            "address": acct.address,
            "balance": bal_wei
        }
    except Exception as e:
        print(f"Error checking index {index}: {e}")
        return None

def main():
    if not SEED_PHRASE:
        print("ERROR: BOT_SEED_PHRASE not found in .env")
        return
    if not DESTINATION_ADDRESS:
        print("ERROR: DESTINATION_ADDRESS not found in .env. Needed to drain leftover funds.")
        return

    print(f"Scanning indices {START_INDEX} to {END_INDEX}...")
    wallets = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(get_wallet_info, i) for i in range(START_INDEX, END_INDEX + 1)]
        for future in as_completed(futures):
            res = future.result()
            if res:
                wallets.append(res)
    
    # Sort by index to maintain logical order
    wallets.sort(key=lambda w: w['index'])

    # Get dynamic network gas price
    gas_price = w3.eth.gas_price
    gas_cost = TRANSFER_GAS_LIMIT * gas_price
    chain_id = w3.eth.chain_id

    print(f"Current network gas price: {w3.from_wei(gas_price, 'gwei')} Gwei")
    print(f"Transfer gas cost: {w3.from_wei(gas_cost, 'ether')} BOT")
    print("-" * 50)

    donors = []
    receivers = []

    # Segregate wallets
    for w in wallets:
        if w['balance'] > target_balance_wei + gas_cost:
            donors.append(w)
        elif w['balance'] < target_balance_wei:
            receivers.append(w)

    print(f"Donors found: {len(donors)}")
    print(f"Receivers needing funds: {len(receivers)}")
    print("=" * 50)

    # 1. Distribute excess from donors to receivers
    donor_idx = 0
    receiver_idx = 0

    while donor_idx < len(donors) and receiver_idx < len(receivers):
        donor = donors[donor_idx]
        receiver = receivers[receiver_idx]

        available = donor['balance'] - target_balance_wei - gas_cost
        if available <= 0:
            donor_idx += 1
            continue
        
        needed = target_balance_wei - receiver['balance']
        if needed <= 0:
            receiver_idx += 1
            continue

        send_amount = min(available, needed)

        print(f"Transferring {w3.from_wei(send_amount, 'ether'):.6f} BOT from {donor['address']} to {receiver['address']}...")
        
        tx = {
            'nonce': w3.eth.get_transaction_count(donor['address']),
            'to': receiver['address'],
            'value': send_amount,
            'gas': TRANSFER_GAS_LIMIT,
            'gasPrice': gas_price,
            'chainId': chain_id
        }
        
        try:
            signed_tx = donor['account'].sign_transaction(tx)
            # CHANGED TO raw_transaction FOR WEB3.PY V6
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction) 
            w3.eth.wait_for_transaction_receipt(tx_hash)
            print(f"  -> Success: {tx_hash.hex()}")

            # Update balances locally to reflect the move
            donor['balance'] -= (send_amount + gas_cost)
            receiver['balance'] += send_amount
        except Exception as e:
            print(f"  -> Failed: {e}")
            donor_idx += 1
            continue

    print("=" * 50)
    print("REDISTRIBUTION COMPLETE. CHECKING FOR LEFTOVERS...")
    print("=" * 50)

    # 2. Drain any receivers that couldn't be filled to the target
    for w in receivers:
        if w['balance'] < target_balance_wei and w['balance'] > gas_cost:
            drain_amount = w['balance'] - gas_cost
            print(f"Draining remaining {w3.from_wei(drain_amount, 'ether'):.6f} BOT from empty wallet {w['address']}...")
            
            tx = {
                'nonce': w3.eth.get_transaction_count(w['address']),
                'to': DESTINATION_ADDRESS,
                'value': drain_amount,
                'gas': TRANSFER_GAS_LIMIT,
                'gasPrice': gas_price,
                'chainId': chain_id
            }
            try:
                signed_tx = w['account'].sign_transaction(tx)
                # CHANGED TO raw_transaction FOR WEB3.PY V6
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                w3.eth.wait_for_transaction_receipt(tx_hash)
                print(f"  -> Drain Success: {tx_hash.hex()}")
                w['balance'] = 0
            except Exception as e:
                print(f"  -> Drain Failed: {e}")

    # 3. Drain any donors that still have excess (ensuring they only keep exactly 0.0008)
    for w in donors:
        excess = w['balance'] - target_balance_wei - gas_cost
        if excess > 0:
            print(f"Draining unused excess {w3.from_wei(excess, 'ether'):.6f} BOT from donor {w['address']}...")
            
            tx = {
                'nonce': w3.eth.get_transaction_count(w['address']),
                'to': DESTINATION_ADDRESS,
                'value': excess,
                'gas': TRANSFER_GAS_LIMIT,
                'gasPrice': gas_price,
                'chainId': chain_id
            }
            try:
                signed_tx = w['account'].sign_transaction(tx)
                # CHANGED TO raw_transaction FOR WEB3.PY V6
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                w3.eth.wait_for_transaction_receipt(tx_hash)
                print(f"  -> Drain Success: {tx_hash.hex()}")
                w['balance'] -= (excess + gas_cost)
            except Exception as e:
                print(f"  -> Drain Failed: {e}")

    print("=" * 50)
    print("ALL DONE.")

if __name__ == "__main__":
    main()