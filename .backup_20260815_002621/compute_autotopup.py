#!/usr/bin/env python3
"""
Auto compute top-up for all Armadillo agents.
Checks each agent's USDC balance on Base and tops up compute if >= $1.
Runs via Hermes cron every 10 minutes.
"""
import subprocess
import json
import os
import sys
from datetime import datetime

# MUST set this for headless Linux - cross-keychain file backend
ENV = os.environ.copy()
ENV["TS_KEYRING_BACKEND"] = "file"

CONFIG_PATH = os.path.expanduser("~/.config/acp/config.json")
LOG_PATH = os.path.expanduser("~/.hermes/logs/compute_autotopup.log")

AGENTS = [
    {"name": "ArmaBase",      "evm": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc", "id": "019fbb50-31de-7e2f-be3b-2225023960b3"},
    {"name": "Scout",         "evm": "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4", "id": "019fa674-7be2-72ce-956c-3a7f831e9102"},
    {"name": "Saint-ARMAD",   "evm": "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d", "id": "019f9f75-493a-7011-b547-aa9c2df1a1ac"},
    {"name": "Saint-OGSAINT", "evm": "0x52a140c6dab119a6a050f857591bbf469c1856ce", "id": "019f9f75-130e-75fc-9459-5358c8d25206"},
]

CHAIN_ID = 8453  # Base
MIN_TOPUP = 1.0  # Minimum $1 for compute top-up
RESERVE_USDC = 0.0  # Keep $0 reserve (auto-billing handles everything)

def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as f:
            f.write(line + '\n')
    except:
        pass

def write_active_wallet(evm_addr):
    """Write activeWallet to config.json (crons may overwrite between calls)."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        config["activeWallet"] = evm_addr
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        log(f"  ERROR writing config: {e}")

def get_usdc_balance(evm_addr):
    """Get USDC balance on Base for the given agent wallet."""
    write_active_wallet(evm_addr)
    r = subprocess.run(
        ["acp", "wallet", "balance", "--chain-id", str(CHAIN_ID), "--json"],
        capture_output=True, text=True, timeout=30, env=ENV
    )
    raw = r.stdout.strip()
    clean = raw.split('\n[acp-wrapper]')[0].strip()
    try:
        data = json.loads(clean)
        for t in data.get("tokens", []):
            meta = t.get("tokenMetadata", {}) or {}
            sym = (meta.get("symbol") or "").upper()
            if sym == "USDC":
                bal_raw = t.get("tokenBalance", "0x0")
                if isinstance(bal_raw, str) and bal_raw.startswith("0x"):
                    bal_val = int(bal_raw, 16)
                else:
                    bal_val = int(bal_raw) if bal_raw else 0
                decimals = meta.get("decimals", 6)
                return bal_val / (10 ** decimals)
    except Exception as e:
        log(f"  ERROR parsing balance: {e}")
    return 0.0

def get_compute_status(evm_addr):
    """Get compute limitRemaining for the agent."""
    write_active_wallet(evm_addr)
    r = subprocess.run(
        ["acp", "compute", "status", "--json"],
        capture_output=True, text=True, timeout=30, env=ENV
    )
    raw = r.stdout.strip()
    clean = raw.split('\n[acp-wrapper]')[0].strip()
    try:
        data = json.loads(clean)
        return data.get("limitRemaining", 0)
    except:
        return 0

def topup_compute(evm_addr, amount):
    """Run compute top-up for the agent."""
    write_active_wallet(evm_addr)
    # Round down to 2 decimals
    amount = round(amount, 2)
    if amount < MIN_TOPUP:
        return False, f"Amount ${amount} below minimum ${MIN_TOPUP}"
    
    r = subprocess.run(
        ["acp", "compute", "top-up", "--amount", str(amount), "--chain-id", str(CHAIN_ID), "--json"],
        capture_output=True, text=True, timeout=60, env=ENV
    )
    raw = r.stdout.strip()
    clean = raw.split('\n[acp-wrapper]')[0].strip()
    try:
        data = json.loads(clean)
        if "txnHash" in data or "amount" in data:
            return True, data
        elif "error" in data:
            return False, data.get("error", "unknown error")
        return True, data
    except:
        return False, f"Parse error: {raw[:200]}"

def main():
    log("=" * 50)
    log("Compute auto-top-up check starting")
    log("=" * 50)
    
    for agent in AGENTS:
        name = agent["name"]
        evm = agent["evm"]
        
        usdc = get_usdc_balance(evm)
        compute_remaining = get_compute_status(evm)
        
        log(f"{name}: USDC=${usdc:.4f} | Compute remaining={compute_remaining:.2f}")
        
        # If USDC >= $1, top up compute
        topup_amount = usdc - RESERVE_USDC
        if topup_amount >= MIN_TOPUP:
            log(f"  -> Topping up ${topup_amount:.2f} compute...")
            success, result = topup_compute(evm, topup_amount)
            if success:
                tx = result.get("txnHash", "N/A") if isinstance(result, dict) else "N/A"
                log(f"  ✅ Top-up success! TX: {tx}")
            else:
                log(f"  ❌ Top-up failed: {result}")
        else:
            log(f"  -> Skip (USDC ${usdc:.4f} < minimum ${MIN_TOPUP})")
    
    log("Done.")

if __name__ == "__main__":
    main()
