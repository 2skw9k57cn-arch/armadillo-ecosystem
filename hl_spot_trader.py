#!/usr/bin/env python3
"""
Hyperliquid Spot Trading Module
================================
Trades HL spot markets alongside existing perps. No liquidation risk.

Strategy:
1. Fetch HL spot markets (available pairs)
2. Get orderbook mid prices + spreads
3. Look for mean-reversion entries (price below recent average)
4. Buy spot when:
   - RSI < 35 (oversold)
   - Price below 1h EMA
   - Spread is tight (liquid market)
5. Sell when:
   - RSI > 65 (overbought)
   - Price above 1h EMA by 2%+
   - Or stop-loss at -5%
6. Position size: 10% of available HL USDC per trade
7. Max 3 concurrent spot positions

Runs every 30 minutes via cron.
"""

import json, time, os, subprocess, requests, math
from datetime import datetime, timezone
from collections import defaultdict

# ============ CONFIG ============
SAINT_ID = "019f9f75-493a-7011-b547-aa9c2df1a1ac"
ARMA_BASE_ID = "019fbb50-31de-7e2f-be3b-2225023960b3"
HL_API = "https://api.hyperliquid.xyz/info"
HL_CHAIN = 1337

# Trading config
MAX_POSITION_PCT = 0.10      # Max 10% of HL USDC per spot position
MAX_POSITIONS = 3             # Max concurrent spot positions
MIN_HL_BALANCE = 2.0          # Min HL USDC to trade
MIN_SPREAD_PCT = 0.5          # Min spread to consider (liquidity check)
TAKE_PROFIT_PCT = 0.03        # +3% take profit
STOP_LOSS_PCT = -0.05         # -5% stop loss
RSI_OVERSOLD = 35             # Buy threshold
RSI_OVERBOUGHT = 65           # Sell threshold
EMA_PERIOD = 12               # 1h EMA period

# Spot markets to scan (HL spot markets)
SPOT_MARKETS = ["BTC", "ETH", "SOL", "PURR", "UMA"]

SPOT_LOG = "/workspace/hl_spot_log.json"
SPOT_POSITIONS_FILE = "/workspace/hl_spot_positions.json"

# ============ UTILITIES ============
def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1

def use_saint():
    run(f"acp agent use --agent-id {SAINT_ID}")

def log(action, details):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details
    }
    log_data = []
    if os.path.exists(SPOT_LOG):
        try:
            with open(SPOT_LOG) as f:
                log_data = json.load(f)
        except Exception:
            pass
    log_data.append(entry)
    log_data = log_data[-200:]
    with open(SPOT_LOG, "w") as f:
        json.dump(log_data, f, indent=2)
    print(f"[{entry['timestamp'][:19]}] {action}: {json.dumps(details)[:200]}")

def load_positions():
    if os.path.exists(SPOT_POSITIONS_FILE):
        try:
            with open(SPOT_POSITIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_positions(positions):
    with open(SPOT_POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

# ============ HL API ============
def hl_get_meta():
    """Get HL metadata including spot markets"""
    try:
        r = requests.post(HL_API, json={"type": "meta"}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def hl_get_spot_meta():
    """Get spot asset metadata"""
    try:
        r = requests.post(HL_API, json={"type": "spotMeta"}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def hl_get_orderbook(coin):
    """Get L2 orderbook for a coin"""
    try:
        r = requests.post(HL_API, json={
            "type": "l2Book",
            "coin": coin
        }, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def hl_get_candles(coin, interval="1h", hours=24):
    """Fetch candle data"""
    end = int(time.time() * 1000)
    start = end - hours * 3600000
    try:
        r = requests.post(HL_API, json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end}
        }, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []

def hl_get_account_state():
    """Get HL account state (balances + positions)"""
    # Use ACP trade hl-status
    out, _, _ = run("acp trade hl-status --json 2>&1", timeout=20)
    try:
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        return json.loads(raw)
    except Exception:
        return None

# ============ INDICATORS ============
def calculate_rsi(candles, period=14):
    """Calculate RSI from candle data"""
    if len(candles) < period + 1:
        return 50  # Neutral
    closes = [float(c.get("c", 0)) for c in candles[-(period+1):]]
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_ema(candles, period=12):
    """Calculate EMA from candle data"""
    if len(candles) < period:
        return 0
    closes = [float(c.get("c", 0)) for c in candles[-period*2:]]
    multiplier = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = (c - ema) * multiplier + ema
    return ema

def get_mid_price(coin):
    """Get mid price from orderbook"""
    book = hl_get_orderbook(coin)
    if not book:
        return 0, 0
    levels = book.get("levels", [])
    if not levels or len(levels) < 2:
        return 0, 0
    # HL format: levels = [[bids...], [asks...]]
    bids = levels[0]  # First list = bids (descending)
    asks = levels[1]  # Second list = asks (ascending)
    if not bids or not asks:
        return 0, 0
    best_bid = float(bids[0].get("px", 0))
    best_ask = float(asks[0].get("px", 0))
    if best_bid == 0 or best_ask == 0:
        return 0, 0
    mid = (best_bid + best_ask) / 2
    spread_pct = ((best_ask - best_bid) / mid) * 100
    return mid, spread_pct

# ============ TRADING ============
def get_hl_usdc_balance():
    """Get available USDC on HL via direct API"""
    # First try ACP hl-status
    out, _, _ = run("acp trade hl-status --json 2>&1", timeout=20)
    try:
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        state = json.loads(raw)
        if isinstance(state, dict):
            # Try different response formats
            if "marginState" in state:
                return float(state["marginState"].get("availableMargin", 0))
            elif "withdrawable" in state:
                return float(state["withdrawable"])
            elif "accountValue" in state:
                return float(state["accountValue"])
    except Exception:
        pass
    
    # Fallback: use oversight state which has HL balance
    try:
        with open("/workspace/oversight_state.json") as f:
            oversight = json.load(f)
        for check in oversight.get("checks", []):
            if check.get("check") == "hl_positions":
                return float(check.get("withdrawable", 0))
    except Exception:
        pass
    return 0.0

def execute_spot_buy(token, amount_usdc):
    """Buy spot on HL via ACP"""
    cmd = f"acp trade --token-in usdc --chain-in {HL_CHAIN} --amount-in {amount_usdc} --token-out {token} --chain-out {HL_CHAIN} --json 2>&1"
    out, err, rc = run(cmd, timeout=60)
    return out, err, rc

def execute_spot_sell(token, amount):
    """Sell spot on HL via ACP"""
    cmd = f"acp trade --token-in {token} --chain-in {HL_CHAIN} --amount-in {amount} --token-out usdc --chain-out {HL_CHAIN} --json 2>&1"
    out, err, rc = run(cmd, timeout=60)
    return out, err, rc

def scan_and_trade():
    print("\n📈 HL Spot Trading Module —", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    use_saint()

    hl_balance = get_hl_usdc_balance()
    print(f"   HL USDC available: ${hl_balance:.2f}")

    if hl_balance < MIN_HL_BALANCE:
        print(f"   ⏭️ Insufficient HL balance (need ${MIN_HL_BALANCE})")
        log("skip", {"reason": "insufficient_hl_balance", "balance": hl_balance})
        return

    positions = load_positions()
    print(f"   Open spot positions: {len(positions)}")

    # Manage existing positions first
    for token, pos in list(positions.items()):
        mid_price, spread = get_mid_price(token)
        if mid_price == 0:
            continue

        entry_price = pos.get("entry_price", 0)
        if entry_price == 0:
            continue

        pnl_pct = (mid_price - entry_price) / entry_price
        print(f"   📊 {token}: entry=${entry_price:.4f} current=${mid_price:.4f} PnL={pnl_pct*100:.1f}%")

        # Take profit
        if pnl_pct >= TAKE_PROFIT_PCT:
            print(f"   ✅ {token}: Take profit triggered (+{pnl_pct*100:.1f}%)")
            amount = pos.get("amount", 0)
            out, err, rc = execute_spot_sell(token, amount)
            if rc == 0 or "success" in out.lower():
                log("take_profit", {"token": token, "pnl_pct": pnl_pct, "amount": amount})
                del positions[token]
                save_positions(positions)
                print(f"   ✅ Sold {amount} {token}")
            else:
                print(f"   ❌ Sell failed: {err[:100]}")
            continue

        # Stop loss
        if pnl_pct <= STOP_LOSS_PCT:
            print(f"   🛑 {token}: Stop loss triggered ({pnl_pct*100:.1f}%)")
            amount = pos.get("amount", 0)
            out, err, rc = execute_spot_sell(token, amount)
            if rc == 0 or "success" in out.lower():
                log("stop_loss", {"token": token, "pnl_pct": pnl_pct, "amount": amount})
                del positions[token]
                save_positions(positions)
                print(f"   🛑 Sold {amount} {token} at loss")
            else:
                print(f"   ❌ Sell failed: {err[:100]}")
            continue

        # RSI-based exit
        candles = hl_get_candles(token, "1h", 48)
        rsi = calculate_rsi(candles)
        if rsi > RSI_OVERBOUGHT:
            print(f"   📉 {token}: RSI overbought ({rsi:.0f}), selling")
            amount = pos.get("amount", 0)
            out, err, rc = execute_spot_sell(token, amount)
            if rc == 0 or "success" in out.lower():
                log("rsi_exit", {"token": token, "rsi": rsi, "pnl_pct": pnl_pct, "amount": amount})
                del positions[token]
                save_positions(positions)
                print(f"   📉 Sold {amount} {token}")

    # Look for new entries
    if len(positions) >= MAX_POSITIONS:
        print(f"   Max positions reached ({MAX_POSITIONS})")
        log("skip", {"reason": "max_positions"})
        return

    available_usdc = hl_balance * MAX_POSITION_PCT
    if available_usdc < 1.0:
        print(f"   Position size too small (${available_usdc:.2f})")
        return

    for token in SPOT_MARKETS:
        if token in positions:
            continue  # Already holding
        if len(positions) >= MAX_POSITIONS:
            break

        mid_price, spread = get_mid_price(token)
        if mid_price == 0 or spread > MIN_SPREAD_PCT * 2:
            continue  # No price or illiquid

        candles = hl_get_candles(token, "1h", 48)
        if len(candles) < 15:
            continue

        rsi = calculate_rsi(candles)
        ema = calculate_ema(candles)

        print(f"   🔍 {token}: price=${mid_price:.4f} RSI={rsi:.0f} EMA=${ema:.4f} spread={spread:.2f}%")

        # Buy signal: oversold + below EMA
        if rsi < RSI_OVERSOLD and mid_price < ema:
            print(f"   🟢 {token}: BUY signal (RSI={rsi:.0f}, below EMA)")
            trade_usdc = min(available_usdc, hl_balance * MAX_POSITION_PCT)
            out, err, rc = execute_spot_buy(token, trade_usdc)
            
            if rc == 0 or "success" in out.lower():
                # Try to parse amount received
                try:
                    raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
                    result = json.loads(raw)
                    amount_received = float(result.get("amountOut", 0))
                except Exception:
                    amount_received = trade_usdc / mid_price  # Estimate

                positions[token] = {
                    "entry_price": mid_price,
                    "amount": amount_received,
                    "usdc_spent": trade_usdc,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "rsi_at_entry": rsi,
                }
                save_positions(positions)
                log("buy", {
                    "token": token, 
                    "price": mid_price, 
                    "usdc_spent": trade_usdc,
                    "amount": amount_received,
                    "rsi": rsi,
                })
                print(f"   ✅ Bought {amount_received:.6f} {token} for ${trade_usdc:.2f}")
            else:
                print(f"   ❌ Buy failed: {err[:100]}")
                log("buy_failed", {"token": token, "error": err[:200]})

    print(f"\n   Summary: {len(positions)} open positions")

if __name__ == "__main__":
    scan_and_trade()
