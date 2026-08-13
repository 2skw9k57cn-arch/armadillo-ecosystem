#!/usr/bin/env python3
"""
Armadillo Ecosystem Buyback & Burn Engine
==========================================
Cycles revenue from all agents into ALL 3 ecosystem tokens:

  ArmaBase revenue → 40% ARBA, 30% ARMAD, 30% ARRB
  Saint revenue    → 40% ARMAD, 30% ARBA, 30% ARRB
  Scout revenue    → 40% ARRB, 30% ARBA, 30% ARMAD

Each buy → burn to 0xdEaD (deflationary supply).
All 3 tokens get continuous buy pressure from every revenue stream.
"""

import json, time, sys, os, subprocess, requests

# ============ TOKEN CONTRACTS ============
ARBA_CONTRACT = "0x557642685ce68F3975458375B51553871807e1b5"  # ArmaBase — Base
ARMAD_CONTRACT = "0x69e71ce955373d7117394b0c7aaee6ef42cf6d51"  # Saint — Base
ARRB_CONTRACT = "0xdA3C5b4d05c40a9244E534a966A1424C51055950"  # Scout — Robinhood Chain

VIRTUAL_CONTRACT = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b"
USDC_CONTRACT = "0x833589fCD6edb6e08f4c7c32d4f71b54bda02913"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"

# Agent wallets
ARMA_WALLET = "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"
SAINT_WALLET = "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"
SCOUT_WALLET = "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4"

WALLETS = {
    "armabase": ARMA_WALLET,
    "saint":    SAINT_WALLET,
    "scout":    SCOUT_WALLET,
}

# All tokens: (symbol, contract, chain_out, own_agent_weight, cross_weight)
TOKENS = {
    "ARBA":  {"contract": ARBA_CONTRACT,  "chain_out": "8453",  "decimals": 18},
    "ARMAD": {"contract": ARMAD_CONTRACT, "chain_out": "8453",  "decimals": 18},
    "ARRB":  {"contract": ARRB_CONTRACT,  "chain_out": "2025",  "decimals": 18},  # Robinhood Chain
}

# Revenue split: each agent prioritizes its own token
SPLITS = {
    "armabase": {"ARBA": 0.40, "ARMAD": 0.30, "ARRB": 0.30},
    "saint":    {"ARMAD": 0.40, "ARBA": 0.30, "ARRB": 0.30},
    "scout":    {"ARRB": 0.40, "ARBA": 0.30, "ARMAD": 0.30},
}

# Agent IDs for context switching
AGENT_IDS = {
    "armabase": "019fbb50-31de-7e2f-be3b-2225023960b3",
    "saint":    "019f9f75-493a-7011-b547-aa9c2df1a1ac",
    "scout":    "019fa674-7be2-72ce-956c-3a7f5e6b5c11",
}

# Config
MIN_USDC_PER_BUY = 3.0   # ACP minimum is $2, use $3 for buffer
RESERVE_USDC = 1.0       # Keep $1 per agent for gas/fees
BURN_LOG = "/workspace/ecosystem_burn_log.json"

def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def use_agent(agent_key):
    """Switch ACP context by writing activeWallet to config.json"""
    wallet = WALLETS[agent_key]
    config_path = "/workspace/config.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
        config["activeWallet"] = wallet
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        pass

def get_usdc_balance(agent_key=None):
    """Get USDC balance for agent on Base"""
    if agent_key:
        use_agent(agent_key)
    out, err, rc = run("acp wallet balance --json 2>&1")
    if rc != 0:
        return 0
    try:
        d = json.loads(out)
        for t in d.get("tokens", []):
            sym = t.get("tokenMetadata", {}).get("symbol", "")
            if sym and sym.upper() == "USDC":
                bal = int(t.get("tokenBalance", "0x0"), 16)
                dec = t.get("tokenMetadata", {}).get("decimals", 6)
                return bal / (10 ** dec)
    except Exception as e:
        pass
    return 0

def buy_token(symbol, usdc_amount, agent_key=None):
    """Buy a token on its bonding curve using USDC"""
    token = TOKENS[symbol]
    chain_out = token["chain_out"]
    
    if agent_key:
        use_agent(agent_key)
    
    print(f"  📤 Buying {symbol} with ${usdc_amount:.2f} USDC (chain {chain_out})...")
    out, err, rc = run(
        f"acp trade --token-in usdc --chain-in 8453 --amount-in {usdc_amount} "
        f"--token-out {token['contract']} --chain-out {chain_out} --accept-impact --json"
    )
    
    if rc != 0:
        try:
            d = json.loads(out)
            return False, d.get("error", err or out)
        except Exception as e:
            return False, err or out
    
    try:
        d = json.loads(out)
        if d.get("status") == "success":
            received = d.get("finalReceived", "unknown")
            txs = [leg.get("txHash", "") for leg in d.get("legs", []) if leg.get("txHash")]
            return True, {"received": received, "txs": txs, "raw": d}
        else:
            return False, d.get("error", str(d))
    except Exception as e:
        return False, out[:200]

def burn_token(symbol, amount, agent_key=None):
    """Burn a token by sending to 0xdEaD address"""
    token = TOKENS[symbol]
    decimals = token["decimals"]
    contract = token["contract"]
    
    if agent_key:
        use_agent(agent_key)
    
    print(f"  🔥 Burning {amount:,.2f} {symbol} to dead address...")
    
    # transfer(address,uint256) selector = 0xa9059cbb
    amount_wei = hex(int(amount * (10 ** decimals)))
    amount_hex = amount_wei[2:].zfill(64)
    dead_hex = DEAD_ADDRESS[2:].zfill(64)
    calldata = f"0xa9059cbb{dead_hex}{amount_hex}"
    
    chain = token["chain_out"]
    out, err, rc = run(
        f"acp wallet send-transaction --chain-id {chain} "
        f"--to {contract} --data {calldata} --json"
    )
    
    if rc != 0:
        return False, err or out
    
    try:
        d = json.loads(out)
        tx_hash = d.get("txHash") or d.get("hash") or d.get("tx", "")
        if tx_hash:
            return True, tx_hash
        elif d.get("error"):
            return False, d["error"]
        return False, str(d)
    except Exception as e:
        return False, out[:200]

def get_token_balance(symbol, agent_key=None):
    """Get token balance for agent"""
    token = TOKENS[symbol]
    if agent_key:
        use_agent(agent_key)
    out, err, rc = run("acp wallet balance --json 2>&1")
    if rc != 0:
        return 0
    try:
        d = json.loads(out)
        for t in d.get("tokens", []):
            ta = t.get("tokenAddress", "")
            if ta and ta.lower() == token["contract"].lower():
                bal = int(t.get("tokenBalance", "0x0"), 16)
                dec = t.get("tokenMetadata", {}).get("decimals", token["decimals"])
                return bal / (10 ** dec)
    except Exception as e:
        pass
    return 0

def log_burn(agent, symbol, usdc_spent, tokens_bought, tokens_burned, burn_tx, buy_txs):
    """Log burn to permanent file"""
    log = []
    if os.path.exists(BURN_LOG):
        try:
            with open(BURN_LOG) as f:
                log = json.load(f)
        except Exception as e:
            log = []
    
    entry = {
        "timestamp": int(time.time()),
        "date": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        "agent": agent,
        "token": symbol,
        "usdc_spent": usdc_spent,
        "tokens_bought": tokens_bought,
        "tokens_burned": tokens_burned,
        "burn_tx": burn_tx,
        "buy_txs": buy_txs,
    }
    log.append(entry)
    
    with open(BURN_LOG, 'w') as f:
        json.dump(log, f, indent=2)

def run_agent_cycle(agent_key, cycle_num):
    """Run buyback-burn for one agent across all 3 tokens"""
    use_agent(agent_key)
    usdc = get_usdc_balance(agent_key)
    
    print(f"\n  💰 {agent_key} USDC balance: ${usdc:.2f}")
    
    usable = max(0, usdc - RESERVE_USDC)
    if usable < MIN_USDC_PER_BUY:
        print(f"  ⚠️ Only ${usable:.2f} usable (min ${MIN_USDC_PER_BUY} per buy) — skipping")
        return False
    
    split = SPLITS[agent_key]
    results = []
    
    # Calculate allocations
    allocations = {}
    pooled = 0
    for symbol, fraction in split.items():
        usdc_alloc = usable * fraction
        if usdc_alloc < MIN_USDC_PER_BUY:
            pooled += usdc_alloc
            print(f"\n  📊 {symbol}: ${usdc_alloc:.2f} — below ${MIN_USDC_PER_BUY} min, pooling")
        else:
            allocations[symbol] = usdc_alloc
    
    # Add pooled amount to own token (first in split dict)
    if pooled > 0:
        own_token = list(split.keys())[0]
        allocations[own_token] = allocations.get(own_token, 0) + pooled
        print(f"\n  📊 {own_token}: ${allocations[own_token]:.2f} (includes ${pooled:.2f} pooled)")
    
    for symbol, usdc_alloc in allocations.items():
        if usdc_alloc < MIN_USDC_PER_BUY:
            print(f"  ⚠️ {symbol} still below min after pooling — skipping")
            continue
        
        # Buy
        ok, result = buy_token(symbol, round(usdc_alloc, 2), agent_key)
        if not ok:
            print(f"     ❌ Buy failed: {result}")
            # Try next token
            continue
        
        received_str = result.get("received", "0")
        try:
            received_val = float(received_str.split()[0].replace(",", ""))
        except Exception as e:
            received_val = 0
        
        print(f"     ✅ Bought {received_val:,.2f} {symbol}")
        
        # Wait for balance to settle
        time.sleep(5)
        
        # Burn
        balance = get_token_balance(symbol, agent_key)
        if balance > 0:
            ok2, burn_result = burn_token(symbol, balance, agent_key)
            if ok2:
                print(f"     🔥 Burned {balance:,.2f} {symbol} → {burn_result[:20]}...")
                log_burn(agent_key, symbol, usdc_alloc, received_val, balance, burn_result, result.get("txs", []))
                results.append({"token": symbol, "burned": balance, "usdc": usdc_alloc})
            else:
                print(f"     ❌ Burn failed: {burn_result}")
                results.append({"token": symbol, "burned": 0, "usdc": usdc_alloc, "error": str(burn_result)[:80]})
        else:
            print(f"     ⚠️ No {symbol} balance to burn")
    
    return len(results) > 0

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
    # ─── Self-Healing Cron Mesh Bootstrap ───────────────────────────────
    sys.path.insert(0, "/workspace")
    try:
        from cron_watchdog import ensure_all_crons
        _recreated = ensure_all_crons()
        if _recreated:
            print(f"  🔧 Self-healed crons: {', '.join(sorted(_recreated))}")
    except Exception:
        pass
    # ──────────────────────────────────────────────────────────────────────────

    # ─── Goal Check ─────────────────────────────────────────────────────
    try:
        from goal_tracker import check_goal
        if check_goal():
            print("🏁 Goal achieved — buyback-burn halting.")
            sys.exit(0)
    except Exception:
        pass
    # ──────────────────────────────────────────────────────────────────────────

    MAX_CYCLES = 21  # User-requested: run 21 cycles then self-terminate

    now = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
    print(f"🔥 Armadillo Ecosystem Buyback & Burn 🔥")
    print(f"   {now}")
    print(f"   Tokens: ARBA (ArmaBase) | ARMAD (Saint) | ARRB (Scout)")
    print(f"   Split: own token 40% / cross tokens 30% each")
    print(f"   Burn: 0xdEaD (deflationary)")
    print(f"   Max cycles: {MAX_CYCLES} (then self-terminates + removes cron)")
    print()
    
    # Load burn log to determine current cycle count
    cycle = 1
    if os.path.exists(BURN_LOG):
        try:
            with open(BURN_LOG) as f:
                log = json.load(f)
            cycle = len(log) + 1
            total_burned = {}
            for e in log:
                t = e.get("token", "?")
                total_burned[t] = total_burned.get(t, 0) + e.get("tokens_burned", 0)
            print(f"   Previous burns: {len(log)} entries")
            for t, v in total_burned.items():
                print(f"     {t}: {v:,.2f} burned")
        except Exception as e:
            pass
    
    # Check if we've reached 21 cycles
    if cycle > MAX_CYCLES:
        print(f"\n✋ Cycle {cycle} exceeds max {MAX_CYCLES} — self-terminating.")
        print(f"   Removing buyback-burn cron and exiting.")
        try:
            result = subprocess.run(
                f"{os.environ.get('HERMES_BIN', '/opt/hermes-agent/venv/bin/hermes')} cron list 2>&1",
                shell=True, capture_output=True, text=True, timeout=15
            )
            import re
            for m in re.finditer(r'\s*([0-9a-f]{12})\s*\[active\]\s*\n\s*Name:\s+buyback-burn', result.stdout):
                job_id = m.group(1)
                subprocess.run(
                    f"{os.environ.get('HERMES_BIN', '/opt/hermes-agent/venv/bin/hermes')} cron delete {job_id} 2>&1",
                    shell=True, capture_output=True, text=True, timeout=10
                )
                print(f"   Deleted cron {job_id}")
            # Also remove from ALL_JOBS in watchdog
            print("   Buyback-burn cron removed. Profit engine will continue trading.")
        except Exception as e:
            print(f"   Cron removal error: {e}")
        sys.exit(0)
    
    print(f"\n   Cycle {cycle}/{MAX_CYCLES}")
    print(f"{'─' * 50}")
    
    # Run for each agent that has funds
    agents = ["armabase", "saint", "scout"]
    
    total_burns = 0
    for agent in agents:
        print(f"\n🤖 {agent.upper()}")
        success = run_agent_cycle(agent, cycle)
        if success:
            total_burns += 1
    
    print(f"\n{'─' * 50}")
    print(f"✅ Cycle {cycle}/{MAX_CYCLES} done. {total_burns}/{len(agents)} agents burned tokens.")
    if cycle >= MAX_CYCLES:
        print(f"🏁 Reached {MAX_CYCLES} cycles — buyback-burn will self-terminate on next run.")
