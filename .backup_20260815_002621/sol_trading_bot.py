#!/usr/bin/env python3
"""
ArmaBase Smart Solana Trading Bot
=================================
Scans Solana tokens for profitable opportunities, trades with judgment.

Strategy:
1. Scans BONK, POPCAT, MEW, and other liquid Solana tokens
2. Gets price changes (1h, 6h, 24h) from DexScreener
3. Uses judgment rules to decide buy/sell:
   - Buy tokens that are trending up (positive 1h + 6h momentum)
   - Sell tokens that are losing momentum or hit profit target
   - Skip tokens with no clear trend
4. Executes trades via Jupiter aggregator
5. Tracks P&L per trade and cumulatively

Risk management:
- Max 0.02 SOL per trade (~$3.50)
- Stop loss: sell if position drops 5%
- Take profit: sell if position gains 3%+
- Max hold time: 5 cycles (~15 min)
- Never trade with less than 0.03 SOL (gas reserve)
"""

import json, time, base64, requests, os, sys
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from datetime import datetime

# ============ CONFIG ============
SOL_KEYPAIR_PATH = "/home/hermes/.config/solana/armabase-sol.json"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/search"
TRADE_LOG = "/workspace/sol_trade_log.json"
POSITIONS_FILE = "/workspace/sol_positions.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Tradeable tokens (verified working on Jupiter)
TRADEABLE_TOKENS = {
    "BONK": {
        "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "decimals": 5,
        "min_trade_sol": 0.01,
    },
    "POPCAT": {
        "mint": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
        "decimals": 6,
        "min_trade_sol": 0.01,
    },
    "MEW": {
        "mint": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
        "decimals": 6,
        "min_trade_sol": 0.01,
    },
}

# Risk parameters
MAX_TRADE_SOL = 0.02       # Max SOL per trade
MIN_SOL_RESERVE = 0.03     # Keep at least 0.03 SOL for gas
STOP_LOSS_PCT = -5.0       # Sell if down 5%
TAKE_PROFIT_PCT = 3.0      # Sell if up 3%
MAX_HOLD_CYCLES = 5        # Max cycles to hold a position
CYCLE_DELAY = 60           # Seconds between cycles
SCAN_INTERVAL = 180        # Seconds between market scans

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

def get_token_balance(mint):
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accounts = resp.json().get("result", {}).get("value", [])
    if accounts:
        return float(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def get_market_data(symbol):
    """Get price change data from DexScreener"""
    resp = requests.get(DEXSCREENER, params={"q": symbol}, timeout=15)
    pairs = resp.json().get("pairs", [])
    for p in pairs:
        if p.get("chainId") == "solana" and p.get("baseToken", {}).get("symbol", "").upper() == symbol:
            return {
                "price": float(p.get("priceUsd", "0") or "0"),
                "liq": float(p.get("liquidity", {}).get("usd", 0) or 0),
                "vol24h": float(p.get("volume", {}).get("h24", 0) or 0),
                "chg1h": float(p.get("priceChange", {}).get("h1", 0) or 0),
                "chg6h": float(p.get("priceChange", {}).get("h6", 0) or 0),
                "chg24h": float(p.get("priceChange", {}).get("h24", 0) or 0),
                "address": p.get("baseToken", {}).get("address", ""),
            }
    return None

def jup_swap(input_mint, output_mint, amount_raw, slippage=500):
    """Execute Jupiter swap. Returns (success, details)"""
    # Get quote
    resp = requests.get(JUP_QUOTE, params={
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": str(amount_raw), "slippageBps": str(slippage),
    }, timeout=15)
    quote = resp.json()
    if "error" in quote:
        return False, f"Quote: {quote['error']}"
    out_amount = int(quote["outAmount"])
    
    # Build swap tx
    resp2 = requests.post(JUP_SWAP, json={
        "quoteResponse": quote, "userPublicKey": WALLET, "wrapUnwrapSOL": True,
    }, timeout=15)
    swap = resp2.json()
    if "error" in swap:
        return False, f"Swap build: {swap['error']}"
    
    # Sign and send
    tx_bytes = base64.b64decode(swap["swapTransaction"])
    tx = VersionedTransaction.from_bytes(tx_bytes)
    signed = VersionedTransaction(tx.message, [KEYPAIR])
    signed_b64 = base64.b64encode(bytes(signed)).decode()
    
    resp3 = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
    }, timeout=60)
    result = resp3.json()
    if "error" in result:
        return False, f"Send: {result['error'].get('message', '')[:100]}"
    
    return True, {"sig": result["result"], "out": out_amount}

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return {}

def save_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

def load_trade_log():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            return json.load(f)
    return []

def save_trade_log(log):
    with open(TRADE_LOG, "w") as f:
        json.dump(log, f, indent=2)

def log_trade(entry):
    log = load_trade_log()
    log.append(entry)
    save_trade_log(log)

# ============ TRADING LOGIC ============
def scan_market():
    """Scan all tradeable tokens and return opportunities sorted by score"""
    opportunities = []
    for symbol, info in TRADEABLE_TOKENS.items():
        data = get_market_data(symbol)
        if not data:
            continue
        if data["liq"] < 1000:  # Need at least $1K liquidity
            continue
        
        # Score: weighted momentum (1h most important, then 6h, then 24h)
        score = (data["chg1h"] * 0.5) + (data["chg6h"] * 0.3) + (data["chg24h"] * 0.2)
        
        # Judgment: only trade if there's clear momentum
        # Buy signal: positive score > 0.5 (combined upward momentum)
        # The stronger the momentum, the higher the score
        opportunities.append({
            "symbol": symbol,
            "mint": info["mint"],
            "decimals": info["decimals"],
            "price": data["price"],
            "liq": data["liq"],
            "vol24h": data["vol24h"],
            "chg1h": data["chg1h"],
            "chg6h": data["chg6h"],
            "chg24h": data["chg24h"],
            "score": score,
            "action": "BUY" if score > 0.5 else ("SELL" if score < -0.5 else "HOLD"),
        })
    
    opportunities.sort(key=lambda x: abs(x["score"]), reverse=True)
    return opportunities

def execute_buy(symbol, mint, decimals, sol_amount):
    """Buy token with SOL"""
    sol_raw = int(sol_amount * 1e9)
    print(f"  📗 BUY {symbol}: {sol_amount:.4f} SOL -> {symbol}")
    ok, details = jup_swap(SOL_MINT, mint, sol_raw)
    if ok:
        token_received = details["out"] / (10 ** decimals)
        print(f"  ✅ Got {token_received:,.2f} {symbol} | TX: {details['sig'][:20]}...")
        
        # Record position
        positions = load_positions()
        positions[symbol] = {
            "mint": mint,
            "decimals": decimals,
            "entry_sol": sol_amount,
            "entry_token": token_received,
            "entry_price": sol_amount / token_received if token_received > 0 else 0,
            "entry_time": time.time(),
            "cycles_held": 0,
            "tx": details["sig"],
        }
        save_positions(positions)
        
        log_trade({
            "time": datetime.utcnow().isoformat(),
            "action": "BUY", "symbol": symbol,
            "sol_in": sol_amount, "token_out": token_received,
            "tx": details["sig"], "score": 0,
        })
        return True, token_received
    else:
        print(f"  ❌ Buy failed: {details}")
        return False, 0

def execute_sell(symbol, position):
    """Sell token position back to SOL"""
    mint = position["mint"]
    decimals = position["decimals"]
    
    # Get current balance (might differ from recorded)
    balance = get_token_balance(mint)
    if balance < 0.0001:
        print(f"  ⚠️ No {symbol} balance to sell")
        return False, 0
    
    token_raw = int(balance * (10 ** decimals))
    print(f"  📕 SELL {symbol}: {balance:,.2f} {symbol} -> SOL")
    ok, details = jup_swap(mint, SOL_MINT, token_raw)
    if ok:
        sol_received = details["out"] / 1e9
        entry_sol = position["entry_sol"]
        pnl_pct = ((sol_received - entry_sol) / entry_sol) * 100
        print(f"  ✅ Got {sol_received:.6f} SOL | P&L: {pnl_pct:+.2f}% | TX: {details['sig'][:20]}...")
        
        # Remove position
        positions = load_positions()
        if symbol in positions:
            del positions[symbol]
        save_positions(positions)
        
        log_trade({
            "time": datetime.utcnow().isoformat(),
            "action": "SELL", "symbol": symbol,
            "token_in": balance, "sol_out": sol_received,
            "entry_sol": entry_sol, "pnl_pct": pnl_pct,
            "tx": details["sig"],
        })
        return True, sol_received
    else:
        print(f"  ❌ Sell failed: {details}")
        return False, 0

def check_positions(opportunities_map):
    """Check open positions for stop-loss, take-profit, or max-hold"""
    positions = load_positions()
    if not positions:
        return
    
    for symbol, pos in list(positions.items()):
        pos["cycles_held"] = pos.get("cycles_held", 0) + 1
        mint = pos["mint"]
        decimals = pos["decimals"]
        
        # Get current value via Jupiter quote (no execution)
        balance = get_token_balance(mint)
        if balance < 0.0001:
            print(f"  ⚠️ {symbol} position empty, removing")
            del positions[symbol]
            continue
        
        token_raw = int(balance * (10 ** decimals))
        resp = requests.get(JUP_QUOTE, params={
            "inputMint": mint, "outputMint": SOL_MINT,
            "amount": str(token_raw), "slippageBps": "500",
        }, timeout=15)
        quote = resp.json()
        if "outAmount" not in quote:
            continue
        
        current_sol_value = int(quote["outAmount"]) / 1e9
        entry_sol = pos["entry_sol"]
        pnl_pct = ((current_sol_value - entry_sol) / entry_sol) * 100
        
        hold_cycles = pos["cycles_held"]
        reason = None
        
        # Take profit
        if pnl_pct >= TAKE_PROFIT_PCT:
            reason = f"TAKE PROFIT (+{pnl_pct:.1f}%)"
        # Stop loss
        elif pnl_pct <= STOP_LOSS_PCT:
            reason = f"STOP LOSS ({pnl_pct:.1f}%)"
        # Max hold time
        elif hold_cycles >= MAX_HOLD_CYCLES:
            reason = f"MAX HOLD ({hold_cycles} cycles, P&L: {pnl_pct:+.1f}%)"
        # Momentum reversed - sell if token is now trending down
        elif symbol in opportunities_map:
            opp = opportunities_map[symbol]
            if opp["action"] == "SELL" and pnl_pct > -2:
                reason = f"MOMENTUM REVERSED (score: {opp['score']:.1f}, P&L: {pnl_pct:+.1f}%)"
        
        if reason:
            print(f"  📤 {symbol}: {reason} | Value: {current_sol_value:.6f} SOL")
            execute_sell(symbol, pos)
        else:
            print(f"  ⏸️ HOLD {symbol}: P&L {pnl_pct:+.2f}% | {hold_cycles}/{MAX_HOLD_CYCLES} cycles")
    
    save_positions(positions)

def run_cycle(cycle_num):
    """Run one trading cycle"""
    print(f"\n{'='*50}")
    print(f"Cycle {cycle_num} — {datetime.utcnow().isoformat()[:19]}")
    print(f"{'='*50}")
    
    sol_balance = get_sol_balance()
    print(f"SOL balance: {sol_balance:.6f}")
    
    if sol_balance < MIN_SOL_RESERVE:
        print(f"⚠️ Below SOL reserve ({sol_balance:.4f} < {MIN_SOL_RESERVE}). Stopping.")
        return False
    
    # Scan market
    print("\n📊 Market Scan:")
    opportunities = scan_market()
    opp_map = {o["symbol"]: o for o in opportunities}
    
    for opp in opportunities:
        print(f"  {opp['symbol']:8} | Score: {opp['score']:+5.1f} | "
              f"1h: {opp['chg1h']:+5.1f}% 6h: {opp['chg6h']:+5.1f}% 24h: {opp['chg24h']:+5.1f}% "
              f"| Liq: ${opp['liq']:>10,.0f} | {opp['action']}")
    
    # Check existing positions first
    positions = load_positions()
    if positions:
        print(f"\n📋 Open positions: {len(positions)}")
        check_positions(opp_map)
    else:
        print("\n📋 No open positions")
    
    # Look for new buy opportunities
    available_sol = sol_balance - MIN_SOL_RESERVE
    if available_sol >= 0.01:
        buy_candidates = [o for o in opportunities if o["action"] == "BUY"]
        
        if buy_candidates:
            # Buy the top-scoring token
            best = buy_candidates[0]
            trade_sol = min(MAX_TRADE_SOL, available_sol)
            
            # Don't buy if we already hold this token
            if best["symbol"] not in load_positions():
                print(f"\n🎯 Best opportunity: {best['symbol']} (score: {best['score']:.1f})")
                execute_buy(best["symbol"], best["mint"], best["decimals"], trade_sol)
            else:
                print(f"\n⏸️ Already holding {best['symbol']}, waiting for exit signal")
        else:
            print("\n😴 No buy signals. Market is flat or declining.")
    else:
        print(f"\n⚠️ Not enough SOL to trade ({available_sol:.4f} SOL available)")
    
    # Show cumulative P&L
    log = load_trade_log()
    sells = [t for t in log if t["action"] == "SELL"]
    if sells:
        total_pnl = sum(s.get("pnl_pct", 0) for s in sells)
        total_sol = sum(s.get("sol_out", 0) for s in sells)
        wins = len([s for s in sells if s.get("pnl_pct", 0) > 0])
        losses = len([s for s in sells if s.get("pnl_pct", 0) <= 0])
        print(f"\n📈 Cumulative: {len(sells)} sells | Win rate: {wins}/{wins+losses} | "
              f"Avg P&L: {total_pnl/max(len(sells),1):+.1f}% | Total SOL returned: {total_sol:.6f}")
    
    return True

def main():
    print("🤖 ArmaBase Smart Solana Trading Bot")
    print(f"Wallet: {WALLET}")
    print(f"Strategy: Momentum-based buy/sell with {TAKE_PROFIT_PCT}% TP / {STOP_LOSS_PCT}% SL")
    print(f"Trade size: max {MAX_TRADE_SOL} SOL | Reserve: {MIN_SOL_RESERVE} SOL")
    print(f"Tokens: {', '.join(TRADEABLE_TOKENS.keys())}")
    print()
    
    cycle = 0
    while True:
        cycle += 1
        try:
            ok = run_cycle(cycle)
            if not ok:
                break
        except Exception as e:
            print(f"❌ Cycle error: {e}")
        
        print(f"\n⏳ Waiting {CYCLE_DELAY}s until next cycle...")
        time.sleep(CYCLE_DELAY)

if __name__ == "__main__":
    main()
