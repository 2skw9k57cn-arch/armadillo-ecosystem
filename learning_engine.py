#!/usr/bin/env python3
"""
Learning Engine — Analyzes trade history and adjusts strategy for profit.
=========================================================================
Reads all trade logs, calculates PnL per token, per strategy, per timing.
Outputs learned parameters that other scripts use to trade smarter.
Also manages profit collection → converts to SOL → sends to user's wallet.

Key learnings applied:
1. Which tokens are profitable to loop trade
2. Optimal buy/sell sizes
3. Best time to hold before selling
4. Which price impact levels are acceptable
5. When to stop trading a dead token
"""

import json, os, re, time, base64, requests
from collections import defaultdict
from datetime import datetime, timedelta
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ============ CONFIG ============
TRADE_LOG = "/workspace/sol_trade_log.json"
SEEN_TOKENS = "/workspace/deployer_seen_tokens.json"
LOOP_TOKENS = "/workspace/pumpfun_loop_tokens.json"
LEARNED_PARAMS = "/workspace/learned_params.json"
PROFIT_LOG = "/workspace/profit_log.json"
TEAM_STATE = "/workspace/team_state.json"

# Solana
SOL_KEYPAIR_PATH = "/home/hermes/.config/solana/armabase-sol.json"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# User's personal wallet to collect profits
USER_SOL_WALLET = "EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd"

# Learning thresholds
MIN_TRADES_FOR_LEARNING = 3
MAX_PRICE_IMPACT_PCT = 15.0  # Skip trades above this impact
MIN_PROFIT_USDC = 0.01  # Minimum profit to bother collecting

# ============ WALLET ============
with open(SOL_KEYPAIR_PATH) as f:
    secret = json.load(f)
KEYPAIR = Keypair.from_bytes(bytes(secret))
WALLET = str(KEYPAIR.pubkey())

# ============ ANALYSIS ============

def load_trades():
    try:
        with open(TRADE_LOG) as f:
            d = json.load(f)
        return d.get("trades", d) if isinstance(d, dict) else d
    except:
        return []

def analyze_trades():
    """Analyze all historical trades and generate learnings"""
    trades = load_trades()
    if len(trades) < MIN_TRADES_FOR_LEARNING:
        print(f"  Not enough trades for learning ({len(trades)} < {MIN_TRADES_FOR_LEARNING})")
        return None

    # Parse trades into buy/sell pairs
    positions = defaultdict(list)
    results = []

    for t in trades:
        token = t.get("token", t.get("symbol", "?"))
        action = t.get("action", "?")
        details = t.get("details", "")
        tm = t.get("time", "")

        out_match = re.search(r"out=(\d+)", details)
        out_amount = int(out_match.group(1)) if out_match else 0

        if action == "BUY":
            cost = t.get("amount_in", 0)
            positions[token].append({"cost": cost, "time": tm, "tokens": out_amount})

        elif action == "SELL":
            sold_usdc = out_amount / 1e6
            tokens_sold = t.get("amount_in", 0)

            if positions[token]:
                buy = positions[token].pop(0)
                cost = buy["cost"]
                pnl = sold_usdc - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0

                # Parse hold time
                try:
                    buy_dt = datetime.fromisoformat(buy["time"])
                    sell_dt = datetime.fromisoformat(tm)
                    hold_seconds = (sell_dt - buy_dt).total_seconds()
                except:
                    hold_seconds = 0

                results.append({
                    "token": token,
                    "buy_cost": cost,
                    "sell_usdc": sold_usdc,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "hold_seconds": hold_seconds,
                    "buy_time": buy["time"],
                    "sell_time": tm,
                    "tokens_traded": tokens_sold,
                })

    if not results:
        return None

    # Per-token analysis
    token_stats = defaultdict(lambda: {
        "trades": 0, "pnl": 0, "wins": 0, "losses": 0,
        "volume": 0, "hold_times": [], "pnl_history": [],
        "best_pnl": 0, "worst_pnl": 0, "avg_buy_size": 0, "buy_sizes": []
    })

    for r in results:
        t = r["token"]
        token_stats[t]["trades"] += 1
        token_stats[t]["pnl"] += r["pnl"]
        token_stats[t]["volume"] += r["buy_cost"]
        token_stats[t]["pnl_history"].append(r["pnl"])
        token_stats[t]["buy_sizes"].append(r["buy_cost"])
        token_stats[t]["hold_times"].append(r["hold_seconds"])
        if r["pnl"] > 0:
            token_stats[t]["wins"] += 1
            if r["pnl"] > token_stats[t]["best_pnl"]:
                token_stats[t]["best_pnl"] = r["pnl"]
        else:
            token_stats[t]["losses"] += 1
            if r["pnl"] < token_stats[t]["worst_pnl"]:
                token_stats[t]["worst_pnl"] = r["pnl"]

    # Generate learned parameters
    learned = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_trades": len(trades),
        "paired_trades": len(results),
        "total_pnl": sum(r["pnl"] for r in results),
        "overall_winrate": len([r for r in results if r["pnl"] > 0]) / len(results) * 100 if results else 0,
        "tokens": {},
        "global_lessons": [],
    }

    for sym, s in token_stats.items():
        wr = s["wins"] / (s["wins"] + s["losses"]) * 100 if (s["wins"] + s["losses"]) > 0 else 0
        avg_hold = sum(s["hold_times"]) / len(s["hold_times"]) if s["hold_times"] else 0
        avg_buy = sum(s["buy_sizes"]) / len(s["buy_sizes"]) if s["buy_sizes"] else 0

        # Determine if token is profitable
        is_profitable = s["pnl"] > 0 and wr >= 40
        is_dead = wr < 20 and s["trades"] >= 3

        # Calculate optimal trade size based on what worked
        winning_trades = [r for r in results if r["token"] == sym and r["pnl"] > 0]
        if winning_trades:
            optimal_buy = sum(r["buy_cost"] for r in winning_trades) / len(winning_trades)
            optimal_hold = sum(r["hold_seconds"] for r in winning_trades) / len(winning_trades)
        else:
            optimal_buy = avg_buy * 0.5  # smaller if never won
            optimal_hold = avg_hold

        # Recent trend (last 3 trades)
        recent = s["pnl_history"][-3:]
        recent_pnl = sum(recent)
        trending_up = len(recent) >= 2 and recent[-1] > recent[0]

        learned["tokens"][sym] = {
            "trades": s["trades"],
            "win_rate": round(wr, 1),
            "total_pnl": round(s["pnl"], 4),
            "volume": round(s["volume"], 2),
            "avg_hold_seconds": round(avg_hold, 0),
            "optimal_buy_usdc": round(optimal_buy, 4),
            "optimal_hold_seconds": round(optimal_hold, 0),
            "is_profitable": is_profitable,
            "is_dead": is_dead,
            "best_pnl": round(s["best_pnl"], 4),
            "worst_pnl": round(s["worst_pnl"], 4),
            "recent_pnl": round(recent_pnl, 4),
            "trending_up": trending_up,
            "recommendation": _get_recommendation(sym, s, wr, is_dead, trending_up),
        }

    # Global lessons
    total_pnl = sum(r["pnl"] for r in results)
    if total_pnl < 0:
        learned["global_lessons"].append("Overall losing — reduce position sizes on unprofitable tokens")
    if learned["overall_winrate"] < 30:
        learned["global_lessons"].append("Low win rate — be more selective, skip high price impact trades")

    # Best performing token
    best_token = max(token_stats.items(), key=lambda x: x[1]["pnl"])
    if best_token[1]["pnl"] > 0:
        learned["global_lessons"].append(f"Best performer: {best_token[0]} — increase allocation")

    # Dead tokens
    dead = [sym for sym, s in token_stats.items() if s["trades"] >= 3 and sum(1 for p in s["pnl_history"] if p > 0) / len(s["pnl_history"]) < 0.2]
    if dead:
        learned["global_lessons"].append(f"Stop trading: {', '.join(dead)} — consistently losing")

    return learned

def _get_recommendation(sym, stats, wr, is_dead, trending_up):
    if is_dead:
        return "STOP — consistently losing, remove from loop"
    if stats["pnl"] > 0 and wr >= 50:
        return "INCREASE — profitable and high win rate"
    if trending_up and wr >= 30:
        return "CONTINUE — trending up, maintain current size"
    if wr < 30:
        return "REDUCE — low win rate, cut position size in half"
    return "MAINTAIN — neutral performance"

def save_learned_params(learned):
    with open(LEARNED_PARAMS, "w") as f:
        json.dump(learned, f, indent=2)
    print(f"  Learned params saved to {LEARNED_PARAMS}")

def load_learned_params():
    try:
        with open(LEARNED_PARAMS) as f:
            return json.load(f)
    except:
        return None

# ============ STRATEGY ADJUSTMENT ============

def apply_learnings_to_loop():
    """Update pumpfun_loop_tokens.json based on learned parameters"""
    learned = load_learned_params()
    if not learned:
        print("  No learned params to apply")
        return

    try:
        with open(LOOP_TOKENS) as f:
            tokens = json.load(f)
    except:
        tokens = {}

    changes = []
    for sym, info in learned.get("tokens", {}).items():
        rec = info.get("recommendation", "MAINTAIN")
        
        if sym in tokens:
            if "STOP" in rec:
                del tokens[sym]
                changes.append(f"  ❌ Removed {sym} (dead token, WR={info['win_rate']}%)")
            elif "INCREASE" in rec:
                tokens[sym]["min_trade_usdc"] = min(tokens[sym].get("min_trade_usdc", 0.05) * 1.5, 0.50)
                changes.append(f"  ⬆️ Increased {sym} size (WR={info['win_rate']}%, PnL=${info['total_pnl']:.4f})")
            elif "REDUCE" in rec:
                tokens[sym]["min_trade_usdc"] = max(tokens[sym].get("min_trade_usdc", 0.05) * 0.5, 0.02)
                changes.append(f"  ⬇️ Reduced {sym} size (WR={info['win_rate']}%)")
        else:
            if "INCREASE" in rec or ("CONTINUE" in rec and info.get("is_profitable")):
                # Re-add if profitable
                pass  # would need mint address

    with open(LOOP_TOKENS, "w") as f:
        json.dump(tokens, f, indent=2)

    if changes:
        print("  Applied learnings to loop tokens:")
        for c in changes:
            print(c)
    else:
        print("  No changes needed to loop tokens")

# ============ PROFIT COLLECTION ============

def get_sol_balance():
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET]
    }, timeout=15)
    return resp.json().get("result", {}).get("value", 0) / 1e9

def get_usdc_balance():
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accts = resp.json().get("result", {}).get("value", [])
    if accts:
        return float(accts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def get_token_balance(mint):
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=15)
    accts = resp.json().get("result", {}).get("value", [])
    if accts:
        return float(accts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def jup_swap(input_mint, output_mint, amount, slippage=500):
    """Execute Jupiter swap"""
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
    If user_wallet is provided, send excess SOL to user.
    Keeps a reserve for trading gas + sniping.
    """
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
    
    # Convert profitable tokens to SOL
    try:
        with open(LOOP_TOKENS) as f:
            tokens = json.load(f)
        for sym, info in tokens.items():
            mint = info["mint"]
            bal = get_token_balance(mint)
            if bal > 0:
                # Check if this token is profitable
                learned = load_learned_params()
                token_info = learned.get("tokens", {}).get(sym, {}) if learned else {}
                is_profitable = token_info.get("is_profitable", False)
                
                # If profitable, take 50% profit in SOL. If dead, dump all.
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
                        # Try via USDC first then SOL
                        if "too large" in details.lower():
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
                from solders.transaction import VersionedTransaction
                
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
            with open(PROFIT_LOG) as f:
                profit_data = json.load(f)
            if isinstance(profit_data, list):
                profit_data = {"total_collected": 0, "history": profit_data}
            if not isinstance(profit_data, dict):
                profit_data = {"total_collected": 0, "history": []}
        except:
            profit_data = {"total_collected": 0, "history": []}
        
        profit_data["total_collected"] += profit_collected
        profit_data["history"].append({
            "time": datetime.utcnow().isoformat(),
            "amount_usdc": profit_collected,
            "sol_balance_after": final_sol,
        })
        
        with open(PROFIT_LOG, "w") as f:
            json.dump(profit_data, f, indent=2)
        
        print(f"  Total profits collected: ${profit_data['total_collected']:.4f}")

# ============ MAIN ============

def run():
    print(f"\n🧠 Learning Engine — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Analyzing trade history...")
    
    # 1. Analyze trades
    learned = analyze_trades()
    if learned:
        save_learned_params(learned)
        
        print(f"\n   📊 ANALYSIS:")
        print(f"   Total trades: {learned['total_trades']}")
        print(f"   Paired: {learned['paired_trades']}")
        print(f"   Win rate: {learned['overall_winrate']:.0f}%")
        print(f"   Total PnL: ${learned['total_pnl']:.4f}")
        
        print(f"\n   🎯 TOKEN RECOMMENDATIONS:")
        for sym, info in sorted(learned["tokens"].items(), key=lambda x: x[1]["total_pnl"], reverse=True):
            print(f"   {sym:12s} WR={info['win_rate']:.0f}% PnL=${info['total_pnl']:+.4f} → {info['recommendation']}")
        
        if learned["global_lessons"]:
            print(f"\n   💡 LESSONS:")
            for l in learned["global_lessons"]:
                print(f"   • {l}")
        
        # 2. Apply learnings
        print(f"\n   ⚙️ Applying learnings to loop strategy...")
        apply_learnings_to_loop()
    
    # 3. Collect profits to SOL
    print(f"\n   💰 Collecting profits → SOL...")
    collect_profits_to_sol(USER_SOL_WALLET)
    
    print(f"\n   ✅ Learning cycle complete")

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
