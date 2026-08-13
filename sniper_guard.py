#!/usr/bin/env python3
"""
Sniper Guard — Adaptive Position Manager
=========================================
Manages every token the Solana sniper wallet holds.

PROFIT MANAGEMENT (scale out winners):
  Stage 1: +25%  → sell 40% of bag (lock initial gains)
  Stage 2: +60%  → sell 30% more (ride the rest with trailing stop)
  Stage 3: +100% → sell 20% more
  Trailing stop: once in profit, trail at 15% below peak price
  Final exit: trailing stop hit → sell remaining 10%

LOSS MANAGEMENT (cut losers early, not at -70%):
  Stage 1: -12%  → sell 50% (cut exposure in half, limit damage)
  Stage 2: -22%  → sell 50% of remaining (now holding 25% of original)
  Stage 3: -35%  → sell everything (stop out, accept the loss)
  Dead bag: -50%+ with no recovery in 6h → dump remainder

RE-ADJUSTMENT (counter losing positions):
  If token drops -8% then bounces back above entry:
    → mark as "recovered", reset loss stages, hold
  If token drops -15% and volume is still healthy:
    → sell 50% only, keep rest in case of recovery
  If token drops -15% and volume is dying:
    → sell 75%, keep 25% as moonshot lottery ticket

Runs every 10 minutes via cron.
"""
import json, base64, requests, time, os, sys
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from datetime import datetime, timezone

# ============ CONFIG ============
SOL_KEYPAIR_PATH = "/home/hermes/.config/solana/armabase-sol.json"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

MIN_SOL_RESERVE = 0.005
MAX_SLIPPAGE_BPS = 1500
MIN_SELL_VALUE_USDC = 0.001

# ─── Profit thresholds (scale out) ───
PROFIT_STAGE_1_PCT = 25     # +25% → sell 40%
PROFIT_STAGE_2_PCT = 60     # +60% → sell 30%
PROFIT_STAGE_3_PCT = 100    # +100% → sell 20%
PROFIT_SELL_FRACTION = {1: 0.40, 2: 0.30, 3: 0.20}
TRAILING_STOP_PCT = 15      # Trail 15% below peak once profitable

# ─── Loss thresholds (cut early) ───
LOSS_STAGE_1_PCT = 12       # -12% → sell 50%
LOSS_STAGE_2_PCT = 22       # -22% → sell 50% of remaining
LOSS_STAGE_3_PCT = 35       # -35% → sell everything
LOSS_SELL_FRACTION = {1: 0.50, 2: 0.50, 3: 1.0}

# ─── Dead bag cleanup ───
DEAD_BAG_LOSS_PCT = 50      # -50%+
DEAD_BAG_HOURS = 6          # ...with no recovery in 6h → dump remainder

# ─── Stale bag ───
STALE_HOURS = 48            # Held 48h with no profit → trim 50%

# State files
BAGS_FILE = "/workspace/sniper_bags.json"
TRADE_LOG = "/workspace/sol_trade_log.json"
SEEN_TOKENS_FILE = "/workspace/deployer_seen_tokens.json"

# ============ WALLET ============
with open(SOL_KEYPAIR_PATH) as f:
    secret = json.load(f)
KEYPAIR = Keypair.from_bytes(bytes(secret))
WALLET = str(KEYPAIR.pubkey())

# ============ FUNCTIONS ============

def rpc_call(method, params):
    try:
        resp = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }, timeout=15)
        return resp.json()
    except Exception as e:
        return {}

def get_sol_balance():
    r = rpc_call("getBalance", [WALLET])
    return r.get("result", {}).get("value", 0) / 1e9

def get_all_token_accounts():
    holdings = []
    for program_id in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                       "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
        r = rpc_call("getTokenAccountsByOwner", [
            WALLET, {"programId": program_id}, {"encoding": "jsonParsed"}
        ])
        for a in r.get("result", {}).get("value", []):
            info = a["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amt = float(info["tokenAmount"]["uiAmount"] or 0)
            dec = info["tokenAmount"]["decimals"]
            if amt > 0 and mint != USDC_MINT:
                holdings.append({"mint": mint, "amount": amt, "decimals": dec})
    return holdings

def get_token_usd_value(mint, amount, decimals):
    try:
        raw = int(amount * (10 ** decimals))
        q = requests.get(JUP_QUOTE, params={
            "inputMint": mint, "outputMint": USDC_MINT,
            "amount": str(raw), "slippageBps": str(MAX_SLIPPAGE_BPS),
        }, timeout=10)
        qd = q.json()
        if "outAmount" in qd:
            usd = int(qd["outAmount"]) / 1e6
            impact = float(qd.get("priceImpactPct", 0) or 0)
            return usd, impact, None
        return 0, 0, qd.get("error", "no quote")[:100] if "error" in qd else "no quote"
    except Exception as e:
        return 0, 0, str(e)[:100]

def get_token_price(mint, decimals):
    try:
        raw = int(1 * (10 ** decimals))
        q = requests.get(JUP_QUOTE, params={
            "inputMint": mint, "outputMint": USDC_MINT,
            "amount": str(raw), "slippageBps": str(MAX_SLIPPAGE_BPS),
        }, timeout=10)
        qd = q.json()
        if "outAmount" in qd:
            return int(qd["outAmount"]) / 1e6
    except Exception as e:
        pass
    return 0

def jup_swap(input_mint, output_mint, amount, slippage=MAX_SLIPPAGE_BPS):
    try:
        resp = requests.get(JUP_QUOTE, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(amount), "slippageBps": str(slippage),
        }, timeout=15)
        quote = resp.json()
        if "error" in quote:
            return False, "", f"Quote error: {quote['error'][:150]}"
        out_amount = int(quote["outAmount"])

        resp2 = requests.post(JUP_SWAP, json={
            "quoteResponse": quote, "userPublicKey": WALLET, "wrapUnwrapSOL": False,
        }, timeout=15)
        swap = resp2.json()
        if "error" in swap:
            return False, "", f"Swap build error: {swap['error'][:150]}"

        tx_b64 = swap["swapTransaction"]
        tx_bytes = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [KEYPAIR])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

        resp3 = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64",
                         "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=60)
        result = resp3.json()
        if "error" in result:
            return False, "", f"Send error: {result['error'].get('message', '')[:150]}"
        return True, result["result"], f"out={out_amount} USDC"
    except Exception as e:
        return False, "", f"Exception: {str(e)[:150]}"

def sell_partial(mint, amount, decimals, fraction, symbol="?"):
    """Sell a fraction of holdings. Returns (ok, sig, details, usd_out, tokens_sold)"""
    sell_amount = amount * fraction
    # Round to avoid dust issues
    raw_amount = int(sell_amount * (10 ** decimals))
    if raw_amount <= 0:
        return False, "", "amount too small", 0, 0
    ok, sig, details = jup_swap(mint, USDC_MINT, raw_amount)
    usd_out = 0
    if ok and "out=" in details:
        try:
            usd_out = int(details.split("out=")[1].split(" ")[0]) / 1e6
        except Exception as e:
            usd_out = 0
    return ok, sig, details, usd_out, sell_amount

# ─── Bag Tracking ─────────────────────────────────────────────────────

def load_bags():
    try:
        with open(BAGS_FILE) as f:
            return json.load(f)
    except Exception as e:
        return {"bags": {}, "sells": [], "total_realized_pnl": 0.0}

def save_bags(data):
    with open(BAGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_seen_tokens():
    try:
        with open(SEEN_TOKENS_FILE) as f:
            return json.load(f)
    except Exception as e:
        return {"tokens": {}}

def get_entry_info(mint):
    """Get entry price and symbol from seen_tokens"""
    seen = load_seen_tokens()
    info = seen.get("tokens", {}).get(mint, {})
    symbol = info.get("symbol", "?")
    # Entry was $0.10 USDC per snipe — but we need to track actual token amount received
    # to compute real entry price. For now use $0.10 as the cost basis.
    entry_cost_usdc = 0.10
    return entry_cost_usdc, symbol

def update_bag(bags_data, mint, current_price, current_value, symbol, current_amount):
    """Track or update a bag"""
    bags = bags_data.setdefault("bags", {})
    entry_cost, _ = get_entry_info(mint)

    if mint not in bags:
        bags[mint] = {
            "symbol": symbol,
            "entry_cost_usdc": entry_cost,
            "original_amount": current_amount,
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "price_history": [],
            "peak_price": current_price,
            "profit_stages_triggered": [],
            "loss_stages_triggered": [],
            "status": "holding",
        }

    bag = bags[mint]
    bag["symbol"] = symbol
    bag["current_price"] = current_price
    bag["current_value"] = current_value
    bag["current_amount"] = current_amount
    bag["last_checked"] = datetime.now(timezone.utc).isoformat()

    # Track peak price for trailing stop
    if current_price > bag.get("peak_price", 0):
        bag["peak_price"] = current_price

    # Price history (keep last 30 readings)
    history = bag.setdefault("price_history", [])
    history.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "price": current_price,
        "value": current_value,
        "amount": current_amount,
    })
    if len(history) > 30:
        bag["price_history"] = history[-30:]

    return bag

def compute_pnl_pct(bag, current_value):
    """Compute PnL percentage based on entry cost vs current value"""
    entry = bag.get("entry_cost_usdc", 0.10)
    if entry <= 0:
        return 0
    return ((current_value - entry) / entry) * 100

def evaluate_position(bag, current_price, current_value, current_amount):
    """
    Decide what to do with a position.
    Returns: (action, sell_fraction, reason, urgency)
    
    Actions: "hold", "sell_partial", "sell_all", "skip"
    """
    entry = bag.get("entry_cost_usdc", 0.10)
    original_amount = bag.get("original_amount", current_amount)
    status = bag.get("status", "holding")
    peak = bag.get("peak_price", current_price)
    history = bag.get("price_history", [])
    profit_stages = bag.get("profit_stages_triggered", [])
    loss_stages = bag.get("loss_stages_triggered", [])

    if status in ("sold", "dust", "illiquid"):
        return "skip", 0, f"status={status}", "none"

    if entry <= 0 or current_value <= 0:
        if current_value < MIN_SELL_VALUE_USDC:
            return "sell_all", 1.0, "dust (no value)", "low"
        return "hold", 0, "no price data", "none"

    pnl_pct = compute_pnl_pct(bag, current_value)

    # ─── PROFIT MANAGEMENT ───
    if pnl_pct > 0:
        # Stage 1: +25% → sell 40%
        if pnl_pct >= PROFIT_STAGE_1_PCT and 1 not in profit_stages:
            return "sell_partial", PROFIT_SELL_FRACTION[1], f"profit stage 1 (+{pnl_pct:.0f}%)", "high"

        # Stage 2: +60% → sell 30%
        if pnl_pct >= PROFIT_STAGE_2_PCT and 2 not in profit_stages:
            return "sell_partial", PROFIT_SELL_FRACTION[2], f"profit stage 2 (+{pnl_pct:.0f}%)", "high"

        # Stage 3: +100% → sell 20%
        if pnl_pct >= PROFIT_STAGE_3_PCT and 3 not in profit_stages:
            return "sell_partial", PROFIT_SELL_FRACTION[3], f"profit stage 3 (+{pnl_pct:.0f}%)", "high"

        # Trailing stop: once peaked, if dropped 15% from peak → sell remaining
        if peak > current_price and peak > 0:
            drop_from_peak = ((peak - current_price) / peak) * 100
            if drop_from_peak >= TRAILING_STOP_PCT and len(profit_stages) > 0:
                return "sell_all", 1.0, f"trailing stop (-{drop_from_peak:.0f}% from peak)", "high"

        return "hold", 0, f"in profit (+{pnl_pct:.0f}%, peak +{((peak-entry)/entry*100) if entry>0 else 0:.0f}%)", "none"

    # ─── LOSS MANAGEMENT ───
    # PnL is negative
    pnl_pct = abs(pnl_pct)  # work with positive numbers for loss

    # Check for recovery: if we triggered loss stage 1 but price came back above -8%
    if 1 in loss_stages and pnl_pct < 8:
        # Reset loss stages — token is recovering
        bag["loss_stages_triggered"] = []
        return "hold", 0, f"recovering (-{pnl_pct:.0f}%, reset loss stages)", "none"

    # Stage 1: -12% → sell 50% (cut exposure in half)
    if pnl_pct >= LOSS_STAGE_1_PCT and 1 not in loss_stages:
        # Check if volume/liquidity still healthy (value > 50% of entry)
        if current_value > entry * 0.5:
            return "sell_partial", LOSS_SELL_FRACTION[1], f"loss stage 1 (-{pnl_pct:.0f}%, cutting 50%)", "high"
        else:
            # Value already collapsed — sell 75% to salvage what we can
            return "sell_partial", 0.75, f"loss stage 1 (-{pnl_pct:.0f}%, low value, cutting 75%)", "critical"

    # Stage 2: -22% → sell 50% of remaining
    if pnl_pct >= LOSS_STAGE_2_PCT and 2 not in loss_stages:
        return "sell_partial", LOSS_SELL_FRACTION[2], f"loss stage 2 (-{pnl_pct:.0f}%, cutting half of remaining)", "high"

    # Stage 3: -35% → sell everything (stop out)
    if pnl_pct >= LOSS_STAGE_3_PCT and 3 not in loss_stages:
        return "sell_all", 1.0, f"stop out (-{pnl_pct:.0f}%)", "critical"

    # Dead bag: -50%+ with no recovery in 6h
    if pnl_pct >= DEAD_BAG_LOSS_PCT:
        try:
            first_dt = datetime.fromisoformat(bag.get("first_seen", "").replace("Z", "+00:00"))
            hours_held = (datetime.now(timezone.utc) - first_dt).total_seconds() / 3600
        except Exception as e:
            hours_held = 0
        if hours_held >= DEAD_BAG_HOURS:
            return "sell_all", 1.0, f"dead bag (-{pnl_pct:.0f}%, {hours_held:.0f}h held)", "critical"

    # Stale bag: 48h with no profit
    try:
        first_dt = datetime.fromisoformat(bag.get("first_seen", "").replace("Z", "+00:00"))
        hours_held = (datetime.now(timezone.utc) - first_dt).total_seconds() / 3600
    except Exception as e:
        hours_held = 0
    if hours_held >= STALE_HOURS and pnl_pct > 5:
        return "sell_partial", 0.50, f"stale ({hours_held:.0f}h, -{pnl_pct:.0f}%)", "medium"

    return "hold", 0, f"holding (-{pnl_pct:.0f}%, ${current_value:.4f})", "none"

def log_sell(bags_data, mint, symbol, tokens_sold, usd_received, entry_cost, reason, partial=False):
    """Log a completed sell. PnL = usd_received - proportional_cost_of_tokens_sold."""
    bag = bags_data.get("bags", {}).get(mint, {})
    original = bag.get("original_amount", 0)
    current_amount = bag.get("current_amount", 0)
    
    # Proportional entry cost: if we sold X% of our current holding,
    # the entry cost attributed to that portion is entry_cost * (tokens_sold / original)
    if original > 0:
        proportional_entry = entry_cost * (tokens_sold / original)
    else:
        proportional_entry = 0
    
    pnl = usd_received - proportional_entry
    pnl_pct = (pnl / proportional_entry * 100) if proportional_entry > 0 else 0

    sell_entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "action": "SELL_PARTIAL" if partial else "SELL",
        "symbol": symbol,
        "mint": mint,
        "token_in": tokens_sold,
        "usd_out": usd_received,
        "entry_cost": entry_cost,
        "pnl": pnl,
        "pnl_pct": pnl_pct / 100,
        "reason": reason,
    }

    bags_data.setdefault("sells", []).append(sell_entry)
    bags_data["total_realized_pnl"] = bags_data.get("total_realized_pnl", 0.0) + pnl

    # Also log to sol_trade_log.json
    try:
        log = json.load(open(TRADE_LOG))
        log.setdefault("trades", []).append(sell_entry)
        with open(TRADE_LOG, "w") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        pass

    return sell_entry

def mark_stage(bag, stage_type, stage_num):
    """Mark a profit/loss stage as triggered"""
    key = f"{stage_type}_stages_triggered"
    stages = bag.setdefault(key, [])
    if stage_num not in stages:
        stages.append(stage_num)

# ============ MAIN ============

def run():
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"\n🛡️  Sniper Guard — Adaptive Position Manager — {now_str}")
    print(f"   Wallet: {WALLET[:12]}...")

    sol_bal = get_sol_balance()
    print(f"   SOL: {sol_bal:.6f}")

    if sol_bal < MIN_SOL_RESERVE:
        print(f"   ⚠️ SOL below reserve — can't execute sells")
        print(f"   ✅ Guard cycle complete (low gas)")
        return

    holdings = get_all_token_accounts()
    print(f"   Holdings: {len(holdings)} tokens")

    if not holdings:
        print(f"   No tokens to guard")
        print(f"   ✅ Guard cycle complete")
        return

    bags_data = load_bags()
    bags_data["last_run"] = now_str

    seen = load_seen_tokens()
    mint_to_symbol = {}
    for mint, info in seen.get("tokens", {}).items():
        mint_to_symbol[mint] = info.get("symbol", "?")

    sells_executed = 0
    total_usd_out = 0.0

    for h in holdings:
        mint = h["mint"]
        amount = h["amount"]
        decimals = h["decimals"]
        symbol = mint_to_symbol.get(mint, mint[:8])

        usd_value, price_impact, error = get_token_usd_value(mint, amount, decimals)
        current_price = get_token_price(mint, decimals)

        if error and usd_value <= 0:
            print(f"\n   📦 {symbol} ({mint[:15]}...)")
            print(f"      Amount: {amount:,.2f}")
            print(f"      Value: unknown ({error})")
            update_bag(bags_data, mint, 0, 0, symbol, amount)
            continue

        bag = update_bag(bags_data, mint, current_price, usd_value, symbol, amount)

        action, sell_fraction, reason, urgency = evaluate_position(bag, current_price, usd_value, amount)

        pnl = compute_pnl_pct(bag, usd_value)
        pnl_str = f"{pnl:+.0f}%"

        print(f"\n   📦 {symbol} ({mint[:15]}...)")
        print(f"      Amount: {amount:,.2f}  Value: ${usd_value:.4f}  PnL: {pnl_str}")
        print(f"      Action: {'🔫' if 'sell' in action else '✅'} {action} — {reason} [{urgency}]")

        if action == "skip" or action == "hold":
            continue

        if usd_value < MIN_SELL_VALUE_USDC:
            print(f"      ⏭️ Skipping — value ${usd_value:.4f} below threshold")
            bag["status"] = "dust"
            continue

        # Execute the sell
        is_partial = action == "sell_partial"
        print(f"      🔫 Selling {sell_fraction*100:.0f}% of {amount:,.2f} {symbol}...")

        ok, sig, details, usd_out, tokens_sold = sell_partial(
            mint, amount, decimals, sell_fraction, symbol
        )

        if ok:
            entry_cost = bag.get("entry_cost_usdc", 0.10)
            sell_log = log_sell(bags_data, mint, symbol, tokens_sold, usd_out,
                               entry_cost, reason, partial=is_partial)

            print(f"      ✅ SOLD: {tokens_sold:,.2f} {symbol} → ${usd_out:.4f} USDC")
            print(f"      💰 PnL: ${sell_log['pnl']:+.4f} ({sell_log['pnl_pct']*100:+.1f}%)")
            sells_executed += 1
            total_usd_out += usd_out

            # Mark the stage as triggered
            if "profit stage" in reason:
                stage_num = int(reason.split("stage ")[1].split(" ")[0])
                mark_stage(bag, "profit", stage_num)
            elif "loss stage" in reason:
                stage_num = int(reason.split("stage ")[1].split(" ")[0])
                mark_stage(bag, "loss", stage_num)
            elif "stop out" in reason:
                mark_stage(bag, "loss", 3)
                bag["status"] = "sold"
            elif "trailing stop" in reason:
                bag["status"] = "sold"
            elif "dead bag" in reason:
                bag["status"] = "sold"
            elif "stale" in reason:
                mark_stage(bag, "loss", 1)  # Count as a loss trim

            bag["last_sell_time"] = datetime.now(timezone.utc).isoformat()
        else:
            print(f"      ❌ Sell failed: {details}")
            if "liquidity" in details.lower() or "route" in details.lower():
                bag["status"] = "illiquid"

    save_bags(bags_data)

    print(f"\n{'─' * 60}")
    print(f"   Sells: {sells_executed}  |  USDC out: ${total_usd_out:.4f}")
    print(f"   Total realized PnL: ${bags_data.get('total_realized_pnl', 0):.4f}")
    print(f"   ✅ Guard cycle complete")


# ─── Self-Healing Cron Mesh ──────────────────────────────────────────────
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
except Exception:
    pass
# ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
