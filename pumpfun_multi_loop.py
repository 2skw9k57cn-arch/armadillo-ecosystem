#!/usr/bin/env python3
"""
Multi-Token Pump.fun Loop Trading Bot
======================================
Loop-trades multiple pump.fun tokens on Solana to generate volume and capture
price movements. Uses Jupiter aggregator for all swaps.

Strategy:
- Cycles buy → sell for each token in the list
- Uses 60% of USDC per buy, sells 50% of token balance per sell
- Alternates between tokens to spread volume
- Tracks P&L per token and cumulatively
- Safety limits: max cycles, min balances, gas reserve

Supported tokens are configured in TOKENS dict below.
"""

import json, base64, requests, time, sys, os
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from datetime import datetime

# ============ CONFIG ============
SOL_KEYPAIR_PATH = "/home/hermes/.config/solana/armabase-sol.json"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
TRADE_LOG = "/workspace/sol_trade_log.json"
POSITIONS_FILE = "/workspace/sol_positions.json"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Tokens to loop-trade (add new tokens here)
# Also reads from /workspace/pumpfun_loop_tokens.json (written by deployer_watcher.py)
DEFAULT_TOKENS = {
    "ARMASA": {
        "mint": "C5Np9tRxNnuZkuknyQezrvWj5d115gxhfSf31eo4pump",
        "decimals": 6,
        "min_trade_usdc": 0.1,
    },
    "ARRB": {
        "mint": "2GL1Licg8692gW3ucgAoacJhhzackmcVVb2nAVmWpump",
        "decimals": 6,
        "min_trade_usdc": 0.1,
    },
}

# Load dynamic tokens from deployer watcher if available
def load_dynamic_tokens():
    """Load tokens from watcher, then adjust sizes based on learned params"""
    try:
        with open("/workspace/pumpfun_loop_tokens.json") as f:
            dynamic = json.load(f)
            merged = dict(DEFAULT_TOKENS)
            merged.update(dynamic)
            
            # Apply learned parameters if available
            try:
                with open("/workspace/learned_params.json") as f:
                    learned = json.load(f)
                for sym, info in learned.get("tokens", {}).items():
                    if sym in merged:
                        rec = info.get("recommendation", "")
                        if "STOP" in rec:
                            del merged[sym]  # Remove dead tokens
                        elif "INCREASE" in rec:
                            merged[sym]["min_trade_usdc"] = min(merged[sym].get("min_trade_usdc", 0.05) * 1.5, 0.50)
                        elif "REDUCE" in rec:
                            merged[sym]["min_trade_usdc"] = max(merged[sym].get("min_trade_usdc", 0.05) * 0.5, 0.02)
            except Exception as e:
                pass  # No learned params yet, use defaults
            
            return merged
    except Exception as e:
        return DEFAULT_TOKENS

TOKENS = load_dynamic_tokens()

# Trading parameters
MAX_CYCLES = 50           # Safety limit per run
BUY_PCT = 0.60            # Use 60% of USDC per buy
SELL_PCT = 0.50           # Sell 50% of token balance per sell
SLIPPAGE_BPS = 500        # 5% slippage
MIN_USDC = 0.1            # Min USDC to keep trading
MIN_SOL_RESERVE = 0.005   # Min SOL for gas
TRADE_DELAY = 3           # Seconds between trades
MAX_PRICE_IMPACT = 0.15   # Skip if >15% price impact

# ============ WALLET ============
with open(SOL_KEYPAIR_PATH) as f:
    secret = json.load(f)
KEYPAIR = Keypair.from_bytes(bytes(secret))
WALLET = str(KEYPAIR.pubkey())

# ============ FUNCTIONS ============

def get_sol_balance():
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET]
    }, timeout=15)
    return resp.json().get("result", {}).get("value", 0) / 1e9

def get_balance(mint):
    """Get token balance for a mint address"""
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accounts = resp.json().get("result", {}).get("value", [])
    if accounts:
        amt = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return float(amt["uiAmount"] or 0)
    return 0.0

def get_all_balances():
    """Get all token balances at once"""
    balances = {"SOL": get_sol_balance(), "USDC": get_balance(USDC_MINT)}
    for sym, info in TOKENS.items():
        balances[sym] = get_balance(info["mint"])
    return balances

def jup_swap(input_mint, output_mint, amount, slippage=SLIPPAGE_BPS):
    """Execute a Jupiter swap. Returns (success, tx_sig, details)"""
    try:
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
        price_impact = float(quote.get("priceImpactPct", 0) or 0)

        # Check price impact
        if price_impact > MAX_PRICE_IMPACT:
            return False, "", f"Price impact too high: {price_impact*100:.1f}%"

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
        signed_tx = VersionedTransaction(tx.message, [KEYPAIR])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

        resp3 = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=60)

        result = resp3.json()
        if "error" in result:
            err_msg = result["error"].get("message", "")
            return False, "", f"Send error: {err_msg[:200]}"

        sig = result["result"]
        return True, sig, f"out={out_amount} impact={price_impact*100:.2f}%"

    except Exception as e:
        return False, "", f"Exception: {str(e)[:200]}"

def load_trade_log():
    try:
        with open(TRADE_LOG) as f:
            data = json.load(f)
            if isinstance(data, list):
                return {"trades": data, "total_volume": 0, "total_pnl": 0}
            return data
    except Exception as e:
        return {"trades": [], "total_volume": 0, "total_pnl": 0}

def save_trade_log(log):
    with open(TRADE_LOG, "w") as f:
        json.dump(log, f, indent=2)

def log_trade(log, action, token, amount_in, amount_out, tx_sig, details):
    entry = {
        "time": datetime.utcnow().isoformat(),
        "action": action,
        "token": token,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "tx_sig": tx_sig[:30] if tx_sig else "",
        "details": details,
    }
    log["trades"].append(entry)
    if len(log["trades"]) > 200:
        log["trades"] = log["trades"][-100:]
    save_trade_log(log)

def get_token_price(mint):
    """Get current token price from DexScreener"""
    try:
        resp = requests.get(f"{DEXSCREENER}{mint}", timeout=10)
        data = resp.json()
        pairs = data.get("pairs", [])
        if pairs:
            return float(pairs[0].get("priceUsd", 0) or 0)
    except Exception as e:
        pass
    return 0.0

# ============ MAIN LOOP ============

def run():
    print(f"\n🔄 Multi-Token Pump.fun Loop Trading Bot")
    print(f"   {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Wallet: {WALLET}")
    print(f"   Tokens: {', '.join(TOKENS.keys())}")
    print()

    # Initial balances
    balances = get_all_balances()
    print(f"   Starting Balances:")
    for sym, amt in balances.items():
        if amt > 0.0001:
            print(f"     {sym}: {amt:,.4f}")
    print()

    sol_bal = balances["SOL"]
    if sol_bal < MIN_SOL_RESERVE:
        print(f"   ❌ Insufficient SOL for gas: {sol_bal:.6f} (need {MIN_SOL_RESERVE})")
        return

    usdc_bal = balances["USDC"]
    if usdc_bal < MIN_USDC:
        print(f"   ❌ Insufficient USDC: ${usdc_bal:.2f} (need ${MIN_USDC})")
        return

    log = load_trade_log()
    cycle = 0
    total_volume = 0

    # Trade each token in sequence
    token_list = list(TOKENS.keys())

    while cycle < MAX_CYCLES:
        for sym in token_list:
            cycle += 1
            if cycle > MAX_CYCLES:
                break

            info = TOKENS[sym]
            mint = info["mint"]
            decimals = info["decimals"]

            # Refresh balances
            usdc_bal = get_balance(USDC_MINT)
            token_bal = get_balance(mint)
            sol_bal = get_sol_balance()

            # Safety checks
            if sol_bal < MIN_SOL_RESERVE:
                print(f"   [C{cycle}] ⚠️ Low SOL ({sol_bal:.6f}). Stopping.")
                break

            if usdc_bal < MIN_USDC and token_bal < 1:
                print(f"   [C{cycle}] ⚠️ No funds for {sym} (USDC=${usdc_bal:.2f}, {sym}={token_bal:.0f})")
                continue

            # Decide: buy or sell?
            # Buy if we have USDC and less than threshold of token
            # Sell if we have token balance
            if token_bal > 1 and (usdc_bal < MIN_USDC or cycle % 2 == 0):
                # SELL
                sell_amount_ui = token_bal * SELL_PCT
                sell_amount = int(sell_amount_ui * (10 ** decimals))

                if sell_amount < 1:
                    continue

                print(f"   [C{cycle}] SELL {sym}: {sell_amount_ui:,.0f} → USDC")

                ok, sig, details = jup_swap(mint, USDC_MINT, sell_amount)
                if not ok and "too large" in details.lower():
                    # Fallback: sell via SOL route (smaller transactions)
                    print(f"     Retrying via SOL route...")
                    ok, sig, details = jup_swap(mint, SOL_MINT, sell_amount, slippage=SLIPPAGE_BPS)
                if ok:
                    time.sleep(2)  # Wait for RPC to confirm
                    new_usdc = get_balance(USDC_MINT)
                    usdc_gained = new_usdc - usdc_bal
                    print(f"     ✅ TX: {sig[:20]}... | {details}")
                    print(f"     USDC: ${usdc_bal:.4f} → ${new_usdc:.4f} (+${usdc_gained:.4f})")
                    total_volume += sell_amount_ui * get_token_price(mint)
                    log_trade(log, "SELL", sym, sell_amount_ui, usdc_gained, sig, details)
                else:
                    print(f"     ❌ {details}")
                    if "paused" in details.lower() or "6001" in details:
                        print(f"     Bonding curve paused. Skipping {sym}.")
                        continue

            elif usdc_bal >= MIN_USDC:
                # BUY
                buy_usdc = min(usdc_bal * BUY_PCT, usdc_bal - 0.05)  # keep 0.05 USDC buffer
                buy_amount = int(buy_usdc * 1e6)  # USDC has 6 decimals

                if buy_amount < 10000:  # less than 0.01 USDC
                    continue

                print(f"   [C{cycle}] BUY {sym}: ${buy_usdc:.4f} USDC → {sym}")

                ok, sig, details = jup_swap(USDC_MINT, mint, buy_amount)
                if not ok and "too large" in details.lower():
                    # Fallback: buy via SOL route (smaller transactions)
                    print(f"     Retrying via SOL route...")
                    sol_bal_now = get_sol_balance()
                    sol_amount = int(min(sol_bal_now - MIN_SOL_RESERVE, 0.003) * 1e9)
                    if sol_amount > 1000:
                        ok, sig, details = jup_swap(SOL_MINT, mint, sol_amount, slippage=SLIPPAGE_BPS)
                if ok:
                    time.sleep(2)  # Wait for RPC to confirm
                    new_token = get_balance(mint)
                    print(f"     ✅ TX: {sig[:20]}... | {details}")
                    print(f"     {sym}: {token_bal:.0f} → {new_token:,.0f}")
                    total_volume += buy_usdc
                    log_trade(log, "BUY", sym, buy_usdc, new_token - token_bal, sig, details)
                else:
                    print(f"     ❌ {details}")
                    if "paused" in details.lower() or "6001" in details:
                        print(f"     Bonding curve paused. Skipping {sym}.")
                        continue

            time.sleep(TRADE_DELAY)

        # Check if we still have funds
        usdc_bal = get_balance(USDC_MINT)
        sol_bal = get_sol_balance()
        if usdc_bal < MIN_USDC and sol_bal < MIN_SOL_RESERVE:
            print(f"\n   ⚠️ Funds depleted. USDC=${usdc_bal:.4f}, SOL={sol_bal:.6f}")
            break

    # Final summary
    final = get_all_balances()
    print(f"\n{'='*55}")
    print(f"   FINAL SUMMARY")
    print(f"{'='*55}")
    print(f"   Cycles: {cycle}")
    print(f"   Total volume: ~${total_volume:.2f}")
    print(f"   Final balances:")
    for sym, amt in final.items():
        if amt > 0.0001:
            print(f"     {sym}: {amt:,.4f}")
    print(f"   Total trades logged: {len(log.get('trades', []))}")
    print(f"{'='*55}")

# ─── Self-Healing Cron Mesh ──────────────────────────────────────────────
# Any surviving cron script rebuilds all 10 jobs (including watchdog) on run.
import sys as _sys, os as _os
from goal_tracker import check_goal
_sys.path.insert(0, "/workspace")
try:
    from cron_watchdog import ensure_all_crons
    _recreated = ensure_all_crons()
    # Check $1M goal — halt if achieved
    try:
        if check_goal():
            sys.exit(0)
    except Exception as e:
        print(f"Goal check skipped: {e}")
    if _recreated:
        print(f"  🔧 Self-healed crons: {', '.join(sorted(_recreated))}")
except Exception as _e:
    print(f"  ⚠️ Cron self-heal skipped: {_e}")
# ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
