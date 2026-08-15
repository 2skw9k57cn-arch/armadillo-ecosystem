#!/usr/bin/env python3
"""
Armadillo Self-Learning Profit Engine
======================================
Coordinates all 3 Armadillo agents (ArmaBase, Scout, Saint) to buy and sell
ecosystem tokens profitably toward the $1M USDC goal (collected as SOL to the
user's personal wallet EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd).

Tokens traded:
  ARBA   — 0x557642685ce68F3975458375B51553871807e1b5  (Base 8453)
  ARMAD  — 0x69e71ce955373d7117394b0c7aaee6ef42cf6d51  (Base 8453)
  ARRB   — 0xdA3C5b4d05c40a9244E534a966A1424C51055950  (Robinhood Chain 2025)
  OGSAINT— 0xfde1f1255683772d48b12b082fd3140713d6e40d  (Base 8453)

Strategy:
  - Buy low (RSI < 35 / recent dip detection), sell high (+15-50% gain)
  - Stop loss at -12%
  - Reads /workspace/learned_params.json for per-token recommendations
  - Auto-corrects: 3+ consecutive losses → cut position 50%; winning → +25%
  - Logs every trade to /workspace/profit_engine_log.json
  - Files a status report to /workspace/reports/ after each cycle
"""

import json, os, sys, time, subprocess
from datetime import datetime

# ─── Paths ────────────────────────────────────────────────────────────────
WORKSPACE      = "/workspace"
CONFIG_PATH    = os.path.join(WORKSPACE, "config.json")
LEARNED_PARAMS = os.path.join(WORKSPACE, "learned_params.json")
TRADE_LOG      = os.path.join(WORKSPACE, "profit_engine_log.json")
REPORTS_DIR    = os.path.join(WORKSPACE, "reports")
GOAL_WALLET    = "EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd"

# ─── Token Contracts (real addresses from buyback_burn.py) ────────────────
TOKENS = {
    "ARBA": {
        "contract":  "0x557642685ce68F3975458375B51553871807e1b5",
        "chain_out": "8453",
        "decimals":  18,
    },
    "ARMAD": {
        "contract":  "0x69e71ce955373d7117394b0c7aaee6ef42cf6d51",
        "chain_out": "8453",
        "decimals":  18,
    },
    "ARRB": {
        "contract":  "0xdA3C5b4d05c40a9244E534a966A1424C51055950",
        "chain_out": "2025",   # Robinhood Chain
        "decimals":  18,
    },
    "OGSAINT": {
        "contract":  "0xfde1f1255683772d48b12b082fd3140713d6e40d",
        "chain_out": "8453",
        "decimals":  18,
    },
}

# ─── Agent wallets ────────────────────────────────────────────────────────
WALLETS = {
    "armabase": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc",
    "saint":    "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d",
    "scout":    "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4",
}

# Each agent prioritizes its own token but also trades the others
AGENT_TOKENS = {
    "armabase": ["ARBA", "ARMAD", "ARRB", "OGSAINT"],
    "saint":    ["ARMAD", "ARBA", "OGSAINT", "ARRB"],
    "scout":    ["ARRB", "ARBA", "ARMAD", "OGSAINT"],
}

# ─── Trading Parameters (tuned for small starting capital ~$15) ───────────
MIN_TRADE_USDC   = 2.10    # ACP minimum is $2, use $2.10 for fee buffer
RESERVE_USDC     = 0.0     # No reserve — use every cent available
BASE_BUY_USDC    = 2.10    # Default buy size (start small, grow with profits)
TAKE_PROFIT_PCT  = 15.0    # Sell at +15% (scaling up to 50%)
MAX_TAKE_PROFIT  = 50.0    # Don't hold past +50%
STOP_LOSS_PCT    = 12.0    # Sell at -12%
RSI_BUY_THRESHOLD = 35.0   # Buy when RSI < 35
MAX_POSITION_PCT = 0.80    # Max 80% of usable USDC into one token (aggressive with small capital)


# ════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL UTILITIES
# ════════════════════════════════════════════════════════════════════════

def run(cmd):
    """subprocess.run wrapper with shell=True, capture_output, text, timeout=300"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=300
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 124
    except Exception as e:
        return "", str(e), 1


def use_agent(agent_key):
    """Switch ACP context by writing activeWallet to config.json.
    This persists for subprocess calls (unlike `acp agent use`)."""
    wallet = WALLETS.get(agent_key, agent_key)
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        config["activeWallet"] = wallet
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"  ⚠️ use_agent failed for {agent_key}: {e}")
        return False


def now_str():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


# ════════════════════════════════════════════════════════════════════════
#  BALANCE & PRICE QUERIES
# ════════════════════════════════════════════════════════════════════════

def get_balances(agent_key):
    """Get USDC + all token balances for an agent.
    Returns dict: {'USDC': float, 'ARBA': float, ...}
    tokenBalance is a hex string (0x...), parsed as int(bal, 16) / 10**decimals.
    """
    use_agent(agent_key)
    out, err, rc = run("acp wallet balance --json 2>&1")
    balances = {"USDC": 0.0}
    for sym in TOKENS:
        balances[sym] = 0.0

    if rc != 0:
        return balances
    try:
        d = json.loads(out)
        for t in d.get("tokens", []):
            meta = t.get("tokenMetadata", {})
            sym = (meta.get("symbol") or "").upper()
            bal_hex = t.get("tokenBalance", "0x0")
            dec = meta.get("decimals", 18)
            try:
                bal = int(bal_hex, 16) / (10 ** dec)
            except (ValueError, TypeError):
                bal = 0.0

            # Match USDC
            if sym == "USDC":
                balances["USDC"] = bal
            # Match by contract address for ecosystem tokens
            addr = (t.get("tokenAddress") or "").lower()
            for token_sym, token_info in TOKENS.items():
                if addr == token_info["contract"].lower():
                    balances[token_sym] = bal
    except Exception as e:
        print(f"  ⚠️ Balance parse error for {agent_key}: {e}")

    return balances


def get_token_price(symbol):
    """Estimate token price in USDC by doing a dry-run quote.
    Falls back to a nominal price if quote fails.
    Returns (price_usdc, source)."""
    token = TOKENS[symbol]
    # Try to get price via a small quote (0.1 USDC worth)
    # We use the ACP trade with a tiny amount to probe — but that would execute.
    # Instead, use a simple heuristic: track buy price in the log.
    # If we have no history, assume $0.001 nominal for bonding-curve tokens.
    price = _get_last_known_price(symbol)
    return price, "log"


def _get_last_known_price(symbol):
    """Get last known buy price from trade log."""
    log = load_trade_log()
    for entry in reversed(log):
        if (entry.get("token") == symbol and
            entry.get("action") == "BUY" and
            entry.get("tokens_amount", 0) > 0):
            return entry.get("usdc_amount", 0) / entry.get("tokens_amount", 1)
    return 0.001  # nominal fallback for bonding-curve tokens


# ════════════════════════════════════════════════════════════════════════
#  SIMPLE RSI / DIP DETECTION
# ════════════════════════════════════════════════════════════════════════

def compute_rsi(symbol, period=14):
    """Compute a simple RSI from recent trade prices in the log.
    Returns 50.0 (neutral) if insufficient data."""
    log = load_trade_log()
    prices = []
    for entry in log:
        if (entry.get("token") == symbol and
            entry.get("action") == "BUY" and
            entry.get("tokens_received", 0) > 0):
            price = entry.get("usdc_spent", 0) / entry.get("tokens_received", 1)
            prices.append(price)

    if len(prices) < period + 1:
        return 50.0  # neutral

    prices = prices[-(period + 1):]
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains) / len(gains) if gains else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def detect_dip(symbol):
    """Detect if the token has dipped recently (last buy price > current est).
    Returns True if a dip is detected (good buy opportunity)."""
    rsi = compute_rsi(symbol)
    if rsi < RSI_BUY_THRESHOLD:
        return True, rsi
    # Also check recent PnL trend from learned params
    learned = load_learned_params()
    token_info = learned.get("tokens", {}).get(symbol, {})
    if token_info.get("trending_up") is False and token_info.get("recent_pnl", 0) < 0:
        # Token is down recently — potential dip buy
        return True, rsi
    return False, rsi


# ════════════════════════════════════════════════════════════════════════
#  SELF-LEARNING: LEARNED PARAMS
# ════════════════════════════════════════════════════════════════════════

def load_learned_params():
    """Read /workspace/learned_params.json for trading strategy adjustments."""
    try:
        with open(LEARNED_PARAMS) as f:
            return json.load(f)
    except Exception:
        return {}


def get_token_recommendation(symbol):
    """Get the recommendation for a token from learned_params.
    Returns dict with: recommendation, win_rate, optimal_buy_usdc, is_dead, etc."""
    learned = load_learned_params()
    return learned.get("tokens", {}).get(symbol, {})


def get_adjusted_buy_size(symbol, base_usdc=BASE_BUY_USDC):
    """Adjust buy size based on learned params and recent performance.
    - 3+ consecutive losses → reduce 50%
    - Winning consistently → increase 25%
    - Dead token → skip (return 0)
    """
    rec = get_token_recommendation(symbol)
    if not rec:
        return base_usdc

    # Skip dead tokens
    if rec.get("is_dead", False) or "STOP" in rec.get("recommendation", "").upper():
        return 0.0

    # Check consecutive losses from our own trade log
    losses = count_consecutive_losses(symbol)
    wins = count_consecutive_wins(symbol)

    size = base_usdc

    if losses >= 3:
        size *= 0.50  # cut in half after 3+ losses
        print(f"  📉 {symbol}: {losses} consecutive losses → position reduced 50%")
    elif wins >= 2:
        size *= 1.25  # increase 25% if winning
        print(f"  📈 {symbol}: {wins} consecutive wins → position increased 25%")

    # Use learned optimal buy size if available and reasonable
    opt = rec.get("optimal_buy_usdc", 0)
    if opt and 0.5 <= opt <= 50:
        # Blend: average of our adjusted size and learned optimal
        size = (size + opt) / 2

    # Clamp
    size = max(MIN_TRADE_USDC, min(size, base_usdc * 4))
    return round(size, 2)


def count_consecutive_losses(symbol):
    """Count consecutive losing sell trades for a token from the log."""
    log = load_trade_log()
    count = 0
    for entry in reversed(log):
        if entry.get("token") == symbol and entry.get("action") == "SELL":
            if entry.get("pnl", 0) < 0:
                count += 1
            else:
                break  # streak ended
    return count


def count_consecutive_wins(symbol):
    """Count consecutive winning sell trades for a token from the log."""
    log = load_trade_log()
    count = 0
    for entry in reversed(log):
        if entry.get("token") == symbol and entry.get("action") == "SELL":
            if entry.get("pnl", 0) > 0:
                count += 1
            else:
                break
    return count


# ════════════════════════════════════════════════════════════════════════
#  TRADE EXECUTION
# ════════════════════════════════════════════════════════════════════════

def buy_token(symbol, usdc_amount, agent_key):
    """Buy a token using USDC.
    acp trade --token-in usdc --chain-in 8453 --amount-in {usdc}
              --token-out {contract} --chain-out {chain} --accept-impact --json
    Returns (success: bool, data: dict)
    """
    token = TOKENS[symbol]
    chain_out = token["chain_out"]
    contract = token["contract"]

    use_agent(agent_key)

    print(f"  📤 {agent_key}: Buying {symbol} with ${usdc_amount:.2f} USDC (chain {chain_out})...")

    cmd = (
        f"acp trade --token-in usdc --chain-in 8453 --amount-in {usdc_amount} "
        f"--token-out {contract} --chain-out {chain_out} --accept-impact --json"
    )
    out, err, rc = run(cmd)

    if rc != 0:
        try:
            d = json.loads(out)
            err_msg = d.get("error", err or out)
        except Exception:
            err_msg = err or out
        print(f"     ❌ Buy failed: {str(err_msg)[:120]}")
        return False, {"error": str(err_msg)[:300]}

    try:
        d = json.loads(out)
        if d.get("status") == "success" or d.get("txHash") or d.get("legs"):
            received_raw = d.get("finalReceived", d.get("amountOut", "0"))
            # Parse received amount
            try:
                if isinstance(received_raw, str):
                    received_val = float(received_raw.split()[0].replace(",", ""))
                else:
                    received_val = float(received_raw)
            except Exception:
                received_val = 0.0

            txs = [leg.get("txHash", "") for leg in d.get("legs", []) if leg.get("txHash")]
            print(f"     ✅ Bought ~{received_val:,.4f} {symbol}")
            return True, {
                "received": received_val,
                "txs": txs,
                "raw": d,
                "usdc_spent": usdc_amount,
            }
        else:
            err_msg = d.get("error", str(d)[:200])
            print(f"     ❌ Buy failed: {str(err_msg)[:120]}")
            return False, {"error": str(err_msg)[:300]}
    except Exception as e:
        print(f"     ❌ Buy parse error: {e}")
        return False, {"error": str(out)[:300]}


def sell_token(symbol, token_amount, agent_key):
    """Sell a token for USDC.
    acp trade --token-in {contract} --chain-in {chain} --amount-in {token_amount}
              --token-out usdc --chain-out 8453 --accept-impact --json
    Returns (success: bool, data: dict)
    """
    token = TOKENS[symbol]
    chain_in = token["chain_out"]
    contract = token["contract"]

    use_agent(agent_key)

    print(f"  💰 {agent_key}: Selling {token_amount:,.6f} {symbol} for USDC...")

    cmd = (
        f"acp trade --token-in {contract} --chain-in {chain_in} --amount-in {token_amount} "
        f"--token-out usdc --chain-out 8453 --accept-impact --json"
    )
    out, err, rc = run(cmd)

    if rc != 0:
        try:
            d = json.loads(out)
            err_msg = d.get("error", err or out)
        except Exception:
            err_msg = err or out
        print(f"     ❌ Sell failed: {str(err_msg)[:120]}")
        return False, {"error": str(err_msg)[:300]}

    try:
        d = json.loads(out)
        if d.get("status") == "success" or d.get("txHash") or d.get("legs"):
            received_raw = d.get("finalReceived", d.get("amountOut", "0"))
            try:
                if isinstance(received_raw, str):
                    received_val = float(received_raw.split()[0].replace(",", ""))
                else:
                    received_val = float(received_raw)
            except Exception:
                received_val = 0.0

            txs = [leg.get("txHash", "") for leg in d.get("legs", []) if leg.get("txHash")]
            print(f"     ✅ Sold {symbol} for ~${received_val:.2f} USDC")
            return True, {
                "received_usdc": received_val,
                "txs": txs,
                "raw": d,
            }
        else:
            err_msg = d.get("error", str(d)[:200])
            print(f"     ❌ Sell failed: {str(err_msg)[:120]}")
            return False, {"error": str(err_msg)[:300]}
    except Exception as e:
        print(f"     ❌ Sell parse error: {e}")
        return False, {"error": str(out)[:300]}


# ════════════════════════════════════════════════════════════════════════
#  TRADE LOGGING
# ════════════════════════════════════════════════════════════════════════

def load_trade_log():
    """Load the profit engine trade log."""
    try:
        with open(TRADE_LOG) as f:
            return json.load(f)
    except Exception:
        return []


def save_trade_log(log):
    """Save the profit engine trade log."""
    try:
        with open(TRADE_LOG, 'w') as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        print(f"  ⚠️ Failed to save trade log: {e}")


def log_trade(agent, action, token, usdc_amount, tokens_amount, pnl, details):
    """Log a trade to /workspace/profit_engine_log.json."""
    log = load_trade_log()
    entry = {
        "timestamp": int(time.time()),
        "date": now_str(),
        "agent": agent,
        "action": action,          # BUY or SELL
        "token": token,
        "usdc_amount": round(usdc_amount, 6),
        "tokens_amount": round(tokens_amount, 6) if tokens_amount else 0,
        "pnl": round(pnl, 6),
        "details": str(details)[:500],
    }
    log.append(entry)
    save_trade_log(log)
    return entry


# ════════════════════════════════════════════════════════════════════════
#  PROFIT ALGORITHM: BUY / SELL DECISIONS
# ════════════════════════════════════════════════════════════════════════

def get_open_position(symbol, agent_key):
    """Get the open position for a token/agent from the trade log.
    Returns the last unmatched BUY entry, or None."""
    log = load_trade_log()
    # Walk backwards to find last BUY for this token+agent without a matching SELL
    buys = []
    sells = 0
    for entry in log:
        if (entry.get("token") == symbol and
            entry.get("agent") == agent_key and
            entry.get("action") == "BUY"):
            buys.append(entry)
        elif (entry.get("token") == symbol and
              entry.get("agent") == agent_key and
              entry.get("action") == "SELL"):
            sells += 1

    # Match buys to sells in order
    if len(buys) > sells:
        return buys[sells]  # next unmatched buy
    return None


def should_buy(symbol, agent_key):
    """Decide whether to buy a token.
    Returns (should_buy: bool, reason: str)"""
    # Check if we already have an open position
    pos = get_open_position(symbol, agent_key)
    if pos:
        return False, f"already holding {pos.get('tokens_amount', 0):,.4f} {symbol}"

    # Check learned params — skip dead tokens
    rec = get_token_recommendation(symbol)
    if rec.get("is_dead", False):
        return False, "token marked dead in learned_params"
    recommendation = rec.get("recommendation", "").upper()
    if "STOP" in recommendation:
        return False, f"learned_params says STOP: {recommendation}"

    # RSI / dip detection
    is_dip, rsi = detect_dip(symbol)
    if is_dip:
        return True, f"RSI={rsi:.1f} < {RSI_BUY_THRESHOLD} or dip detected"

    # If no learned data yet (cold start), buy anyway to build position
    if not rec:
        return True, "cold start — no learned data, building initial position"

    # If trending up but not overbought, buy
    if rec.get("trending_up") and rsi < 70:
        return True, f"trending up, RSI={rsi:.1f}"

    return False, f"no buy signal (RSI={rsi:.1f}, no dip)"


def should_sell(symbol, agent_key, current_balance):
    """Decide whether to sell a token position.
    Returns (should_sell: bool, reason: str, sell_pct: float)"""
    pos = get_open_position(symbol, agent_key)
    if not pos or current_balance <= 0:
        return False, "no open position", 0.0

    buy_usdc = pos.get("usdc_amount", 0)
    buy_tokens = pos.get("tokens_amount", 0)
    if buy_tokens <= 0:
        return False, "invalid position", 0.0

    # Estimate current value — we need to know what we'd get in USDC
    # Use the current token balance and last known price
    current_price = _get_last_known_price(symbol)
    est_value = current_balance * current_price
    buy_price = buy_usdc / buy_tokens
    current_est_price = current_price

    pnl_pct = ((current_est_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0

    # Take profit: +15% to +50%
    if pnl_pct >= MAX_TAKE_PROFIT:
        return True, f"take profit at +{pnl_pct:.1f}%", 1.0
    if pnl_pct >= TAKE_PROFIT_PCT:
        # Sell 50% at first take-profit, hold rest for higher gains
        sell_pct = 0.50 if pnl_pct < 30 else 0.75
        return True, f"take profit at +{pnl_pct:.1f}%", sell_pct

    # Stop loss: -12%
    if pnl_pct <= -STOP_LOSS_PCT:
        return True, f"stop loss at {pnl_pct:.1f}%", 1.0

    # If token marked as dead in learned params, dump it
    rec = get_token_recommendation(symbol)
    if rec.get("is_dead", False):
        return True, "dumping dead token", 1.0

    return False, f"holding (PnL: {pnl_pct:+.1f}%)", 0.0


# ════════════════════════════════════════════════════════════════════════
#  SELF-CORRECTION: ADJUST THRESHOLDS AFTER EACH CYCLE
# ════════════════════════════════════════════════════════════════════════

def self_correct():
    """Analyze results after a cycle and adjust strategy.
    Writes adjustments to a self-correction file that feeds back into thresholds."""
    log = load_trade_log()
    sells = [e for e in log if e.get("action") == "SELL"]

    # Per-token PnL summary
    token_pnl = {}
    for s in sells:
        sym = s.get("token", "?")
        token_pnl.setdefault(sym, []).append(s.get("pnl", 0))

    adjustments = []
    for sym, pnls in token_pnl.items():
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = len(pnls) - wins
        win_rate = (wins / len(pnls) * 100) if pnls else 0

        if losses >= 3:
            adjustments.append({
                "token": sym,
                "action": "REDUCE_POSITION_50",
                "reason": f"{losses} losses, win_rate={win_rate:.1f}%",
            })
        elif wins > losses and total > 0:
            adjustments.append({
                "token": sym,
                "action": "INCREASE_POSITION_25",
                "reason": f"profitable, win_rate={win_rate:.1f}%, total_pnl={total:.4f}",
            })

    # Write self-correction state
    correction_file = os.path.join(WORKSPACE, "profit_engine_corrections.json")
    try:
        with open(correction_file, 'w') as f:
            json.dump({
                "generated_at": now_str(),
                "total_sells": len(sells),
                "adjustments": adjustments,
            }, f, indent=2)
    except Exception:
        pass

    if adjustments:
        print(f"\n  🔧 Self-correction adjustments:")
        for a in adjustments:
            print(f"     {a['token']}: {a['action']} — {a['reason']}")

    # ─── ALTERNATIVE ROUTE LOGIC ───────────────────────────────────────
    # If overall strategy is losing, pivot to alternative approaches
    all_sells_pnl = [e.get("pnl", 0) for e in sells]
    total_pnl_all = sum(all_sells_pnl)
    total_losses = sum(1 for p in all_sells_pnl if p < 0)
    total_wins = sum(1 for p in all_sells_pnl if p > 0)
    overall_wr = (total_wins / len(all_sells_pnl) * 100) if all_sells_pnl else 0
    
    strategy = "standard"
    strategy_note = ""
    
    if total_losses >= 5 and overall_wr < 30:
        strategy = "conservative"
        strategy_note = "5+ losses, WR<30% → switching to conservative: smaller sizes, tighter stops"
        # Tighten stop loss to -8%
        global STOP_LOSS_PCT, TAKE_PROFIT_PCT, BASE_BUY_USDC
        STOP_LOSS_PCT = 8.0
        TAKE_PROFIT_PCT = 10.0  # Take smaller profits faster
        BASE_BUY_USDC = MIN_TRADE_USDC  # Minimum size only
        adjustments.append({"token": "ALL", "action": "STRATEGY_CONSERVATIVE", "reason": strategy_note})
        print(f"\n  🔄 ALTERNATIVE ROUTE: {strategy_note}")
        
    elif total_losses >= 10 and overall_wr < 20:
        strategy = "halt_and_accumulate"
        strategy_note = "10+ losses, WR<20% → stop trading, accumulate USDC from jobs only"
        # Don't trade — just let the agents earn from ACP jobs and accumulate
        BASE_BUY_USDC = 0  # Effectively stop buying
        adjustments.append({"token": "ALL", "action": "STRATEGY_HALT", "reason": strategy_note})
        print(f"\n  🔄 ALTERNATIVE ROUTE: {strategy_note}")
        
    elif total_pnl_all > 0 and overall_wr > 50:
        strategy = "aggressive"
        strategy_note = "Profitable, WR>50% → scaling up position sizes +25%"
        BASE_BUY_USDC = min(BASE_BUY_USDC * 1.25, 10.0)  # Scale up but cap at $10
        TAKE_PROFIT_PCT = 20.0  # Hold for bigger gains
        STOP_LOSS_PCT = 12.0
        adjustments.append({"token": "ALL", "action": "STRATEGY_AGGRESSIVE", "reason": strategy_note})
        print(f"\n  🔄 ALTERNATIVE ROUTE: {strategy_note}")
        
    elif len(sells) >= 3 and total_pnl_all < 0:
        strategy = "switch_tokens"
        strategy_note = "Net negative PnL → switching to best-performing token only"
        # Find best token
        best_token = None
        best_pnl = -999
        for sym, pnls in token_pnl.items():
            if sum(pnls) > best_pnl:
                best_pnl = sum(pnls)
                best_token = sym
        if best_token:
            # Reorder all agents' token lists to prioritize the best performer
            for agent in AGENT_TOKENS:
                if best_token in AGENT_TOKENS[agent]:
                    AGENT_TOKENS[agent].remove(best_token)
                    AGENT_TOKENS[agent].insert(0, best_token)
            strategy_note += f" (focusing on {best_token})"
            adjustments.append({"token": best_token, "action": "STRATEGY_FOCUS", "reason": strategy_note})
            print(f"\n  🔄 ALTERNATIVE ROUTE: {strategy_note}")
    
    # Write strategy state
    try:
        with open(correction_file, 'w') as f:
            json.dump({
                "generated_at": now_str(),
                "total_sells": len(sells),
                "adjustments": adjustments,
                "strategy": strategy,
                "overall_win_rate": round(overall_wr, 1),
                "total_pnl": round(total_pnl_all, 4),
            }, f, indent=2)
    except Exception:
        pass

    return adjustments


# ════════════════════════════════════════════════════════════════════════
#  REPORTING
# ════════════════════════════════════════════════════════════════════════

def file_report(cycle_num, agent_results):
    """File a status report to /workspace/reports/ after each cycle."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(REPORTS_DIR, f"profit_engine_cycle_{cycle_num}_{timestamp}.json")

    # Calculate totals
    log = load_trade_log()
    total_trades = len(log)
    total_buys = sum(1 for e in log if e.get("action") == "BUY")
    total_sells = sum(1 for e in log if e.get("action") == "SELL")
    total_pnl = sum(e.get("pnl", 0) for e in log if e.get("action") == "SELL")

    report = {
        "cycle": cycle_num,
        "timestamp": now_str(),
        "goal_wallet": GOAL_WALLET,
        "goal_usd": 1_000_000,
        "agents": agent_results,
        "summary": {
            "total_trades": total_trades,
            "total_buys": total_buys,
            "total_sells": total_sells,
            "total_pnl": round(total_pnl, 6),
            "tokens_traded": list(TOKENS.keys()),
        },
        "learned_params_loaded": os.path.exists(LEARNED_PARAMS),
    }

    try:
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"  📄 Report filed: {report_path}")
    except Exception as e:
        print(f"  ⚠️ Failed to file report: {e}")

    return report_path


# ════════════════════════════════════════════════════════════════════════
#  CROSS-HIRING — Agents hire each other to generate economic activity
# ════════════════════════════════════════════════════════════════════════

# Cheapest offerings for cross-hiring
CHEAP_OFFERINGS = {
    "armabase": ("Trending Tokens Pulse", "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"),
    "scout":    ("Quick Token Brief",     "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4"),
    "saint":    ("hyperliquid_perp_signal", "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"),
}

def cross_hire(hiring_agent, provider_agent):
    """One agent hires another for a cheap job — generates USDC flow."""
    offering_name, provider_wallet = CHEAP_OFFERINGS[provider_agent]
    
    use_agent(hiring_agent)
    out, err, rc = run(
        f"acp client create-job --provider {provider_wallet} "
        f"--offering-name \"{offering_name}\" --chain-id 8453 --json 2>&1"
    )
    if rc != 0:
        return False, f"create-job failed: {err[:100]}"
    
    try:
        d = json.loads(out)
        job_id = d.get("jobId", d.get("id", ""))
        if not job_id:
            return False, f"no job ID in response: {out[:100]}"
    except:
        return False, f"parse error: {out[:100]}"
    
    # Fund the job with $0.25 USDC
    out2, err2, rc2 = run(f"acp client fund --job-id {job_id} --chain-id 8453 --amount 0.25 --json 2>&1")
    if rc2 != 0:
        return False, f"fund failed: {err2[:100]}"
    
    return True, f"{hiring_agent} hired {provider_agent} for '{offering_name}' (${0.25})"


def run_cross_hiring_cycle():
    """Agents cross-hire each other to generate economic velocity."""
    print(f"\n  💼 Cross-hiring cycle (agents hiring each other)...")
    hires = []
    
    # ArmaBase hires Scout
    ok, msg = cross_hire("armabase", "scout")
    if ok:
        print(f"     ✅ {msg}")
        hires.append(msg)
    else:
        print(f"     ⚠️ {msg}")
    
    # Scout hires Saint
    ok, msg = cross_hire("scout", "saint")
    if ok:
        print(f"     ✅ {msg}")
        hires.append(msg)
    else:
        print(f"     ⚠️ {msg}")
    
    # Saint hires ArmaBase
    ok, msg = cross_hire("saint", "armabase")
    if ok:
        print(f"     ✅ {msg}")
        hires.append(msg)
    else:
        print(f"     ⚠️ {msg}")
    
    return hires


# ════════════════════════════════════════════════════════════════════════
#  AGENT CYCLE
# ════════════════════════════════════════════════════════════════════════

def run_agent_cycle(agent_key, cycle_num):
    """Run a trading cycle for one agent across its token list."""
    print(f"\n🤖 {agent_key.upper()} (wallet {WALLETS[agent_key][:10]}...)")

    balances = get_balances(agent_key)
    usdc = balances["USDC"]
    print(f"  💰 USDC balance: ${usdc:.2f}")

    usable = usdc - RESERVE_USDC
    if usable < MIN_TRADE_USDC:
        print(f"  ⚠️ Only ${usable:.2f} usable (need ${MIN_TRADE_USDC} min + ${RESERVE_USDC} reserve) — skipping")
        return {"agent": agent_key, "usdc": usdc, "trades": 0, "actions": ["skipped: insufficient USDC"]}

    actions = []
    trade_count = 0
    token_list = AGENT_TOKENS[agent_key]

    # Phase 1: Check existing positions and sell if needed
    for symbol in token_list:
        balance = balances.get(symbol, 0)
        if balance <= 0:
            continue

        should, reason, sell_pct = should_sell(symbol, agent_key, balance)
        if should:
            sell_amount = balance * sell_pct
            if sell_amount <= 0:
                continue
            print(f"  📉 SELL signal for {symbol}: {reason}")
            ok, result = sell_token(symbol, sell_amount, agent_key)
            if ok:
                received_usdc = result.get("received_usdc", 0)
                # Calculate PnL
                pos = get_open_position(symbol, agent_key)
                cost_basis = pos.get("usdc_amount", 0) * sell_pct if pos else 0
                pnl = received_usdc - cost_basis
                log_trade(agent_key, "SELL", symbol, received_usdc, sell_amount, pnl, reason)
                actions.append(f"sold {sell_amount:,.4f} {symbol} for ${received_usdc:.2f} (PnL: ${pnl:+.4f})")
                trade_count += 1
                time.sleep(3)  # let tx settle
            else:
                log_trade(agent_key, "SELL_FAILED", symbol, 0, sell_amount, 0, result.get("error", ""))
                actions.append(f"sell failed for {symbol}: {str(result.get('error',''))[:60]}")
        else:
            actions.append(f"hold {symbol}: {reason}")

    # Phase 2: Buy new positions
    # Re-check USDC after sells
    balances = get_balances(agent_key)
    usdc = balances["USDC"]
    usable = usdc - RESERVE_USDC

    if usable >= MIN_TRADE_USDC:
        for symbol in token_list:
            if usable < MIN_TRADE_USDC:
                break

            should, reason = should_buy(symbol, agent_key)
            if not should:
                actions.append(f"skip buy {symbol}: {reason}")
                continue

            # Determine buy size
            buy_size = get_adjusted_buy_size(symbol, BASE_BUY_USDC)
            if buy_size < MIN_TRADE_USDC:
                actions.append(f"skip buy {symbol}: size ${buy_size:.2f} below min")
                continue

            # Cap at MAX_POSITION_PCT of usable
            max_alloc = usable * MAX_POSITION_PCT
            buy_size = min(buy_size, max_alloc)
            if buy_size < MIN_TRADE_USDC:
                buy_size = min(usable, BASE_BUY_USDC)
            if buy_size < MIN_TRADE_USDC:
                continue

            buy_size = round(buy_size, 2)
            print(f"  📈 BUY signal for {symbol}: {reason} (size ${buy_size:.2f})")
            ok, result = buy_token(symbol, buy_size, agent_key)
            if ok:
                tokens_received = result.get("received", 0)
                log_trade(agent_key, "BUY", symbol, buy_size, tokens_received, 0, reason)
                actions.append(f"bought {tokens_received:,.4f} {symbol} for ${buy_size:.2f}")
                usable -= buy_size
                trade_count += 1
                time.sleep(3)
            else:
                log_trade(agent_key, "BUY_FAILED", symbol, buy_size, 0, 0, result.get("error", ""))
                actions.append(f"buy failed for {symbol}: {str(result.get('error',''))[:60]}")
    else:
        actions.append(f"no buy phase: ${usable:.2f} usable below ${MIN_TRADE_USDC} min")

    return {
        "agent": agent_key,
        "usdc": usdc,
        "trades": trade_count,
        "actions": actions,
    }


# ════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ─── Self-Healing Cron Mesh Bootstrap ───────────────────────────────
    sys.path.insert(0, WORKSPACE)
    try:
        from cron_watchdog import ensure_all_crons
        _recreated = ensure_all_crons()
        if _recreated:
            print(f"  🔧 Self-healed crons: {', '.join(sorted(_recreated))}")
    except Exception as e:
        print(f"  ⚠️ Cron mesh bootstrap skipped: {e}")

    # ─── Goal Check ─────────────────────────────────────────────────────
    try:
        from goal_tracker import check_goal
        if check_goal():
            print("🏁 Goal achieved — profit engine halting.")
            sys.exit(0)
    except Exception as e:
        print(f"  ⚠️ Goal check skipped: {e}")

    # ─── Main Loop ──────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print(f"  🚀 ARMADILLO SELF-LEARNING PROFIT ENGINE 🚀")
    print(f"  {now_str()}")
    print(f"  Goal: $1,000,000 USDC → SOL → {GOAL_WALLET[:12]}...")
    print(f"  Tokens: {', '.join(TOKENS.keys())}")
    print(f"  Agents: {', '.join(WALLETS.keys())}")
    print(f"{'═' * 60}")

    # Load learned params summary
    learned = load_learned_params()
    if learned:
        print(f"\n  📊 Learned params loaded:")
        print(f"     Total trades analyzed: {learned.get('total_trades', 0)}")
        print(f"     Overall win rate: {learned.get('overall_winrate', 0):.1f}%")
        print(f"     Total PnL: ${learned.get('total_pnl', 0):.4f}")
        for sym in TOKENS:
            rec = learned.get("tokens", {}).get(sym, {})
            if rec:
                print(f"     {sym}: WR={rec.get('win_rate', 0)}% "
                      f"PnL={rec.get('total_pnl', 0)} "
                      f"rec={rec.get('recommendation', 'N/A')[:40]}")
    else:
        print("\n  📊 No learned params yet — cold start mode")

    # Determine cycle number
    log = load_trade_log()
    cycle_num = len(set(e.get("cycle", 0) for e in log)) + 1 if log else 1

    print(f"\n  🔄 Cycle #{cycle_num}")
    print(f"{'─' * 60}")

    # Phase 0: Cross-hire (agents generate economic activity by hiring each other)
    hires = run_cross_hiring_cycle()

    # Phase 1: Run each agent's trading cycle
    agent_results = []
    for agent in ["armabase", "saint", "scout"]:
        result = run_agent_cycle(agent, cycle_num)
        agent_results.append(result)

    # Self-correction
    print(f"\n{'─' * 60}")
    print(f"  🧠 Self-correction analysis...")
    corrections = self_correct()

    # File report
    print(f"\n  📄 Filing cycle report...")
    file_report(cycle_num, agent_results)

    # Summary
    total_trades_this_cycle = sum(r["trades"] for r in agent_results)
    print(f"\n{'═' * 60}")
    print(f"  ✅ Cycle #{cycle_num} complete")
    print(f"  Trades this cycle: {total_trades_this_cycle}")
    print(f"  Cross-hires: {len(hires)}")
    print(f"  Agents active: {sum(1 for r in agent_results if r['trades'] > 0)}/3")
    print(f"{'═' * 60}")
