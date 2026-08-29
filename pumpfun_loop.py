#!/usr/bin/env python3
"""
Pump.fun ARRB Trading Loop
Cycles buys and sells to generate trading volume on the pump.fun bonding curve.
"""

import json, base64, requests, time, sys
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# Load keypair
with open("/home/hermes/.config/solana/armabase-sol.json") as f:
    secret = json.load(f)
keypair = Keypair.from_bytes(bytes(secret))
WALLET = str(keypair.pubkey())

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ARRB = "2GL1Licg8692gW3ucgAoacJhhzackmcVVb2nAVmWpump"
RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"

def get_balance(mint):
    """Get token balance"""
    resp = requests.post(RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accounts = resp.json().get("result", {}).get("value", [])
    if accounts:
        amt = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return float(amt["uiAmount"] or 0)
    return 0.0

def get_sol_balance():
    resp = requests.post(RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [WALLET]
    }, timeout=15)
    return resp.json().get("result", {}).get("value", 0) / 1e9

def jup_swap(input_mint, output_mint, amount, slippage=500):
    """Execute a Jupiter swap. Returns (success, tx_sig, details)"""
    # Get quote
    resp = requests.get(JUP_QUOTE, params={
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage),
    }, timeout=15)
    quote = resp.json()
    if "error" in quote:
        return False, "", f"Quote error: {quote['error']}"
    
    out_amount = int(quote["outAmount"])
    in_amount = int(quote.get("inAmount", amount))
    
    # Build swap
    resp2 = requests.post(JUP_SWAP, json={
        "quoteResponse": quote,
        "userPublicKey": WALLET,
        "wrapUnwrapSOL": False,
    }, timeout=15)
    swap = resp2.json()
    if "error" in swap:
        return False, "", f"Swap build error: {swap['error']}"
    
    # Sign and send
    tx_b64 = swap["swapTransaction"]
    tx_bytes = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    signed_b64 = base64.b64encode(bytes(signed_tx)).decode()
    
    resp3 = requests.post(RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
    }, timeout=60)
    
    result = resp3.json()
    if "error" in result:
        err_msg = result["error"].get("message", "")
        # Check for mayhem paused
        if "6001" in str(result["error"]) or "Custom" in str(result["error"]):
            return False, "", "Pump.fun bonding curve paused (mayhem)"
        return False, "", f"Send error: {err_msg[:200]}"
    
    sig = result["result"]
    return True, sig, f"out={out_amount}"

def check_mayhem():
    """Check if pump.fun bonding curve is active"""
    try:
        resp = requests.get(f"https://frontend-api-v3.pump.fun/coins/{ARRB}", timeout=10)
        data = resp.json()
        state = data.get("mayhem_state", "unknown")
        mc = float(data.get("usd_market_cap", 0))
        return state, mc
    except:
        return "unknown", 0

# Trading loop
print(f"=== ARRB Pump.fun Trading Loop ===")
print(f"Wallet: {WALLET}")
print()

usdc_bal = get_balance(USDC)
arrb_bal = get_balance(ARRB)
sol_bal = get_sol_balance()
print(f"Starting balances: {usdc_bal:.2f} USDC | {arrb_bal:,.0f} ARRB | {sol_bal:.4f} SOL")
print()

cycle = 0
total_volume = 0
max_cycles = 20  # safety limit

while cycle < max_cycles:
    cycle += 1
    usdc_bal = get_balance(USDC)
    arrb_bal = get_balance(ARRB)
    sol_bal = get_sol_balance()
    
    # Check if we have enough to continue
    if usdc_bal < 1.5 and arrb_bal < 50000:
        print(f"[Cycle {cycle}] Insufficient funds: {usdc_bal:.2f} USDC, {arrb_bal:,.0f} ARRB. Stopping.")
        break
    
    if sol_bal < 0.005:
        print(f"[Cycle {cycle}] Low SOL for gas: {sol_bal:.6f}. Stopping.")
        break
    
    # Check mayhem state
    state, mc = check_mayhem()
    if state == "paused":
        print(f"[Cycle {cycle}] Mayhem PAUSED (mc=${mc:,.0f}). Waiting 30s...")
        time.sleep(30)
        state, mc = check_mayhem()
        if state == "paused":
            print(f"[Cycle {cycle}] Still paused. Stopping.")
            break
    
    # Alternate: buy on odd cycles, sell on even
    if cycle % 2 == 1:  # BUY
        if usdc_bal < 1.5:
            print(f"[Cycle {cycle}] Not enough USDC ({usdc_bal:.2f}). Skipping buy.")
            continue
        
        # Use ~40% of USDC each buy to keep it going
        buy_usdc = min(usdc_bal * 0.6, usdc_bal - 0.5)  # keep 0.5 USDC buffer
        buy_amount = int(buy_usdc * 1e6)  # USDC has 6 decimals
        
        print(f"[Cycle {cycle}] BUY: {buy_usdc:.2f} USDC -> ARRB (mc=${mc:,.0f})")
        ok, sig, details = jup_swap(USDC, ARRB, buy_amount)
        
        if ok:
            new_arrb = get_balance(ARRB)
            print(f"  ✅ TX: {sig[:20]}... | {details}")
            print(f"  New ARRB balance: {new_arrb:,.0f}")
            total_volume += buy_usdc
        else:
            print(f"  ❌ {details}")
            if "paused" in details.lower():
                print("  Bonding curve paused. Waiting 30s...")
                time.sleep(30)
    else:  # SELL
        if arrb_bal < 50000:
            print(f"[Cycle {cycle}] Not enough ARRB ({arrb_bal:,.0f}). Skipping sell.")
            continue
        
        # Sell ~35% of ARRB each time (ARRB has 6 decimals, so multiply by 1e6)
        sell_amount_ui = arrb_bal * 0.35
        sell_arrb = int(sell_amount_ui * 1e6)
        
        print(f"[Cycle {cycle}] SELL: {sell_amount_ui:,.0f} ARRB -> USDC (mc=${mc:,.0f})")
        ok, sig, details = jup_swap(ARRB, USDC, sell_arrb)
        
        if ok:
            new_usdc = get_balance(USDC)
            print(f"  ✅ TX: {sig[:20]}... | {details}")
            print(f"  New USDC balance: {new_usdc:.2f}")
            total_volume += sell_amount_ui * 0.00007  # approx value
        else:
            print(f"  ❌ {details}")
            if "paused" in details.lower():
                print("  Bonding curve paused. Waiting 30s...")
                time.sleep(30)
    
    # Brief pause between trades
    time.sleep(5)

# Final balances
usdc_bal = get_balance(USDC)
arrb_bal = get_balance(ARRB)
sol_bal = get_sol_balance()
print(f"\n=== Final Summary ===")
print(f"Cycles: {cycle}")
print(f"Total volume: ~${total_volume:.2f}")
print(f"Final balances: {usdc_bal:.2f} USDC | {arrb_bal:,.0f} ARRB | {sol_bal:.4f} SOL")
print(f"ARRB on pump.fun: https://dexscreener.com/solana/7fcandwguw1zazhihqdkk4tobadw1k6n6cgmxzkk2ify")
