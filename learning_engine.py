#!/usr/bin/env python3
"""
Learning Engine — Self-Learning Auto-Correcting Trading Intelligence
=========================================================================
Reads ALL trade logs, calculates per-token PnL, tracks win/loss streaks,
auto-corrects strategy parameters, outputs learned parameters, collects
profits → SOL, sends excess to user's personal wallet.

Log sources analyzed:
  1. /workspace/sol_trade_log.json     — main Solana trade log (Jupiter swaps)
  2. /workspace/profit_engine_log.json — profit engine cycle log
  3. /workspace/sniper_bags.json       — sniper bags + sells

Auto-correction rules:
  - 3+ consecutive losses → position size ×0.5, tighter stop loss
  - 3+ consecutive wins   → position size ×1.25, wider take profit
  - win rate <15% after 5+ trades → flag dead, remove from loop
  - optimal hold time + buy size calculated per token from winning trades

Outputs:
  - /workspace/learned_params.json
  - /workspace/reports/learning_report_{timestamp}.json
  - Updates /workspace/pumpfun_loop_tokens.json (adjust min_trade_usdc, remove dead)
  - Sends excess SOL (>0.3 reserve) to user wallet
"""

import json, os, re, sys, time, base64, requests
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ============ CONFIG ============
TRADE_LOG = "/workspace/sol_trade_log.json"
PROFIT_ENGINE_LOG = "/workspace/profit_engine_log.json"
SNIPER_BAGS = "/workspace/sniper_bags.json"
SEEN_TOKENS = "/workspace/deployer_seen_tokens.json"
LOOP_TOKENS = "/workspace/pumpfun_loop_tokens.json"
LEARNED_PARAMS = "/workspace/learned_params.json"
PROFIT_LOG = "/workspace/profit_log.json"
TEAM_STATE = "/workspace/team_state.json"
REPORTS_DIR = "/workspace/reports"

# Solana
SOL_KEYPAIR_PATH = os.path.expanduser("~/.config/solana/armabase-sol.json")
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# User's personal wallet to collect profits
USER_SOL_WALLET = "EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd"

# Learning thresholds
MIN_TRADES_FOR_LEARNING = 3
MIN_TRADES_FOR_DEAD = 5
DEAD_WIN_RATE_PCT = 15.0
LOSS_STREAK_THRESHOLD = 3
WIN_STREAK_THRESHOLD = 3
MAX_PRICE_IMPACT_PCT = 15.0  # Skip trades above this impact
MIN_PROFIT_USDC = 0.01  # Minimum profit to bother collecting

# Base position sizing
BASE_BUY_USDC = 0.10
BASE_STOP_LOSS_PCT = -25.0
BASE_TAKE_PROFIT_PCT = 50.0
TIGHT_STOP_LOSS_PCT = -12.0
WIDE_TAKE_PROFIT_PCT = 100.0

# OGSAINT on Base chain — added to tracked tokens for cross-chain awareness
OGSAINT_BASE_CONTRACT = "0xfde1f1255683772d48b12b082fd3140713d6e40d"
OGSAINT_SYMBOL = "OGSAINT"

# ============ WALLET ============
try:
    with open(SOL_KEYPAIR_PATH) as f:
        secret = json.load(f)
    KEYPAIR = Keypair.from_bytes(bytes(secret))
    WALLET = str(KEYPAIR.pubkey())
except Exception as _e:
    KEYPAIR = None
    WALLET = ""
    print(f"  ⚠️ Could not load Solana keypair: {_e}")

# ============ LOG LOADING ============

def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def load_sol_trades():
    """Load trades from sol_trade_log.json"""
    d = _load_json(TRADE_LOG, {"trades": []})
    if isinstance(d, dict):
        return d.get("trades", [])
    if isinstance(d, list):
        return d
    return []

def load_profit_engine_log():
    """Load profit engine log entries (list of trades or cycles)"""
    d = _load_json(PROFIT_ENGINE_LOG, [])
    if isinstance(d, dict):
        # could be {trades: [...]} or {history: [...]} or {cycles: [...]}
        for key in ("trades", "history", "cycles", "events"):
            if key in d and isinstance(d[key], list):
                return d[key]
        return [d]
    if isinstance(d, list):
        return d
    return []

def load_sniper_bags():
    """Load sniper_bags.json — returns list of sell events + bag info"""
    d = _load_json(SNIPER_BAGS, {})
    results = []
    if not isinstance(d, dict):
        return results

    # Sell events (primary signal source)
    for s in d.get("sells", []):
        results.append(s)

    # Bag entries — include realized PnL for sold bags
    bags = d.get("bags", {})
    if isinstance(bags, dict):
        for mint, info in bags.items():
            entry = dict(info) if isinstance(info, dict) else {}
            entry.setdefault("mint", mint)
            # Sold bags carry realized_pnl as a completed trade
            if entry.get("status") == "sold" and "realized_pnl" in entry:
                results.append({
                    "time": entry.get("sell_time", entry.get("first_seen", "")),
                    "action": "SELL",
                    "symbol": entry.get("symbol", mint[:8]),
                    "mint": mint,
                    "pnl": entry.get("realized_pnl", 0),
                    "entry_cost": entry.get("entry_price", 0.1),
                    "usd_out": entry.get("current_value", 0),
                    "source": "sniper_bags",
                    "reason": entry.get("sell_reason", ""),
                })
    return results

# ============ TRADE PARSING ============

def _parse_time(t):
    if not t or not isinstance(t, str):
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        # Normalize: always return timezone-aware (UTC) so subtraction works
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

def _extract_out_amount(details):
    if not details:
        return 0
    m = re.search(r"out=(\d+)", str(details))
    return int(m.group(1)) if m else 0

def _extract_impact(details):
    if not details:
        return 0.0
    m = re.search(r"impact=([\d.]+)%", str(details))
    return float(m.group(1)) if m else 0.0

def _normalize_trade_entry(t):
    """Normalize a raw trade entry from any log into a common shape."""
    token = t.get("token") or t.get("symbol") or t.get("mint", "")[:8] or "?"
    action_raw = (t.get("action") or "?").upper()
    tm = t.get("time") or t.get("timestamp") or ""
    details = t.get("details") or ""
    source = t.get("source", "sol_trade_log")

    # Determine action
    action = action_raw
    if "SELL" in action_raw or "SELL_PARTIAL" in action_raw:
        action = "SELL"
    elif "BUY" in action_raw:
        action = "BUY"

    return {
        "token": token,
        "symbol": token,
        "action": action,
        "action_raw": action_raw,
        "time": tm,
        "details": details,
        "source": source,
        "mint": t.get("mint", ""),
        "impact": _extract_impact(details) if details else t.get("impact", 0.0),
        # Numeric amounts (may be present or absent)
        "amount_in": t.get("amount_in", t.get("sol_in", t.get("entry_cost", 0))),
        "amount_out": t.get("amount_out", 0),
        "sol_in": t.get("sol_in", 0),
        "sol_out": t.get("sol_out", 0),
        "usd_out": t.get("usd_out", 0),
        "entry_cost": t.get("entry_cost", 0),
        "pnl": t.get("pnl"),
        "pnl_pct": t.get("pnl_pct"),
        "reason": t.get("reason", ""),
    }

def parse_all_trades():
    """Merge trades from all 3 logs, normalize, sort chronologically."""
    raw = []
    for t in load_sol_trades():
        raw.append(_normalize_trade_entry({**t, "source": "sol_trade_log"}))
    for t in load_profit_engine_log():
        if isinstance(t, dict):
            raw.append(_normalize_trade_entry({**t, "source": "profit_engine_log"}))
    for t in load_sniper_bags():
        if isinstance(t, dict):
            raw.append(_normalize_trade_entry({**t, "source": "sniper_bags"}))

    # Sort by time (None → oldest); normalize all to offset-naive UTC for comparison
    def sort_key(t):
        dt = _parse_time(t["time"])
        if dt is None:
            return datetime.min
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)  # drop tzinfo for uniform comparison
        return dt
    raw.sort(key=sort_key)
    return raw

# ============ PAIR MATCHING ============

def pair_trades(trades):
    """
    Match BUY → SELL pairs per token (FIFO) to compute PnL + hold time.
    Also handles pre-computed PnL entries (sniper_bags sells with pnl field).
    Returns list of result dicts.
    """
    positions = defaultdict(deque)  # token -> deque of buys
    results = []

    for t in trades:
        token = t["token"]
        action = t["action"]
        tm = t["time"]

        # Pre-computed PnL (e.g. sniper_bags sell with pnl field)
        if action == "SELL" and t.get("pnl") is not None:
            pnl = float(t["pnl"])
            cost = float(t.get("entry_cost") or 0)
            # hold time: try to compute from first_seen if available
            hold_seconds = 0
            sell_dt = _parse_time(tm)
            # If there's a matching buy in positions, use it
            if positions[token]:
                buy = positions[token].popleft()
                buy_dt = _parse_time(buy["time"])
                if buy_dt and sell_dt:
                    hold_seconds = (sell_dt - buy_dt).total_seconds()
                cost = float(buy.get("amount_in") or buy.get("sol_in") or cost)
            pnl_pct = (pnl / cost * 100) if cost and cost > 0 else 0
            results.append({
                "token": token,
                "mint": t.get("mint", ""),
                "buy_cost": cost,
                "sell_usdc": float(t.get("usd_out") or 0),
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "hold_seconds": max(0, hold_seconds),
                "buy_time": "",  # may be unknown
                "sell_time": tm,
                "source": t.get("source", ""),
                "impact": t.get("impact", 0.0),
            })
            continue

        if action == "BUY":
            cost = float(t.get("amount_in") or t.get("sol_in") or 0)
            # Convert SOL cost to USDC approx using 200 USD/SOL if sol_in present
            if cost == 0 and t.get("sol_in"):
                cost = float(t["sol_in"]) * 200.0
            out_amount = _extract_out_amount(t.get("details"))
            positions[token].append({
                "cost": cost,
                "time": tm,
                "tokens": out_amount,
                "mint": t.get("mint", ""),
                "source": t.get("source", ""),
            })

        elif action == "SELL":
            out_amount = _extract_out_amount(t.get("details"))
            # USD/SOL received
            sold_value = 0.0
            if t.get("usd_out"):
                sold_value = float(t["usd_out"])
            elif t.get("sol_out"):
                sold_value = float(t["sol_out"]) * 200.0
            elif out_amount:
                # details out= is in USDC lamports (6 decimals) for these logs
                sold_value = out_amount / 1e6

            if positions[token]:
                buy = positions[token].popleft()
                cost = float(buy.get("cost") or 0)
                pnl = sold_value - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0

                hold_seconds = 0
                buy_dt = _parse_time(buy["time"])
                sell_dt = _parse_time(tm)
                if buy_dt and sell_dt:
                    hold_seconds = (sell_dt - buy_dt).total_seconds()

                results.append({
                    "token": token,
                    "mint": buy.get("mint", t.get("mint", "")),
                    "buy_cost": cost,
                    "sell_usdc": sold_value,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "hold_seconds": max(0, hold_seconds),
                    "buy_time": buy["time"],
                    "sell_time": tm,
                    "source": t.get("source", ""),
                    "impact": t.get("impact", 0.0),
                })

    return results

# ============ ANALYSIS ============

def compute_streak(pnl_history):
    """Compute current win/loss streak: +N for wins, -N for losses."""
    if not pnl_history:
        return 0
    streak = 0
    last_win = pnl_history[-1] > 0
    for p in reversed(pnl_history):
        is_win = p > 0
        if is_win == last_win:
            streak += 1 if last_win else -1
        else:
            break
    return streak

def analyze_trades():
    """Analyze all historical trades and generate learned parameters + auto-corrections."""
    all_trades = parse_all_trades()
    if len(all_trades) < MIN_TRADES_FOR_LEARNING:
        print(f"  Not enough trades for learning ({len(all_trades)} < {MIN_TRADES_FOR_LEARNING})")
        return None

    results = pair_trades(all_trades)
    if not results:
        print("  No paired trades (BUY→SELL) found")
        return None

    # Per-token aggregation
    token_stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "volume": 0.0,
        "hold_times": [], "pnl_history": [], "buy_sizes": [],
        "best_pnl": 0.0, "worst_pnl": 0.0, "mints": set(),
        "winning_hold_times": [], "winning_buy_sizes": [],
    })

    for r in results:
        t = r["token"]
        s = token_stats[t]
        s["trades"] += 1
        s["pnl"] += r["pnl"]
        s["volume"] += r["buy_cost"]
        s["pnl_history"].append(r["pnl"])
        s["buy_sizes"].append(r["buy_cost"])
        s["hold_times"].append(r["hold_seconds"])
        if r.get("mint"):
            s["mints"].add(r["mint"])
        if r["pnl"] > 0:
            s["wins"] += 1
            s["winning_hold_times"].append(r["hold_seconds"])
            s["winning_buy_sizes"].append(r["buy_cost"])
            if r["pnl"] > s["best_pnl"]:
                s["best_pnl"] = r["pnl"]
        else:
            s["losses"] += 1
            if r["pnl"] < s["worst_pnl"]:
                s["worst_pnl"] = r["pnl"]

    # Build learned parameters + auto-corrections
    learned = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_trades": len(all_trades),
        "paired_trades": len(results),
        "total_pnl": round(sum(r["pnl"] for r in results), 6),
        "win_rate": round(len([r for r in results if r["pnl"] > 0]) / len(results) * 100, 2) if results else 0,
        "tokens": {},
        "global_lessons": [],
        "auto_corrections": [],
    }

    # Per-token learned params with auto-correction
    for sym, s in token_stats.items():
        total = s["wins"] + s["losses"]
        wr = (s["wins"] / total * 100) if total > 0 else 0
        avg_hold = (sum(s["hold_times"]) / len(s["hold_times"])) if s["hold_times"] else 0
        avg_buy = (sum(s["buy_sizes"]) / len(s["buy_sizes"])) if s["buy_sizes"] else 0

        # Optimal buy size + hold time from winning trades
        if s["winning_buy_sizes"]:
            optimal_buy = sum(s["winning_buy_sizes"]) / len(s["winning_buy_sizes"])
            optimal_hold = sum(s["winning_hold_times"]) / len(s["winning_hold_times"])
        else:
            # Never won → halve the default buy, keep avg hold
            optimal_buy = max(avg_buy * 0.5, 0.02) if avg_buy > 0 else BASE_BUY_USDC * 0.5
            optimal_hold = avg_hold

        # Streak detection
        current_streak = compute_streak(s["pnl_history"])
        recent = s["pnl_history"][-3:]
        recent_pnl = sum(recent)
        trending_up = len(recent) >= 2 and recent[-1] > recent[0]

        # Auto-correction: position size multiplier + SL/TP
        position_size_multiplier = 1.0
        stop_loss_pct = BASE_STOP_LOSS_PCT
        take_profit_pct = BASE_TAKE_PROFIT_PCT
        corrections = []

        if current_streak <= -LOSS_STREAK_THRESHOLD:
            # 3+ consecutive losses → halve position, tighten stop
            position_size_multiplier = 0.5
            stop_loss_pct = TIGHT_STOP_LOSS_PCT
            corrections.append(
                f"{sym}: {abs(current_streak)} consecutive losses → position ×0.5, stop loss tightened to {stop_loss_pct}%"
            )
        elif current_streak >= WIN_STREAK_THRESHOLD:
            # 3+ consecutive wins → +25% position, widen TP
            position_size_multiplier = 1.25
            take_profit_pct = WIDE_TAKE_PROFIT_PCT
            corrections.append(
                f"{sym}: {current_streak} consecutive wins → position ×1.25, take profit widened to +{take_profit_pct}%"
            )
        elif wr >= 50 and s["pnl"] > 0:
            # Profitable but no active streak — slight boost
            position_size_multiplier = 1.1
        elif wr < 30 and total >= 3:
            # Underperforming — slight cut
            position_size_multiplier = 0.75
            corrections.append(f"{sym}: low win rate {wr:.0f}% → position ×0.75")

        # Dead token detection
        is_dead = (total >= MIN_TRADES_FOR_DEAD) and (wr < DEAD_WIN_RATE_PCT)
        is_profitable = (s["pnl"] > 0) and (wr >= 40)

        # Recommendation
        if is_dead:
            recommendation = "STOP"
            corrections.append(f"{sym}: dead token (WR {wr:.1f}% over {total} trades) → flagged for removal")
        elif position_size_multiplier >= 1.25:
            recommendation = "INCREASE"
        elif position_size_multiplier <= 0.5:
            recommendation = "REDUCE"
        elif is_profitable and trending_up:
            recommendation = "INCREASE"
        elif wr < 30:
            recommendation = "REDUCE"
        else:
            recommendation = "MAINTAIN"

        learned["tokens"][sym] = {
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": round(wr, 2),
            "total_pnl": round(s["pnl"], 6),
            "optimal_buy_usdc": round(optimal_buy, 6),
            "optimal_hold_seconds": int(optimal_hold),
            "current_streak": current_streak,
            "position_size_multiplier": round(position_size_multiplier, 3),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "is_dead": is_dead,
            "is_profitable": is_profitable,
            "recommendation": recommendation,
            "trending_up": trending_up,
            "recent_pnl": round(recent_pnl, 6),
            "best_pnl": round(s["best_pnl"], 6),
            "worst_pnl": round(s["worst_pnl"], 6),
            "avg_hold_seconds": int(avg_hold),
            "volume": round(s["volume"], 4),
            "mints": list(s["mints"])[:3],
        }

        # Collect corrections for this token
        for c in corrections:
            learned["auto_corrections"].append({
                "token": sym,
                "correction": c,
                "applied_at": learned["generated_at"],
            })

    # ============ GLOBAL LESSONS ============
    total_pnl = learned["total_pnl"]
    if total_pnl < 0:
        learned["global_lessons"].append("Overall losing — reduce position sizes on unprofitable tokens")
    if learned["win_rate"] < 30:
        learned["global_lessons"].append("Low win rate — be more selective, skip high price impact trades")
    if learned["win_rate"] >= 60 and total_pnl > 0:
        learned["global_lessons"].append("Strong performance — increase allocation to top performers")

    # Best/worst performers
    profitable_tokens = [(sym, s) for sym, s in token_stats.items() if s["pnl"] > 0]
    if profitable_tokens:
        best = max(profitable_tokens, key=lambda x: x[1]["pnl"])
        learned["global_lessons"].append(f"Best performer: {best[0]} (+${best[1]['pnl']:.4f}) — increase allocation")

    dead = [sym for sym, info in learned["tokens"].items() if info["is_dead"]]
    if dead:
        learned["global_lessons"].append(f"Stop trading: {', '.join(dead)} — consistently losing (dead tokens)")

    # High price impact trades are losing
    high_impact_losses = [r for r in results if r.get("impact", 0) > 10 and r["pnl"] < 0]
    if len(high_impact_losses) >= 2:
        learned["global_lessons"].append(
            f"High price impact (>10%) trades losing {len(high_impact_losses)}x — skip trades above {MAX_PRICE_IMPACT_PCT}% impact"
        )

    return learned

# ============ PERSISTENCE ============

def save_learned_params(learned):
    with open(LEARNED_PARAMS, "w") as f:
        json.dump(learned, f, indent=2)
    print(f"  Learned params saved to {LEARNED_PARAMS}")

def load_learned_params():
    return _load_json(LEARNED_PARAMS, None)

def ensure_reports_dir():
    os.makedirs(REPORTS_DIR, exist_ok=True)

def generate_status_report(learned):
    """Write a status report file at /workspace/reports/learning_report_{timestamp}.json"""
    ensure_reports_dir()
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REPORTS_DIR, f"learning_report_{ts}.json")

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "report_file": path,
        "summary": {
            "total_trades": learned.get("total_trades", 0),
            "paired_trades": learned.get("paired_trades", 0),
            "total_pnl": learned.get("total_pnl", 0),
            "win_rate": learned.get("win_rate", 0),
            "token_count": len(learned.get("tokens", {})),
            "dead_tokens": [s for s, i in learned.get("tokens", {}).items() if i.get("is_dead")],
            "profitable_tokens": [s for s, i in learned.get("tokens", {}).items() if i.get("is_profitable")],
            "auto_corrections_count": len(learned.get("auto_corrections", [])),
        },
        "global_lessons": learned.get("global_lessons", []),
        "auto_corrections": learned.get("auto_corrections", []),
        "token_details": learned.get("tokens", {}),
    }

    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Status report saved to {path}")
    return path

# ============ STRATEGY ADJUSTMENT ============

def ensure_ogsaint_token():
    """Add OGSAINT (Base chain) to tracked loop tokens for awareness."""
    try:
        tokens = _load_json(LOOP_TOKENS, {})
        if not isinstance(tokens, dict):
            tokens = {}
    except Exception:
        tokens = {}

    if OGSAINT_SYMBOL not in tokens:
        tokens[OGSAINT_SYMBOL] = {
            "mint": OGSAINT_BASE_CONTRACT,
            "decimals": 18,
            "min_trade_usdc": 0.05,
            "chain": "base",
            "note": "OGSAINT on Base — cross-chain tracking only (not Solana-swappable)",
        }
        with open(LOOP_TOKENS, "w") as f:
            json.dump(tokens, f, indent=2)
        print(f"  ➕ Added {OGSAINT_SYMBOL} ({OGSAINT_BASE_CONTRACT[:10]}...) to tracked tokens")
    return tokens

def apply_learnings_to_loop():
    """Update pumpfun_loop_tokens.json based on learned parameters + auto-correction."""
    learned = load_learned_params()
    if not learned:
        print("  No learned params to apply")
        return []

    tokens = _load_json(LOOP_TOKENS, {})
    if not isinstance(tokens, dict):
        tokens = {}

    changes = []
    for sym, info in learned.get("tokens", {}).items():
        rec = info.get("recommendation", "MAINTAIN")
        mult = info.get("position_size_multiplier", 1.0)

        if sym in tokens:
            # Skip OGSAINT — cross-chain, not adjusted by Solana learnings
            if tokens[sym].get("chain") == "base":
                continue

            if rec == "STOP" or info.get("is_dead"):
                del tokens[sym]
                changes.append(f"  ❌ Removed {sym} (dead token, WR={info['win_rate']}%)")
            elif rec == "INCREASE":
                old_min = tokens[sym].get("min_trade_usdc", 0.05)
                new_min = min(old_min * mult, 0.50)
                tokens[sym]["min_trade_usdc"] = round(new_min, 4)
                tokens[sym]["stop_loss_pct"] = info.get("stop_loss_pct", BASE_STOP_LOSS_PCT)
                tokens[sym]["take_profit_pct"] = info.get("take_profit_pct", BASE_TAKE_PROFIT_PCT)
                changes.append(
                    f"  ⬆️ {sym}: min_trade ${old_min:.4f}→${new_min:.4f} (×{mult}, WR={info['win_rate']}%)"
                )
            elif rec == "REDUCE":
                old_min = tokens[sym].get("min_trade_usdc", 0.05)
                new_min = max(old_min * mult, 0.02)
                tokens[sym]["min_trade_usdc"] = round(new_min, 4)
                tokens[sym]["stop_loss_pct"] = info.get("stop_loss_pct", TIGHT_STOP_LOSS_PCT)
                changes.append(
                    f"  ⬇️ {sym}: min_trade ${old_min:.4f}→${new_min:.4f} (×{mult}, WR={info['win_rate']}%)"
                )
            else:  # MAINTAIN
                tokens[sym]["stop_loss_pct"] = info.get("stop_loss_pct", BASE_STOP_LOSS_PCT)
                tokens[sym]["take_profit_pct"] = info.get("take_profit_pct", BASE_TAKE_PROFIT_PCT)

    with open(LOOP_TOKENS, "w") as f:
        json.dump(tokens, f, indent=2)

    if changes:
        print("  Applied learnings to loop tokens:")
        for c in changes:
            print(c)
    else:
        print("  No changes needed to loop tokens")
    return changes

# ============ PROFIT COLLECTION ============

def get_sol_balance():
    if not WALLET:
        return 0.0
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET]
    }, timeout=15)
    return resp.json().get("result", {}).get("value", 0) / 1e9

def get_usdc_balance():
    if not WALLET:
        return 0.0
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accts = resp.json().get("result", {}).get("value", [])
    if accts:
        return float(accts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def get_token_balance(mint):
    if not WALLET or not mint:
        return 0.0
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accts = resp.json().get("result", {}).get("value", [])
    if accts:
        return float(accts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def jup_swap(input_mint, output_mint, amount, slippage=500):
    """Execute Jupiter swap. Returns (ok, signature, details)."""
    try:
        resp = requests.get(JUP_QUOTE, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(amount), "slippageBps": str(slippage),
        }, timeout=15)
        quote = resp.json()
        if "error" in quote:
            return False, "", f"Quote error: {quote['error']}"

        resp2 = requests.post(JUP_SWAP, json={
            "quoteResponse": quote, "userPublicKey": WALLET, "wrapUnwrapSOL": True,
        }, timeout=15)
        swap = resp2.json()
        if "error" in swap:
            return False, "", f"Swap error: {swap['error']}"

        tx = VersionedTransaction.from_bytes(base64.b64decode(swap["swapTransaction"]))
        signed = VersionedTransaction(tx.message, [KEYPAIR])
        signed_b64 = base64.b64encode(bytes(signed)).decode()

        resp3 = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=60)
        result = resp3.json()
        if "error" in result:
            return False, "", f"Send error: {result['error'].get('message', '')[:200]}"

        out_amount = int(quote.get("outAmount", 0))
        return True, result["result"], f"out={out_amount}"
    except Exception as e:
        return False, "", f"Exception: {str(e)[:200]}"

def collect_profits_to_sol(user_wallet=""):
    """
    Collect all USDC + token profits, convert to SOL.
    If user_wallet is provided, send excess SOL (>0.3 reserve) to user.
    Keeps a reserve for trading gas + sniping.
    """
    if not KEYPAIR:
        print("  ⚠️ No keypair loaded — skipping profit collection")
        return

    sol_bal = get_sol_balance()
    usdc_bal = get_usdc_balance()
    print(f"  Solana wallet: {sol_bal:.6f} SOL + ${usdc_bal:.4f} USDC")

    # Reserve: 0.3 SOL for gas + sniping, 2 USDC for loop trading
    SOL_RESERVE = 0.3
    USDC_RESERVE = 2.0

    profit_collected = 0.0

    # Convert excess USDC to SOL
    if usdc_bal > USDC_RESERVE:
        excess_usdc = usdc_bal - USDC_RESERVE
        if excess_usdc >= MIN_PROFIT_USDC:
            print(f"  Converting ${excess_usdc:.4f} excess USDC → SOL...")
            amount_raw = int(excess_usdc * 1e6)
            ok, sig, details = jup_swap(USDC_MINT, SOL_MINT, amount_raw)
            if ok:
                print(f"  ✅ Converted! TX: {sig[:20]}...")
                profit_collected += excess_usdc
                time.sleep(2)
            else:
                print(f"  ❌ Convert failed: {details}")

    # Convert profitable/dead tokens to SOL
    try:
        tokens = _load_json(LOOP_TOKENS, {})
        if not isinstance(tokens, dict):
            tokens = {}
        learned = load_learned_params()
        for sym, info in list(tokens.items()):
            # Skip cross-chain (Base) tokens — not Solana-swappable
            if info.get("chain") == "base":
                continue
            mint = info.get("mint", "")
            if not mint:
                continue
            bal = get_token_balance(mint)
            if bal <= 0:
                continue

            token_info = (learned or {}).get("tokens", {}).get(sym, {})
            is_profitable = token_info.get("is_profitable", False)
            is_dead = token_info.get("is_dead", False)

            if is_dead:
                sell_pct = 1.0  # Dump everything
                print(f"  Dumping dead token {sym} ({bal:,.0f}) → SOL...")
            elif is_profitable and bal > info.get("min_trade_usdc", 0.05):
                sell_pct = 0.3  # Take 30% profit
                print(f"  Taking 30% profit from {sym} ({bal * sell_pct:,.0f}) → SOL...")
            else:
                continue

            decimals = info.get("decimals", 6)
            sell_amount = int(bal * sell_pct * (10 ** decimals))
            if sell_amount > 1000:
                ok, sig, details = jup_swap(mint, SOL_MINT, sell_amount, slippage=1000)
                if ok:
                    print(f"  ✅ Sold {sym} → SOL! TX: {sig[:20]}...")
                    profit_collected += 1  # approximate
                    time.sleep(2)
                else:
                    # Try via USDC first then SOL next cycle
                    if "too large" in details.lower() or "slippage" in details.lower():
                        ok2, sig2, det2 = jup_swap(mint, USDC_MINT, sell_amount, slippage=1000)
                        if ok2:
                            print(f"  ✅ Sold {sym} → USDC (will convert next cycle)")
                            time.sleep(2)
                        else:
                            print(f"  ❌ {sym} sell failed: {det2}")
    except Exception as e:
        print(f"  Token sweep error: {e}")

    # Check final SOL balance
    final_sol = get_sol_balance()
    print(f"  Final SOL: {final_sol:.6f}")

    # Send excess SOL to user's wallet
    if user_wallet and final_sol > SOL_RESERVE:
        excess_sol = final_sol - SOL_RESERVE
        if excess_sol >= 0.01:  # At least 0.01 SOL to send
            print(f"  💸 Sending {excess_sol:.6f} SOL profit to {user_wallet[:12]}...")
            try:
                from solders.system_program import transfer, TransferParams
                from solders.pubkey import Pubkey
                from solders.message import MessageV0
                from solders.hash import Hash

                # Get latest blockhash
                resp = requests.post(SOL_RPC, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                    "params": [{"commitment": "confirmed"}]
                }, timeout=15)
                blockhash_str = resp.json()["result"]["value"]["blockhash"]
                blockhash = Hash.from_string(blockhash_str)

                # Build transfer instruction
                transfer_ix = transfer(TransferParams(
                    from_pubkey=Pubkey.from_string(WALLET),
                    to_pubkey=Pubkey.from_string(user_wallet),
                    lamports=int(excess_sol * 1e9) - 5000
                ))

                # Build message
                msg = MessageV0.try_compile(
                    Pubkey.from_string(WALLET),  # payer
                    [transfer_ix],
                    [],
                    blockhash,
                )

                # Sign and send
                tx = VersionedTransaction(msg, [KEYPAIR])
                signed_b64 = base64.b64encode(bytes(tx)).decode()

                resp2 = requests.post(SOL_RPC, json={
                    "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                    "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
                }, timeout=60)
                result = resp2.json()
                if "error" in result:
                    print(f"  ❌ Transfer failed: {result['error'].get('message','')[:200]}")
                else:
                    print(f"  ✅ SOL sent! TX: {result['result'][:20]}...")
                    print(f"  → {excess_sol:.6f} SOL delivered to {user_wallet}")
            except Exception as e:
                print(f"  Transfer error: {e}")

    # Log profit
    if profit_collected > 0:
        try:
            profit_data = _load_json(PROFIT_LOG, {"total_collected": 0, "history": []})
            if isinstance(profit_data, list):
                profit_data = {"total_collected": 0, "history": profit_data}
            if not isinstance(profit_data, dict):
                profit_data = {"total_collected": 0, "history": []}

            profit_data["total_collected"] = profit_data.get("total_collected", 0) + profit_collected
            profit_data["history"].append({
                "time": datetime.utcnow().isoformat(),
                "amount_usdc": profit_collected,
                "sol_balance_after": final_sol,
            })

            with open(PROFIT_LOG, "w") as f:
                json.dump(profit_data, f, indent=2)

            print(f"  Total profits collected: ${profit_data['total_collected']:.4f}")
        except Exception as e:
            print(f"  Profit log error: {e}")

# ============ MAIN ============

def run():
    print(f"\n🧠 Learning Engine — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Analyzing all trade logs (sol_trade_log + profit_engine_log + sniper_bags)...")

    # Ensure OGSAINT is tracked
    ensure_ogsaint_token()

    # 1. Analyze trades from all 3 logs
    learned = analyze_trades()
    if learned:
        save_learned_params(learned)

        print(f"\n   📊 ANALYSIS:")
        print(f"   Total trades: {learned['total_trades']}")
        print(f"   Paired (BUY→SELL): {learned['paired_trades']}")
        print(f"   Win rate: {learned['win_rate']:.1f}%")
        print(f"   Total PnL: ${learned['total_pnl']:.4f}")

        print(f"\n   🎯 TOKEN RECOMMENDATIONS:")
        for sym, info in sorted(learned["tokens"].items(), key=lambda x: x[1]["total_pnl"], reverse=True):
            streak_str = f"streak={info['current_streak']:+d}"
            mult_str = f"×{info['position_size_multiplier']:.2f}"
            dead_str = " ☠️DEAD" if info["is_dead"] else ""
            print(
                f"   {sym:12s} WR={info['win_rate']:5.1f}% PnL=${info['total_pnl']:+.4f} "
                f"{streak_str} {mult_str} → {info['recommendation']}{dead_str}"
            )

        if learned["auto_corrections"]:
            print(f"\n   🔧 AUTO-CORRECTIONS ({len(learned['auto_corrections'])}):")
            for ac in learned["auto_corrections"]:
                print(f"   • {ac['correction']}")

        if learned["global_lessons"]:
            print(f"\n   💡 LESSONS:")
            for l in learned["global_lessons"]:
                print(f"   • {l}")

        # 2. Apply learnings to loop strategy
        print(f"\n   ⚙️ Applying learnings to loop strategy...")
        apply_learnings_to_loop()

        # 3. Generate status report
        print(f"\n   📝 Generating status report...")
        generate_status_report(learned)

    # 4. Collect profits to SOL + send excess to user wallet
    print(f"\n   💰 Collecting profits → SOL...")
    collect_profits_to_sol(USER_SOL_WALLET)

    print(f"\n   ✅ Learning cycle complete")


# ─── Self-Healing Cron Mesh ──────────────────────────────────────────────
# Any surviving cron script rebuilds all jobs (including watchdog) on run.
import sys as _sys, os as _os
_sys.path.insert(0, "/workspace")
try:
    from cron_watchdog import ensure_all_crons
    _recreated = ensure_all_crons()
    # Check $1M goal — halt if achieved
    try:
        from goal_tracker import check_goal
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
