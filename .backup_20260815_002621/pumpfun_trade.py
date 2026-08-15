#!/usr/bin/env python3
"""
Pump.fun ARRB Trading Script
Trades ARRB on pump.fun (Solana) using the pump.fun bonding curve program.

The agent's Solana wallet: CT5Z79b1ie7AaeRzEsn1uQjMz3p33xTJST7Na49UDoSL
ARRB mint: 2GL1Licg8692gW3ucgAoacJhhzackmcVVb2nAVmWpump
Bonding curve: 7FcANdwGUw1ZAZHiHqDkk4tobaDW1k6n6cgMxzKK2ify
Quote: USDC (EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v)

NOTE: The pump.fun bonding curve is currently PAUSED (mayhem_state: paused, low_stable_reserves)
This means trading may be temporarily halted until reserves increase.
"""

import json, struct, base64, time
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.message import Message
from solders.instruction import Instruction
from solders.hash import Hash
from solana.rpc.api import Client

# Constants
PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5DkDom8osMJimQToPxrr1g2X1oFZr2JRk")
ARRB_MINT = Pubkey.from_string("2GL1Licg8692gW3ucgAoacJhhzackmcVVb2nAVmWpump")
BONDING_CURVE = Pubkey.from_string("7FcANdwGUw1ZAZHiHqDkk4tobaDW1k6n6cgMxzKK2ify")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
ASSOCIATED_BONDING_CURVE = Pubkey.from_string("BNWAVTbxqSgixg1N4LpQJkvCArfVGUc28Nk9SvzSqAuv")
GLOBAL = Pubkey.from_string("ADwCkYyKGxF4i6fDeS7JNQXkR7m3PqyqyPp5p5p5p5p")
FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VVyAbGFm48yZpump")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p5PLyBnUdF3r6fQ5n2k2k2k2k2k2k2k2k")

# Load keypair
import os
keypair_path = os.path.expanduser("~/.config/solana/armabase-sol.json")
with open(keypair_path) as f:
    secret = json.load(f)
keypair = Keypair.from_bytes(bytes(secret))

WALLET = keypair.pubkey()
print(f"Agent Solana wallet: {WALLET}")

# RPC client
client = Client("https://api.mainnet-beta.solana.com")

def get_bonding_curve_state():
    """Get current bonding curve state from pump.fun API"""
    import requests
    resp = requests.get(f"https://frontend-api-v3.pump.fun/coins/{str(ARRB_MINT)}")
    data = resp.json()
    return {
        'price': float(data.get('market_cap', 0)) / 1e9,
        'market_cap_usd': float(data.get('usd_market_cap', 0)),
        'complete': data.get('complete', False),
        'mayhem_state': data.get('mayhem_state', 'unknown'),
        'virtual_sol_reserves': int(data.get('virtual_sol_reserves', 0)),
        'virtual_token_reserves': int(data.get('virtual_token_reserves', 0)),
        'real_sol_reserves': int(data.get('real_sol_reserves', 0)),
        'real_token_reserves': int(data.get('real_token_reserves', 0)),
    }

def get_token_balance(wallet, mint):
    """Get SPL token balance for a wallet"""
    import requests
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            str(wallet),
            {"mint": str(mint)},
            {"encoding": "jsonParsed"}
        ]
    }
    resp = requests.post("https://api.mainnet-beta.solana.com", json=payload)
    data = resp.json()
    accounts = data.get("result", {}).get("value", [])
    if accounts:
        amount = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return float(amount["uiAmount"] or 0)
    return 0.0

def check_status():
    """Check ARRB status on pump.fun"""
    state = get_bonding_curve_state()
    print(f"\n=== ARRB on pump.fun ===")
    print(f"Price: ${state['market_cap_usd'] / 1e9:.8f} per ARRB")
    print(f"Market cap: ${state['market_cap_usd']:,.2f}")
    print(f"Complete (graduated): {state['complete']}")
    print(f"Mayhem state: {state['mayhem_state']}")
    
    # Check our wallet balance
    import requests
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [str(WALLET)]
    }
    resp = requests.post("https://api.mainnet-beta.solana.com", json=payload)
    balance = resp.json().get("result", {}).get("value", 0) / 1e9
    print(f"\nOur SOL balance: {balance:.6f} SOL")
    
    arrb_balance = get_token_balance(WALLET, ARRB_MINT)
    print(f"Our ARRB balance: {arrb_balance:,.2f}")
    
    usdc_balance = get_token_balance(WALLET, USDC_MINT)
    print(f"Our USDC balance: {usdc_balance:.6f}")
    
    return state

if __name__ == "__main__":
    check_status()
