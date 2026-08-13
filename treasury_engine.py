#!/usr/bin/env python3
"""
Treasury Engine — Central SOL treasury management for the Armadillo ecosystem.

GOAL: Accumulate $1,000,000 USD worth of SOL and send to user's wallet.
     EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd

ARCHITECTURE:
  1. Scans all agent wallets (EVM + Solana + Hyperliquid) for profits
  2. Consolidates everything into SOL on Solana
  3. Sends to user's personal wallet
  4. Tracks progress toward $1M goal
  5. Learns from trade history to optimize revenue allocation
  6. Adjusts strategy weights based on what's working

REVENUE SOURCES (ranked by historical performance):
  - Pump.fun sniper: 60.7% win rate, small but frequent profits
  - Saint HL perps: leveraged positions, larger P&L swings
  - Ecosystem token loops: volume generation, marginal profit

LEARNING:
  - Tracks win/loss ratio per strategy
  - Adjusts capital allocation: winning strategies get more budget
  - Adjusts risk parameters: stop-loss, take-profit, position sizing
  - Writes learning state to /workspace/treasury_learning.json

RUNS: Every 15 minutes via Hermes cron
"""

import os
import sys
import json
import time
import subprocess
import re
from datetime import datetime, timezone

# ─── CONFIG ────────────────────────────────────────────────────────
PERSONAL_WALLET = "EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd"
GOAL_USD = 1_000_000

AGENTS = [
    {"name": "ArmaBase",      "evm": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc", "id": "019fbb50-31de-7e2f-be3b-2225023960b3"},
    {"name": "Scout",         "evm": "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4", "id": "019fa674-7be2-72ce-956c-3a7f831e9102"},
    {"name": "Saint-ARMAD",   "evm": "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d", "id": "019f9f75-493a-7011-b547-aa9c2df1a1ac"},
    {"name": "Saint-OGSAINT", "evm": "0x52a140c6dab119a6a050f857591bbf469c1856ce", "id": "019f9f75-130e-75fc-9459-5358c8d25206"},
]

GOAL_STATE_FILE = "/workspace/goal_state.json"
TREASURY_LOG = "/workspace/treasury_log.json"
LEARNING_FILE = "/workspace/treasury_learning.json"
SOL_TRADE_LOG = "/workspace/sol_trade_log.json"
SAINT_POSITIONS = "/workspace/saint_positions.json"

# Reserve kept on each agent for gas/fees (in SOL equivalent)
GAS_RESERVE_USD = 0.50
# Minimum SOL accumulated before sending to user wallet (accumulation threshold)
MIN_SEND_SOL = 20.0
# Revenue split: % sent to treasury vs reinvested
TREASURY_PCT = 80  # 80% to user wallet, 20% stays for trading

CONFIG_PATH = os.path.expanduser("~/.config/acp/config.json")

# ─── UTILITIES ─────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def run(cmd, timeout=30):
    """Run a shell command, return (stdout, stderr, exit_code)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except Exception as e:
        return "", str(e), -1

def acp_cmd(args, timeout=30, agent_wallet=None):
    """Run an ACP CLI command with the file keychain backend.
    Writes activeWallet to config.json immediately before the call
    to avoid race conditions with other cron jobs."""
    env = os.environ.copy()
    env["TS_KEYRING_BACKEND"] = "file"
    
    if agent_wallet:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            cfg["activeWallet"] = agent_wallet
            with open(CONFIG_PATH, 'w') as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass
    
    try:
        r = subprocess.run(["acp"] + args, capture_output=True, text=True, timeout=timeout, env=env)
        raw = r.stdout.strip()
        # Strip acp-wrapper debug lines
        clean = raw.split('\n[acp-wrapper]')[0].strip()
        try:
            return json.loads(clean), r.returncode == 0
        except json.JSONDecodeError:
            return {"raw": clean, "stderr": r.stderr.strip()}, r.returncode == 0
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}, False
    except Exception as e:
        return {"error": str(e)}, False

def get_sol_price():
    """Get current SOL price in USD from DexScreener."""
    try:
        import urllib.request
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        pairs = data.get("pairs", [])
        # Find the SOL pair (not wrapped-SOL variants with different symbols)
        for p in pairs:
            sym = (p.get("baseToken", {}) or {}).get("symbol", "")
            price = p.get("priceUsd")
            if sym.upper() == "SOL" and price:
                return float(price)
    except Exception:
        pass
    # Fallback: try Jupiter price API
    try:
        import urllib.request
        url = "https://price.jup.ag/v6/price?ids=So11111111111111111111111111111111111111112"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return float(data.get("data", {}).get("So11111111111111111111111111111111111111112", {}).get("price", 75.0))
    except Exception:
        pass
    return 75.0  # fallback SOL price

def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  ⚠️ Failed to save {path}: {e}")

# ─── BALANCE SCANNING ──────────────────────────────────────────────

def scan_all_balances():
    """Scan all agents across all chains for any tokens with value.
    Returns dict: {agent_name: {chain: {token: balance_usd}}}"""
    results = {}
    sol_price = get_sol_price()
    
    for agent in AGENTS:
        name = agent["name"]
        evm = agent["evm"]
        results[name] = {"evm": {}, "solana": {}, "hl": {}}
        
        # EVM balances
        data, ok = acp_cmd(["wallet", "balance", "--json"], agent_wallet=evm)
        if ok and isinstance(data, dict):
            for chain in data.get("chains", []):
                chain_id = chain.get("chainId")
                chain_name = {1:"ethereum", 8453:"base", 42161:"arbitrum", 4663:"robinhood"}.get(chain_id, f"chain_{chain_id}")
                native = float(chain.get("nativeBalance", 0))
                if native > 0:
                    results[name]["evm"][chain_name] = {"native": native}
                for t in chain.get("tokens", []):
                    meta = t.get("tokenMetadata", {}) or {}
                    sym = (meta.get("symbol") or "").upper()
                    bal_raw = t.get("tokenBalance", "0x0")
                    bal_val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw or 0)
                    dec = meta.get("decimals") or 18
                    bal = bal_val / (10 ** dec)
                    if bal > 0.001 and sym:
                        results[name]["evm"].setdefault(chain_name, {})[sym] = bal
        
        # Solana balance
        sol_data, sol_ok = acp_cmd(["wallet", "sol", "balance", "--json"], agent_wallet=evm)
        if sol_ok and isinstance(sol_data, dict):
            sol_bal = float(sol_data.get("balance", 0))
            if sol_bal > 0:
                results[name]["solana"]["SOL"] = sol_bal
            for t in sol_data.get("tokens", []):
                sym = (t.get("symbol") or "").upper()
                bal = float(t.get("balance", 0))
                if bal > 0.001 and sym:
                    results[name]["solana"][sym] = bal
        
        # Hyperliquid (Saint-ARMAD only)
        if name == "Saint-ARMAD":
            hl_data, hl_ok = acp_cmd(["trade", "hl-status", "--json"], agent_wallet=evm)
            if hl_ok and isinstance(hl_data, dict):
                withdrawable = float(hl_data.get("withdrawable", 0))
                if withdrawable > 0:
                    results[name]["hl"]["withdrawable_usdc"] = withdrawable
                for pos in hl_data.get("positions", []):
                    pnl = float(pos.get("unrealizedPnl", 0))
                    results[name]["hl"].setdefault("positions", []).append({
                        "token": pos["token"],
                        "pnl": pnl,
                        "size": float(pos["size"]),
                        "entry": float(pos["entryPx"]),
                        "leverage": pos["leverage"]["value"],
                    })
    
    return results, sol_price

# ─── LEARNING ENGINE ───────────────────────────────────────────────

def load_learning_state():
    """Load or initialize learning state."""
    state = load_json(LEARNING_FILE, {
        "strategies": {
            "pumpfun_sniper": {
                "wins": 0, "losses": 0, "total_pnl_sol": 0,
                "weight": 0.40,  # allocation weight
                "best_tokens": [],
                "avg_hold_time_mins": 0,
                "best_signal": "rsi_oversold",
            },
            "hl_perps": {
                "wins": 0, "losses": 0, "total_pnl_usdc": 0,
                "weight": 0.35,
                "best_symbols": [],
                "best_leverage": 3,
                "avg_confidence": 68,
            },
            "ecosystem_loop": {
                "wins": 0, "losses": 0, "total_pnl_sol": 0,
                "weight": 0.25,
                "best_tokens": [],
                "optimal_cycle_count": 3,
            },
        },
        "total_cycles": 0,
        "last_rebalance": 0,
        "insights": [],
        "sol_price_history": [],
    })
    return state

def learn_from_trades(learning_state):
    """Analyze trade history and update strategy weights."""
    # Learn from Solana trades
    sol_log = load_json(SOL_TRADE_LOG, {"trades": []})
    trades = sol_log.get("trades", [])
    
    sniper_wins = 0
    sniper_losses = 0
    sniper_pnl = 0
    token_performance = {}  # token -> {wins, losses, pnl}
    
    for t in trades:
        if not isinstance(t, dict):
            continue
        action = t.get("action", "")
        pnl = float(t.get("pnl", 0) or 0)
        sym = t.get("symbol", "unknown")
        
        # Normalize PnL (some entries have broken decimals)
        if abs(pnl) > 10000:
            pnl = pnl / 1e9  # likely raw token units, not SOL
        
        if "SELL" in action:
            if pnl > 0:
                sniper_wins += 1
            elif pnl < 0:
                sniper_losses += 1
            sniper_pnl += pnl
            
            if sym != "unknown":
                if sym not in token_performance:
                    token_performance[sym] = {"wins": 0, "losses": 0, "pnl": 0}
                if pnl > 0:
                    token_performance[sym]["wins"] += 1
                elif pnl < 0:
                    token_performance[sym]["losses"] += 1
                token_performance[sym]["pnl"] += pnl
    
    # Learn from Saint positions
    positions = load_json(SAINT_POSITIONS, [])
    hl_wins = 0
    hl_losses = 0
    hl_pnl = 0
    best_symbols = {}
    
    if isinstance(positions, list):
        for p in positions:
            if isinstance(p, dict):
                # Count current positions
                pnl = 0  # unrealized, will be checked at close
                sym = p.get("symbol", "?")
                confidence = p.get("confidence", 68)
                signal = p.get("signal_type", "unknown")
                
                if sym not in best_symbols:
                    best_symbols[sym] = {"count": 0, "signals": set()}
                best_symbols[sym]["count"] += 1
                if isinstance(best_symbols[sym]["signals"], set):
                    best_symbols[sym]["signals"].add(signal)
    
    # Update learning state
    s = learning_state["strategies"]
    
    # Pump.fun sniper
    if sniper_wins + sniper_losses > 0:
        s["pumpfun_sniper"]["wins"] = sniper_wins
        s["pumpfun_sniper"]["losses"] = sniper_losses
        s["pumpfun_sniper"]["total_pnl_sol"] = sniper_pnl
        win_rate = sniper_wins / (sniper_wins + sniper_losses)
        
        # Best performing tokens
        sorted_tokens = sorted(token_performance.items(), 
                              key=lambda x: x[1]["pnl"], reverse=True)
        s["pumpfun_sniper"]["best_tokens"] = [
            {"token": t, "pnl": v["pnl"], "win_rate": v["wins"]/max(1,v["wins"]+v["losses"])}
            for t, v in sorted_tokens[:5]
        ]
    
    # HL perps
    s["hl_perps"]["best_symbols"] = [
        {"symbol": sym, "count": v["count"]}
        for sym, v in sorted(best_symbols.items(), key=lambda x: -x[1]["count"])[:5]
    ]
    
    # Rebalance weights based on performance
    total_wins = s["pumpfun_sniper"]["wins"] + s["hl_perps"]["wins"] + s["ecosystem_loop"]["wins"]
    total_losses = s["pumpfun_sniper"]["losses"] + s["hl_perps"]["losses"] + s["ecosystem_loop"]["losses"]
    
    if total_wins + total_losses > 5:  # Only rebalance with enough data
        # Calculate win rates
        strategies = ["pumpfun_sniper", "hl_perps", "ecosystem_loop"]
        win_rates = {}
        for strat in strategies:
            w = s[strat]["wins"]
            l = s[strat]["losses"]
            wr = w / max(1, w + l)
            win_rates[strat] = wr
        
        # Allocate weight proportional to win rate (with minimum floor)
        total_wr = sum(win_rates.values())
        if total_wr > 0:
            for strat in strategies:
                new_weight = max(0.10, win_rates[strat] / total_wr)  # 10% minimum
                s[strat]["weight"] = round(new_weight, 2)
        
        learning_state["last_rebalance"] = int(time.time())
        
        # Record insight
        best_strat = max(win_rates, key=win_rates.get)
        insight = f"[{now_utc()}] Rebalanced: {best_strat} leading at {win_rates[best_strat]*100:.0f}% win rate. Weights: " + \
                  ", ".join(f"{k}={v['weight']}" for k, v in s.items())
        learning_state["insights"].append(insight)
        # Keep last 50 insights
        learning_state["insights"] = learning_state["insights"][-50:]
    
    learning_state["total_cycles"] += 1
    
    # Track SOL price
    learning_state["sol_price_history"].append({
        "time": now_utc(),
        "price": get_sol_price()
    })
    learning_state["sol_price_history"] = learning_state["sol_price_history"][-100:]  # keep last 100
    
    return learning_state

# ─── TREASURY ACTIONS ──────────────────────────────────────────────

def consolidate_to_sol(agent, balances, sol_price):
    """Consolidate any USDC/tokens on an agent to SOL on Solana.
    SOL stays on the agent's Solana wallet (accumulation).
    Returns (sol_gained, actions_taken, sol_now_on_agent)."""
    name = agent["name"]
    evm = agent["evm"]
    sol_gained = 0
    actions = []
    
    # 1. Check for withdrawable USDC on Hyperliquid
    hl_usdc = balances.get("hl", {}).get("withdrawable_usdc", 0)
    if hl_usdc >= 2.0:  # ACP minimum
        actions.append(f"Withdrawing ${hl_usdc:.2f} from HL")
        data, ok = acp_cmd(["trade", "withdraw-from-hl", "--amount", str(round(hl_usdc - 0.1, 2)), "--json"],
                          agent_wallet=evm, timeout=60)
        if ok:
            received = float(data.get("finalReceived", "0").split()[0] if isinstance(data.get("finalReceived"), str) else 0)
            actions.append(f"  ✅ Withdrew {received:.2f} USDC from HL → will swap to SOL")
            sol_gained += received / sol_price
        else:
            actions.append(f"  ❌ HL withdrawal failed: {str(data.get('error',''))[:80]}")
    
    # 2. Check for USDC on any EVM chain → swap to SOL on agent's own Solana wallet
    for chain_name, tokens in balances.get("evm", {}).items():
        chain_id_map = {"ethereum": "1", "base": "8453", "arbitrum": "42161", "robinhood": "4663"}
        chain_id = chain_id_map.get(chain_name)
        
        for sym, bal in tokens.items():
            if sym == "native":
                continue
            if sym == "USDC" and bal >= 2.0:
                # Swap USDC → SOL, keep on agent's Solana wallet (no recipient = own wallet)
                actions.append(f"Swapping {bal:.2f} USDC@{chain_name} → SOL (accumulating)")
                data, ok = acp_cmd([
                    "trade",
                    "--token-in", "usdc", "--chain-in", chain_id,
                    "--token-out", "sol", "--chain-out", "501",
                    "--amount-in", str(round(bal - 0.1, 2)),
                    "--json"
                ], agent_wallet=evm, timeout=120)
                
                if ok:
                    received_str = data.get("finalReceived", "0")
                    if isinstance(received_str, str):
                        received = float(received_str.split()[0])
                    else:
                        received = float(received_str or 0)
                    actions.append(f"  ✅ Swapped to {received:.6f} SOL (held on agent)")
                    sol_gained += received
                else:
                    err = str(data.get("error", ""))[:80]
                    if "below" in err.lower():
                        actions.append(f"  ⚠️ Below $2 minimum, accumulating")
                    else:
                        actions.append(f"  ❌ Swap failed: {err}")
    
    # 3. Check current SOL on agent's Solana wallet (accumulated)
    sol_bal = balances.get("solana", {}).get("SOL", 0)
    
    return sol_gained, actions, sol_bal


def check_and_send_treasury(agent_balances, sol_price):
    """Check if total SOL across all agents >= 20 SOL threshold.
    If so, send to user's personal wallet. Returns (sol_sent, actions)."""
    total_sol = 0
    agent_sols = {}
    
    for agent in AGENTS:
        name = agent["name"]
        sol = agent_balances.get(name, {}).get("solana", {}).get("SOL", 0)
        agent_sols[name] = sol
        total_sol += sol
    
    actions = []
    sol_sent = 0
    
    if total_sol >= MIN_SEND_SOL:
        actions.append(f"💰 Accumulated {total_sol:.4f} SOL across agents (≥ {MIN_SEND_SOL} threshold)")
        actions.append(f"   Sending to treasury: {PERSONAL_WALLET[:12]}...")
        
        for agent in AGENTS:
            name = agent["name"]
            evm = agent["evm"]
            sol = agent_sols[name]
            
            if sol > 0.005:  # Keep 0.005 gas
                send_amount = round(sol - 0.005, 4)
                if send_amount > 0:
                    actions.append(f"   [{name}] Sending {send_amount:.4f} SOL")
                    data, ok = acp_cmd(["wallet", "sol", "transfer", "--to", PERSONAL_WALLET,
                                       "--amount", str(send_amount), "--json"],
                                      agent_wallet=evm, timeout=60)
                    if ok:
                        tx = data.get("signature", data.get("tx", ""))
                        actions.append(f"     ✅ Sent (TX: {str(tx)[:20]}...)")
                        sol_sent += send_amount
                    else:
                        actions.append(f"     ❌ Failed: {str(data.get('error',''))[:80]}")
    else:
        actions.append(f"💤 Accumulating: {total_sol:.4f} SOL / {MIN_SEND_SOL} SOL threshold")
        for name, sol in agent_sols.items():
            if sol > 0.001:
                actions.append(f"   {name}: {sol:.4f} SOL")
    
    return sol_sent, actions

def update_goal_state(sol_sent, sol_price):
    """Update the $1M goal tracker."""
    state = load_json(GOAL_STATE_FILE, {
        "goal_usd": GOAL_USD,
        "personal_wallet": PERSONAL_WALLET,
        "achieved": False,
        "total_earned": 0,
        "total_sent_sol": 0,
        "history": [],
        "last_check": "",
    })
    
    usd_value = sol_sent * sol_price
    state["total_sent_sol"] = state.get("total_sent_sol", 0) + sol_sent
    state["total_earned"] = state.get("total_earned", 0) + usd_value
    state["last_check"] = now_utc()
    
    # Check if goal achieved
    if state["total_earned"] >= GOAL_USD:
        state["achieved"] = True
    
    # Add to history
    if sol_sent > 0:
        state.setdefault("history", []).append({
            "time": now_utc(),
            "sol_sent": sol_sent,
            "usd_value": usd_value,
            "sol_price": sol_price,
            "cumulative_usd": state["total_earned"],
        })
        state["history"] = state["history"][-200:]  # keep last 200
    
    save_json(GOAL_STATE_FILE, state)
    
    pct = state["total_earned"] / GOAL_USD * 100
    return state, pct

# ─── MAIN ──────────────────────────────────────────────────────────

def run():
    print(f"🏦 Treasury Engine — {now_utc()}")
    print("=" * 60)
    
    # 1. Learn from trade history
    print("\n📚 Learning from trade history...")
    learning_state = load_learning_state()
    learning_state = learn_from_trades(learning_state)
    save_json(LEARNING_FILE, learning_state)
    
    for strat, info in learning_state["strategies"].items():
        w = info["wins"]
        l = info["losses"]
        wr = w / max(1, w + l) * 100
        print(f"  {strat}: {w}W/{l}L ({wr:.0f}%) weight={info['weight']}")
    
    # 2. Scan all balances
    print("\n🔍 Scanning all agent balances...")
    balances, sol_price = scan_all_balances()
    print(f"  SOL price: ${sol_price:.2f}")
    
    total_value = 0
    for name, bal in balances.items():
        agent_val = 0
        for chain, tokens in bal.get("evm", {}).items():
            for sym, amt in tokens.items():
                if sym == "USDC":
                    agent_val += amt
                    print(f"  {name} EVM/{chain}: {sym} = {amt:.4f}")
        sol = bal.get("solana", {}).get("SOL", 0)
        if sol > 0:
            agent_val += sol * sol_price
            print(f"  {name} Solana: SOL = {sol:.6f} (${sol*sol_price:.2f})")
        hl = bal.get("hl", {}).get("withdrawable_usdc", 0)
        if hl > 0:
            agent_val += hl
            print(f"  {name} HL: withdrawable = ${hl:.2f}")
        hl_positions = bal.get("hl", {}).get("positions", [])
        for pos in hl_positions:
            pnl = pos.get("pnl", 0)
            if pnl != 0:
                print(f"  {name} HL: {pos['token']} PnL = ${pnl:.2f}")
        total_value += agent_val
    
    print(f"\n  Total ecosystem value: ${total_value:.2f}")
    
    # 3. Consolidate profits → swap to SOL (held on agent wallets, accumulating)
    print(f"\n📤 Consolidating profits → SOL (accumulating on agents)...")
    total_sol_converted = 0
    
    for agent in AGENTS:
        agent_balances = balances.get(agent["name"], {})
        sol_gained, actions, sol_held = consolidate_to_sol(agent, agent_balances, sol_price)
        
        if actions:
            print(f"\n  [{agent['name']}] (holding {sol_held:.4f} SOL)")
            for a in actions:
                print(f"    {a}")
            total_sol_converted += sol_gained
    
    if total_sol_converted > 0:
        print(f"\n💰 Converted {total_sol_converted:.6f} SOL this cycle")
    else:
        print(f"\n💤 No profits to convert this cycle")
    
    # 4. Check 20 SOL threshold → send to treasury if reached
    print(f"\n🏦 Treasury threshold check ({MIN_SEND_SOL} SOL)...")
    sol_sent, treasury_actions = check_and_send_treasury(balances, sol_price)
    for a in treasury_actions:
        print(f"  {a}")
    
    # 5. Update goal tracker
    goal_state, pct = update_goal_state(sol_sent, sol_price)
    print(f"\n🎯 Goal: ${goal_state['total_earned']:.2f} / ${GOAL_USD:,} ({pct:.4f}%)")
    print(f"   SOL sent to wallet: {goal_state.get('total_sent_sol', 0):.6f}")
    
    if goal_state.get("achieved"):
        print(f"\n🏆 GOAL ACHIEVED! $1,000,000 reached!")
        # Could trigger celebration / notification here
    
    # 5. Output learning insights
    if learning_state["insights"]:
        latest = learning_state["insights"][-1]
        print(f"\n🧠 Latest insight: {latest}")
    
    print(f"\n{'='*60}")
    print(f"✅ Treasury cycle complete")

if __name__ == "__main__":
    # Ensure crons are healthy
    try:
        sys.path.insert(0, "/workspace")
        from cron_watchdog import ensure_all_crons
        ensure_all_crons()
    except Exception:
        pass
    
    run()
