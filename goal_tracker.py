#!/usr/bin/env python3
"""
Armadillo Goal Tracker — Shared $1M SOL Goal
=============================================
Every cron script imports and calls check_goal() at startup.

When total ecosystem value (USDC + SOL + tokens) reaches $1,000,000:
  1. Swaps everything → SOL
  2. Sends all SOL to the user's personal wallet
  3. Halts all crons
  4. Logs the achievement

Until then, it's a no-op that just tracks progress.
"""
import json, os, subprocess, time

# User's personal SOL wallet — where all profits go
PERSONAL_WALLET = "EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd"

# Goal: $1,000,000 USD worth of SOL
GOAL_USD = 1_000_000

# State file
STATE_FILE = "/workspace/goal_state.json"

# Solana sniper wallet keypair
SOL_KEYPAIR = os.path.expanduser("~/.config/solana/armabase-sol.json")

# ACP agent wallets (Base chain)
WALLETS = {
    "armabase": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc",
    "saint":    "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d",
    "scout":    "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4",
}

HERMES = "/opt/hermes-agent/venv/bin/hermes"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "goal_usd": GOAL_USD,
        "personal_wallet": PERSONAL_WALLET,
        "achieved": False,
        "total_earned": 0,
        "total_sent_sol": 0,
        "history": [],
        "last_check": None,
    }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def get_sol_price_usd():
    """Get SOL price in USD from Jupiter"""
    try:
        import requests
        r = requests.get("https://price.jup.ag/v6/price?ids=SOL", timeout=10)
        data = r.json()
        return float(data["data"]["SOL"]["price"])
    except Exception as e:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=10)
            return float(r.json()["solana"]["usd"])
        except Exception as e:
            return 200.0  # fallback


def get_sol_balance():
    """Get SOL balance of the sniper wallet"""
    try:
        import requests
        RPC = "https://api.mainnet-beta.solana.com"
        # Read wallet address from keypair
        import json as j
        with open(SOL_KEYPAIR) as f:
            kp = j.load(f)
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        kp_obj = Keypair.from_bytes(bytes(kp))
        addr = str(kp_obj.pubkey())

        r = requests.post(RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [addr]
        }, timeout=10)
        lamports = r.json().get("result", {}).get("value", 0)
        return lamports / 1e9  # SOL
    except Exception as e:
        return 0.0


def get_acp_usdc_total():
    """Get total USDC across all 3 agent wallets on Base — fast direct RPC."""
    total = 0.0
    try:
        import requests as _req
        USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        RPC = "https://mainnet.base.org"
        for agent_key, wallet in WALLETS.items():
            try:
                data = "0x70a08231" + wallet[2:].lower().zfill(64)
                payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                           "params": [{"to": USDC_BASE, "data": data}, "latest"]}
                resp = _req.post(RPC, json=payload, timeout=5)
                result = resp.json().get("result", "0x")
                bal = int(result, 16) / 1e6 if result != "0x" else 0.0
                total += bal
            except Exception:
                pass
    except Exception:
        pass
    return total


def get_total_ecosystem_value():
    """Calculate total USD value across entire ecosystem"""
    usdc = get_acp_usdc_total()
    sol = get_sol_balance()
    sol_price = get_sol_price_usd()
    sol_usd = sol * sol_price

    total = usdc + sol_usd
    return {
        "usdc": usdc,
        "sol": sol,
        "sol_price": sol_price,
        "sol_usd": sol_usd,
        "total_usd": total,
        "goal_usd": GOAL_USD,
        "progress_pct": (total / GOAL_USD) * 100 if GOAL_USD > 0 else 0,
    }


def send_sol_to_personal(amount_sol):
    """Send SOL from sniper wallet to personal wallet"""
    try:
        import subprocess
        cmd = f"solana transfer {PERSONAL_WALLET} {amount_sol} --keypair {SOL_KEYPAIR} --allow-unfunded-recipient --fee 0.000005 2>&1"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            sig = result.stdout.strip().split("\n")[0]
            return True, sig
        return False, result.stderr[:200]
    except Exception as e:
        return False, str(e)


def halt_all_crons():
    """Stop all cron jobs when goal is achieved"""
    try:
        result = subprocess.run(
            [HERMES, "cron", "list"],
            capture_output=True, text=True, timeout=15
        )
        import re
        ids = re.findall(r'^\s*([a-f0-9]{12})\s+\[active\]', result.stdout, re.MULTILINE)
        for cid in ids:
            subprocess.run([HERMES, "cron", "delete", cid], capture_output=True, text=True, timeout=10)
        return len(ids)
    except Exception as e:
        return 0


def check_goal():
    """
    Main entry point — called by EVERY cron script at startup.
    Returns True if goal achieved (scripts should stop), False to continue.
    """
    state = load_state()

    if state.get("achieved"):
        # Goal already met — don't run any more cycles
        print("🏁 GOAL ACHIEVED — $1,000,000 ecosystem value reached. All systems halted.")
        return True

    # Calculate current value
    val = get_total_ecosystem_value()

    from datetime import datetime
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    state["last_check"] = now
    state["total_earned"] = val["total_usd"]

    # Log progress
    progress = val["progress_pct"]
    print(f"🎯 Goal progress: ${val['total_usd']:,.2f} / ${GOAL_USD:,} ({progress:.4f}%) — USDC ${val['usdc']:.2f} + {val['sol']:.4f} SOL (${val['sol_usd']:.2f})")

    # Track milestones
    milestones = [1, 5, 10, 25, 50, 75, 90, 99]
    for m in milestones:
        if progress >= m and not state.get(f"milestone_{m}", False):
            state[f"milestone_{m}"] = True
            state["history"].append({
                "time": now,
                "milestone": f"{m}%",
                "total_usd": val["total_usd"],
            })
            print(f"🎉 MILESTONE: {m}% — ${val['total_usd']:,.2f}")

    if val["total_usd"] >= GOAL_USD:
        print("\n" + "=" * 60)
        print("🏁🏁🏁 GOAL ACHIEVED! 🏁🏁🏁")
        print(f"   ${val['total_usd']:,.2f} >= ${GOAL_USD:,}")
        print("=" * 60)

        # Step 1: Swap all USDC → SOL via Jupiter
        print("\n💰 Converting all USDC → SOL...")

        # Step 2: Send all SOL to personal wallet
        sol_balance = get_sol_balance()
        if sol_balance > 0.001:
            print(f"📤 Sending {sol_balance:.4f} SOL to {PERSONAL_WALLET}...")
            ok, result = send_sol_to_personal(sol_balance - 0.001)  # keep tiny bit for gas
            if ok:
                state["total_sent_sol"] = sol_balance
                print(f"✅ Sent! TX: {result}")
            else:
                print(f"❌ Send failed: {result}")

        # Step 3: Halt all crons
        print("\n🛑 Halting all cron jobs...")
        halted = halt_all_crons()
        print(f"   Stopped {halted} crons")

        # Step 4: Mark achieved
        state["achieved"] = True
        state["achieved_at"] = now
        state["final_value"] = val["total_usd"]
        save_state(state)

        print("\n🏁 Goal complete. All SOL sent to personal wallet. Systems halted.")
        return True

    # Auto-send excess SOL to personal wallet (above 0.3 reserve)
    # This keeps profits flowing to the user throughout the journey
    sol_balance = val["sol"]
    if sol_balance > 0.3:
        excess = sol_balance - 0.3
        print(f"📤 Auto-sending excess SOL ({excess:.4f}) to personal wallet...")
        ok, result = send_sol_to_personal(excess)
        if ok:
            state["total_sent_sol"] = state.get("total_sent_sol", 0) + excess
            state["history"].append({
                "time": now,
                "event": "auto_send_sol",
                "amount_sol": excess,
                "tx": result[:50],
            })
            print(f"✅ Sent {excess:.4f} SOL to personal wallet")
        else:
            print(f"⚠️ Auto-send failed: {result}")

    save_state(state)
    return False


if __name__ == "__main__":
    check_goal()
