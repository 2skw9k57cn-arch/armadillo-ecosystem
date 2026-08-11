#!/usr/bin/env python3
"""
ARBA + ARRB Multi-Chain Profit Loop
====================================
Operates on all available funds to compound into profit:

CHAIN 1 - Base (EVM via ACP):
  USDC -> buy ARBA -> (burn to reduce supply, increase scarcity)
  VIRTUAL -> buy ARBA -> (burn)
  This drives ARBA price up on bonding curve

CHAIN 2 - Solana (pump.fun/PumpSwap via Jupiter):
  USDC -> buy ARRB -> sell ARRB -> USDC -> repeat
  Each cycle captures spread + generates volume
  Volume attracts attention on DexScreener

CHAIN 3 - Robinhood Chain (EVM via ACP):
  ARRB holdings (214K) - monitor for sell opportunities

The goal: loop until total value is 100x the USDC input
"""

import json, time, base64, requests, subprocess, os, sys
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ============ CONFIG ============
# Solana
SOL_KEYPAIR_PATH = "/home/hermes/.config/solana/armabase-sol.json"
ARRB_MINT = "2GL1Licg8692gW3ucgAoacJhhzackmcVVb2nAVmWpump"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"

# EVM (Base)
ARBA_CONTRACT = "0x557642685ce68F3975458375B51553871807e1b5"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"
BURN_LOG = "/workspace/arba_burn_log.json"
PROFIT_LOG = "/workspace/profit_log.json"

# ============ SOLANA FUNCTIONS ============
with open(SOL_KEYPAIR_PATH) as f:
    secret = json.load(f)
SOL_KEYPAIR = Keypair.from_bytes(bytes(secret))
SOL_WALLET = str(SOL_KEYPAIR.pubkey())

def sol_get_balance(mint):
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [SOL_WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accounts = resp.json().get("result", {}).get("value", [])
    if accounts:
        return float(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def sol_get_sol():
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance", "params": [SOL_WALLET]
    }, timeout=15)
    return resp.json().get("result", {}).get("value", 0) / 1e9

def jup_trade(input_mint, output_mint, amount_raw, slippage=1000):
    """Execute Jupiter swap on Solana. Returns (success, details)"""
    # Get quote
    resp = requests.get(JUP_QUOTE, params={
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": str(amount_raw), "slippageBps": str(slippage),
    }, timeout=15)
    quote = resp.json()
    if "error" in quote:
        return False, f"Quote: {quote['error']}"
    out_amount = int(quote["outAmount"])
    
    # Build swap
    resp2 = requests.post(JUP_SWAP, json={
        "quoteResponse": quote, "userPublicKey": SOL_WALLET, "wrapUnwrapSOL": False,
    }, timeout=15)
    swap = resp2.json()
    if "error" in swap:
        return False, f"Swap build: {swap['error']}"
    
    # Sign and send
    tx_bytes = base64.b64decode(swap["swapTransaction"])
    tx = VersionedTransaction.from_bytes(tx_bytes)
    signed = VersionedTransaction(tx.message, [SOL_KEYPAIR])
    signed_b64 = base64.b64encode(bytes(signed)).decode()
    
    resp3 = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
    }, timeout=60)
    result = resp3.json()
    if "error" in result:
        return False, f"Send: {result['error'].get('message','')[:100]}"
    
    return True, {"sig": result["result"], "out": out_amount}

# ============ EVM FUNCTIONS (via ACP CLI) ============
def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def evm_get_balance(symbol, chain=8453):
    out, _, _ = run(f"acp wallet balance --chain-id {chain} --json")
    try:
        d = json.loads(out)
        for t in d['tokens']:
            sym = t['tokenMetadata'].get('symbol') or 'ETH'
            if sym.upper() == symbol.upper():
                bal = int(t['tokenBalance'], 16) if t['tokenBalance'] else 0
                dec = t['tokenMetadata'].get('decimals') or 18
                return bal / (10**dec)
    except:
        pass
    return 0

def evm_buy_arba_usdc(usdc_amount):
    out, err, rc = run(
        f"acp trade --token-in usdc --chain-in 8453 --amount-in {usdc_amount} "
        f"--token-out {ARBA_CONTRACT} --chain-out 8453 --json"
    )
    try:
        d = json.loads(out)
        if d.get('status') == 'success':
            received = float(d.get('finalReceived', '0').replace(' ARBA', ''))
            txs = [leg['txHash'] for leg in d.get('legs', []) if leg.get('txHash')]
            return True, received, txs
        return False, 0, d.get('error', err)
    except:
        return False, 0, err or out

def evm_burn_arba(amount):
    amount_wei = hex(int(amount * 10**18))
    amt_hex = amount_wei[2:].zfill(64)
    calldata = f"0xa9059cbb000000000000000000000000000000000000000000000000000000000000dead{amt_hex}"
    out, err, rc = run(
        f"acp wallet send-transaction --chain-id 8453 "
        f"--to {ARBA_CONTRACT} --data {calldata} --json"
    )
    if "approval" in out.lower() or "pending" in out.lower():
        # Extract approval URL
        import re
        m = re.search(r'https://app\.virtuals\.io/wallet/approve-transaction\?id=[a-f0-9-]+', out)
        if m:
            return False, f"Burn needs approval: {m.group()}"
        return False, "Burn needs manual approval"
    try:
        d = json.loads(out)
        tx = d.get('txHash') or d.get('hash', '')
        if tx:
            return True, tx
        return False, str(d)
    except:
        return False, out[:200]

def get_arba_supply():
    resp = requests.post("https://mainnet.base.org", json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": ARBA_CONTRACT, "data": "0x18160ddd"}, "latest"]
    }, timeout=10)
    return int(resp.json().get("result", "0x0"), 16) / 1e18

# ============ LOGGING ============
def log_profit(cycle, chain, action, amount_in, amount_out, tx, note=""):
    log = []
    if os.path.exists(PROFIT_LOG):
        try:
            with open(PROFIT_LOG) as f:
                log = json.load(f)
        except:
            log = []
    log.append({
        'cycle': cycle, 'timestamp': int(time.time()),
        'date': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        'chain': chain, 'action': action,
        'amount_in': amount_in, 'amount_out': amount_out,
        'tx': tx, 'note': note,
    })
    with open(PROFIT_LOG, 'w') as f:
        json.dump(log, f, indent=2)

# ============ MAIN LOOP ============
def main():
    print("🔥 ARBA + ARRB Multi-Chain Profit Loop 🔥")
    print(f"Solana wallet: {SOL_WALLET}")
    print(f"Target: 100x USDC input")
    print()
    
    # Get starting state
    sol_usdc = sol_get_balance(USDC_MINT)
    sol_arrb = sol_get_balance(ARRB_MINT)
    sol_sol = sol_get_sol()
    evm_usdc = evm_get_balance('USDC')
    evm_arba = evm_get_balance('ARBA')
    evm_virtual = evm_get_balance('VIRTUAL')
    
    starting_usdc = sol_usdc + evm_usdc
    print(f"Starting USDC: ${starting_usdc:.2f} (Solana: ${sol_usdc:.2f}, Base: ${evm_usdc:.2f})")
    print(f"Starting ARRB: {sol_arrb:,.0f} (Solana)")
    print(f"Starting ARBA: {evm_arba:,.0f} (Base)")
    print(f"Starting SOL: {sol_sol:.4f}")
    print()
    
    if starting_usdc < 1:
        print("⚠️ Not enough USDC to start. Need at least $1.")
        return
    
    target = starting_usdc * 100
    print(f"Target (100x): ${target:.2f}")
    print()
    
    cycle = 0
    max_cycles = 50
    total_volume = 0
    
    while cycle < max_cycles:
        cycle += 1
        
        # Refresh balances
        sol_usdc = sol_get_balance(USDC_MINT)
        sol_arrb = sol_get_balance(ARRB_MINT)
        sol_sol = sol_get_sol()
        evm_usdc = evm_get_balance('USDC')
        evm_arba = evm_get_balance('ARBA')
        evm_virtual = evm_get_balance('VIRTUAL')
        current_usdc = sol_usdc + evm_usdc
        
        print(f"\n[Cycle {cycle}] Total USDC: ${current_usdc:.2f} / ${target:.2f} | "
              f"ARRB: {sol_arrb:,.0f} | ARBA: {evm_arba:,.0f} | SOL: {sol_sol:.4f}")
        
        # Check if we hit 100x
        if current_usdc >= target:
            print(f"🎉 TARGET HIT! ${current_usdc:.2f} >= ${target:.2f}")
            break
        
        # Check if we're out of resources
        if sol_sol < 0.003:
            print("⚠️ Low SOL for gas. Stopping Solana operations.")
            if evm_usdc < 2:
                print("⚠️ No EVM funds either. Stopping.")
                break
        
        # ---- SOLANA LOOP: Buy ARRB -> Sell ARRB ----
        if sol_sol >= 0.003:
            if sol_usdc >= 0.5:
                # BUY ARRB
                buy_usdc_raw = int(sol_usdc * 0.8 * 1e6)  # Use 80% of USDC, 6 decimals
                if buy_usdc_raw >= 100000:  # At least 0.1 USDC
                    print(f"  [SOL] BUY: {buy_usdc_raw/1e6:.4f} USDC -> ARRB")
                    ok, details = jup_trade(USDC_MINT, ARRB_MINT, buy_usdc_raw)
                    if ok:
                        arrb_received = details['out'] / 1e6
                        print(f"  ✅ Got {arrb_received:,.0f} ARRB | TX: {details['sig'][:20]}...")
                        log_profit(cycle, 'solana', 'buy_arrb', buy_usdc_raw/1e6, arrb_received, details['sig'])
                        total_volume += buy_usdc_raw / 1e6
                        time.sleep(3)
                    else:
                        print(f"  ❌ {details}")
            
            # Refresh and sell
            sol_arrb = sol_get_balance(ARRB_MINT)
            if sol_arrb >= 100000:
                sell_arrb_raw = int(sol_arrb * 0.7 * 1e6)  # Sell 70% of ARRB
                print(f"  [SOL] SELL: {sell_arrb_raw/1e6:,.0f} ARRB -> USDC")
                ok, details = jup_trade(ARRB_MINT, USDC_MINT, sell_arrb_raw)
                if ok:
                    usdc_received = details['out'] / 1e6
                    print(f"  ✅ Got {usdc_received:.4f} USDC | TX: {details['sig'][:20]}...")
                    log_profit(cycle, 'solana', 'sell_arrb', sell_arrb_raw/1e6, usdc_received, details['sig'])
                    total_volume += usdc_received
                    time.sleep(3)
                else:
                    print(f"  ❌ {details}")
        
        # ---- BASE LOOP: Buy ARBA (buyback-burn) ----
        if evm_usdc >= 2.5:
            buy_amount = evm_usdc - 1.0  # Keep $1 reserve
            print(f"  [BASE] BUYBACK: ${buy_amount:.2f} USDC -> ARBA")
            ok, arba_received, txs = evm_buy_arba_usdc(round(buy_amount, 2))
            if ok:
                print(f"  ✅ Got {arba_received:,.0f} ARBA | TX: {txs[-1][:20]}...")
                log_profit(cycle, 'base', 'buy_arba', buy_amount, arba_received, txs[-1] if txs else '')
                total_volume += buy_amount
                
                # Try to burn
                time.sleep(3)
                new_arba = evm_get_balance('ARBA')
                if new_arba > 100:
                    print(f"  [BASE] BURN: {new_arba:,.0f} ARBA -> dead address")
                    burn_ok, burn_result = evm_burn_arba(new_arba)
                    if burn_ok:
                        print(f"  🔥 Burned! TX: {burn_result[:20]}...")
                        log_profit(cycle, 'base', 'burn_arba', new_arba, 0, burn_result, 'burned to dead')
                    else:
                        print(f"  ⚠️ Burn needs approval: {burn_result[:80]}")
            else:
                print(f"  ❌ Buyback failed: {str(arba_received)[:100]}")
        
        # ---- VIRTUAL: Buy ARBA with VIRTUAL if available ----
        if evm_virtual >= 4:
            buy_v = evm_virtual - 1
            print(f"  [BASE] BUYBACK: {buy_v:.2f} VIRTUAL -> ARBA")
            out, err, rc = run(
                f"acp trade --token-in virtual --chain-in 8453 --amount-in {buy_v} "
                f"--token-out {ARBA_CONTRACT} --chain-out 8453 --json"
            )
            try:
                d = json.loads(out)
                if d.get('status') == 'success':
                    received = float(d.get('finalReceived', '0').replace(' ARBA', ''))
                    txs = [leg['txHash'] for leg in d.get('legs', []) if leg.get('txHash')]
                    print(f"  ✅ Got {received:,.0f} ARBA | TX: {txs[-1][:20]}...")
                    log_profit(cycle, 'base', 'buy_arba_virtual', buy_v, received, txs[-1] if txs else '')
                    total_volume += buy_v * 0.56
                else:
                    print(f"  ❌ {d.get('error', '')[:100]}")
            except:
                print(f"  ❌ {err[:100]}")
        
        # Brief pause
        time.sleep(2)
    
    # Final summary
    sol_usdc = sol_get_balance(USDC_MINT)
    sol_arrb = sol_get_balance(ARRB_MINT)
    sol_sol = sol_get_sol()
    evm_usdc = evm_get_balance('USDC')
    evm_arba = evm_get_balance('ARBA')
    supply = get_arba_supply()
    
    final_usdc = sol_usdc + evm_usdc
    
    print(f"\n{'='*60}")
    print(f"FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"Cycles: {cycle}")
    print(f"Total volume: ${total_volume:.2f}")
    print(f"USDC: ${starting_usdc:.2f} -> ${final_usdc:.2f} ({final_usdc/starting_usdc:.1f}x)")
    print(f"SOL: {sol_sol:.4f}")
    print(f"ARRB (Solana): {sol_arrb:,.0f}")
    print(f"ARBA (Base): {evm_arba:,.0f}")
    print(f"ARBA supply: {supply:,.0f} (burned: {1_000_000_000 - supply:,.0f})")

if __name__ == "__main__":
    main()
