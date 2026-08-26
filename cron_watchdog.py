#!/usr/bin/env python3
"""
Cron Watchdog — Self-Generating Auto-Reload
============================================
Checks if all 10 cron jobs exist. Recreates any missing ones — INCLUDING ITSELF.

Self-generation chain:
1. If cron-watchdog survives a restart → it rebuilds all 10
2. If cron-watchdog is wiped but any OTHER job survives → that job's
   embedded bootstrap calls cron_watchdog.ensure_all_crons() which
   rebuilds everything including cron-watchdog
3. If ALL jobs are wiped → user says "reload crons" and we rebuild

Every script in the ecosystem imports and calls ensure_all_crons() at
the top of its main block. This creates a mesh — any survivor heals all.

Run by cron every 3 minutes.
"""
import subprocess, sys, os, re

PATH = "/opt/hermes-agent/venv/bin:/opt/hermes-agent:" + os.environ.get("PATH", "")
os.environ["PATH"] = PATH

HERMES = "/opt/hermes-agent/venv/bin/hermes"
WORKDIR = "/workspace"
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")

# All 22 active cron jobs: (schedule, name, script)
# Synced with actual Hermes cron list — no paused entries.
ALL_JOBS = [
    ("every 15m", "cron-watchdog",     "cron_watchdog.py"),
    ("every 5m",  "deployer-watcher",  "deployer_watcher.py"),
    ("every 30m", "oversight",         "oversight.py"),
    ("every 10m", "sniper-guard",      "sniper_guard.py"),
    ("every 10m", "compute-autotopup", "compute_autotopup.py"),
    ("every 30m", "saint-perps",       "saint_perps.py"),
    ("every 60m", "learning-engine",   "learning_engine.py"),
    ("every 15m", "treasury-engine",   "treasury_engine.py"),
    ("every 30m", "volume-engine",     "volume_engine.py"),
    ("every 15m", "git-autosync",      "git_autosync.py"),
    ("every 30m", "profit-engine",     "profit_engine.py"),
    ("every 45m", "scout-ecosystem",   "scout_ecosystem.py"),
    ("every 30m", "revenue-engine",    "revenue_engine.py"),
    ("every 120m","growth-engine",     "growth_engine.py"),
    ("every 15m", "team-coordinator",  "team_coordinator.py"),
    ("every 20m", "pumpfun-loop",      "pumpfun_multi_loop.py"),
    ("every 20m", "arb-scanner",       "arb_scanner.py"),
    ("every 30m", "hl-spot-trader",    "hl_spot_trader.py"),
    ("every 60m", "x-poster",          "x_poster.py"),
    ("every 10m", "sol-distributor",   "sol_distributor.py"),
    ("every 60m", "token-utility",     "token_utility_engine.py"),
    ("every 30m", "test-suite",        "test_suite.py"),
]


def sync_scripts():
    """Copy all .py scripts from /workspace to ~/.hermes/scripts/.
    Hermes blocks symlinks and scripts outside the scripts dir — must copy real files."""
    import shutil
    try:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        for sched, name, script in ALL_JOBS:
            src = os.path.join(WORKDIR, script)
            dst = os.path.join(SCRIPTS_DIR, script)
            if os.path.exists(src):
                shutil.copy2(src, dst)
        # Also copy goal_tracker.py and any other imported scripts
        for extra in ["goal_tracker.py", "cron_watchdog.py"]:
            src = os.path.join(WORKDIR, extra)
            dst = os.path.join(SCRIPTS_DIR, extra)
            if os.path.exists(src):
                shutil.copy2(src, dst)
        return True
    except Exception:
        return False


def get_existing_jobs():
    """Return set of job names currently registered in Hermes cron"""
    try:
        result = subprocess.run(
            [HERMES, "cron", "list"],
            capture_output=True, text=True, timeout=15
        )
        existing = set()
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Name:"):
                name = line.split("Name:")[1].strip()
                existing.add(name)
        return existing
    except Exception:
        return set()


def create_job(schedule, name, script):
    """Create a single cron job. Returns True on success."""
    try:
        cmd = [
            HERMES, "cron", "create", schedule,
            "--name", name,
            "--no-agent",
            "--script", script,
            "--workdir", WORKDIR,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False


def ensure_all_crons():
    """
    Public API — called by EVERY script in the ecosystem at startup.
    Syncs scripts to ~/.hermes/scripts/, checks if all 13 crons exist,
    recreates any missing ones (including watchdog).
    Returns the set of jobs that were recreated.
    """
    sync_scripts()
    existing = get_existing_jobs()
    needed = {name for _, name, _ in ALL_JOBS}
    missing = needed - existing

    if not missing:
        return set()

    recreated = set()
    for schedule, name, script in ALL_JOBS:
        if name in missing:
            ok = create_job(schedule, name, script)
            if ok:
                recreated.add(name)

    return recreated


def check_capital_and_resume():
    """Check if ArmaBase (master) has USDC on Base. If so:
    1. Distribute to other agents as needed
    2. Resume paused trading crons
    ArmaBase is the master distributor — all funding goes to it, it splits to others.
    """
    import json as _json
    import os as _os

    # ArmaBase is master — receives all user funding, distributes to others
    MASTER_WALLET = "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"  # ArmaBase
    # Sub-agents that need funding for their specific roles
    SUB_AGENTS = [
        {"name": "Saint-ARMAD",   "evm": "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d", "min_usdc": 5.0,  "role": "HL perps trading"},
        {"name": "Scout",         "evm": "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4", "min_usdc": 2.0,  "role": "ecosystem jobs + swaps"},
        {"name": "Saint-OGSAINT", "evm": "0x52a140c6dab119a6a050f857591bbf469c1856ce", "min_usdc": 2.0,  "role": "OGSAINT token ops"},
    ]
    ALL_WALLETS = [MASTER_WALLET] + [a["evm"] for a in SUB_AGENTS]

    config_path = _os.path.expanduser("~/.config/acp/config.json")
    env = _os.environ.copy()
    env["TS_KEYRING_BACKEND"] = "file"

    def get_usdc(wallet):
        """Get USDC balance on Base for a wallet — direct RPC, no ACP CLI."""
        try:
            import requests as _req
            USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
            # balanceOf(address) selector = 0x70a08231
            data = "0x70a08231" + wallet[2:].lower().zfill(64)
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_call",
                "params": [{"to": USDC_BASE, "data": data}, "latest"]
            }
            resp = _req.post("https://mainnet.base.org", json=payload, timeout=15)
            result = resp.json().get("result", "0x")
            if result == "0x":
                return 0.0
            return int(result, 16) / 1e6
        except Exception:
            pass
        return 0.0

    def send_usdc(from_wallet, to_wallet, amount):
        """Send USDC on Base from one agent to another via acp wallet send-transaction."""
        try:
            # Properly switch active agent using agent use
            # Find the agent ID for this wallet
            with open(config_path) as f:
                cfg = _json.load(f)
            agents_data = cfg.get("agents", {})
            if isinstance(agents_data, dict):
                # agents keyed by wallet address
                agent_id = agents_data.get(from_wallet.lower(), {}).get("id", "")
                if not agent_id:
                    # try case-insensitive match
                    for w, a in agents_data.items():
                        if w.lower() == from_wallet.lower() and isinstance(a, dict):
                            agent_id = a.get("id", "")
                            break
                if agent_id:
                    subprocess.run(
                        ["acp", "agent", "use", "--agent-id", agent_id, "--json"],
                        capture_output=True, text=True, timeout=15, env=env
                    )
            elif isinstance(agents_data, list):
                for a in agents_data:
                    if isinstance(a, dict) and a.get("walletAddress", "").lower() == from_wallet.lower():
                        agent_id = a.get("id", "")
                        if agent_id:
                            subprocess.run(
                                ["acp", "agent", "use", "--agent-id", agent_id, "--json"],
                                capture_output=True, text=True, timeout=15, env=env
                            )
                        break
            cfg["activeWallet"] = from_wallet
            with open(config_path, 'w') as f:
                _json.dump(cfg, f, indent=2)

            # Encode USDC transfer: transfer(address,uint256)
            # selector = 0xa9059cbb
            USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
            amount_raw = int(round(amount, 2) * 1e6)  # USDC has 6 decimals
            # Pad recipient address to 32 bytes and amount to 32 bytes
            recipient_padded = to_wallet[2:].lower().zfill(64)
            amount_hex = format(amount_raw, '064x')
            call_data = "0xa9059cbb" + recipient_padded + amount_hex

            r = subprocess.run(
                ["acp", "wallet", "send-transaction",
                 "--chain-id", "8453",
                 "--to", USDC_BASE,
                 "--data", call_data, "--json"],
                capture_output=True, text=True, timeout=60, env=env
            )
            raw = r.stdout.strip()
            clean = raw.split('\n[acp-wrapper]')[0].strip()
            try:
                data = _json.loads(clean)
                if "transactionHash" in data:
                    return True
                if "error" in data:
                    print(f"     ⚠️ send-transaction error: {data.get('error','')[:80]}")
                    return False
            except Exception:
                pass
            return r.returncode == 0
        except Exception as e:
            print(f"     ⚠️ send_usdc exception: {e}")
            return False

    # 1. Check master (ArmaBase) balance
    master_usdc = get_usdc(MASTER_WALLET)

    # 2. Check all sub-agent balances
    total_usdc = master_usdc
    sub_balances = {}
    for sub in SUB_AGENTS:
        bal = get_usdc(sub["evm"])
        sub_balances[sub["name"]] = bal
        total_usdc += bal

    print(f"  📊 Capital: ArmaBase ${master_usdc:.2f} | " +
          " | ".join(f"{s['name']} ${sub_balances[s['name']]:.2f}" for s in SUB_AGENTS) +
          f" | Total ${total_usdc:.2f}")

    # 3. Distribute from ArmaBase to sub-agents that need it
    distributions = []
    if master_usdc >= 5.0:  # Only distribute if master has enough
        for sub in SUB_AGENTS:
            current = sub_balances[sub["name"]]
            needed = sub["min_usdc"]
            if current < needed:
                # Send enough to bring sub-agent up to its minimum
                send_amount = min(needed - current + 1, master_usdc - 3)  # Keep $3 reserve on master
                if send_amount >= 2.0:  # ACP minimum
                    print(f"  💸 Distributing ${send_amount:.2f} → {sub['name']} ({sub['role']})")
                    ok = send_usdc(MASTER_WALLET, sub["evm"], send_amount)
                    if ok:
                        master_usdc -= send_amount
                        distributions.append(sub["name"])
                        print(f"     ✅ Sent to {sub['name']}")
                    else:
                        print(f"     ❌ Transfer failed")

    # 4. All crons are now permanently active — no pause/resume logic needed.
    # The self-sustaining loop keeps everything running 24/7.
    resumed = []

    if distributions:
        print(f"📤 Distributed to: {', '.join(distributions)}")

    return total_usdc, resumed


def run():
    from datetime import datetime
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    existing = get_existing_jobs()
    needed = {name for _, name, _ in ALL_JOBS}
    missing = needed - existing

    if not missing:
        print(f"✅ All {len(needed)} cron jobs present — nothing to do [{now}]")
    else:
        print(f"⚠️ Missing {len(missing)} jobs: {', '.join(sorted(missing))} [{now}]")
        print(f"   Self-generating...")
        recreated = ensure_all_crons()
        if recreated:
            print(f"   ✅ Recreated: {', '.join(sorted(recreated))}")
        else:
            print(f"   ❌ Failed to recreate any jobs")
        final = get_existing_jobs()
        still_missing = needed - final
        if still_missing:
            print(f"   ⚠️ Still missing: {', '.join(sorted(still_missing))}")
        else:
            print(f"   ✅ All {len(needed)} jobs now active")

    # Capital distribution: move USDC from ArmaBase to sub-agents as needed
    try:
        usdc, resumed = check_capital_and_resume()
        if usdc < 3.0:
            print(f"💤 Low capital: ${usdc:.2f} USDC across ecosystem — agents running on fumes")
    except Exception as e:
        print(f"Capital check skipped: {e}")


if __name__ == "__main__":
    # Check $1M goal — halt everything if achieved
    try:
        from goal_tracker import check_goal
        if check_goal():
            sys.exit(0)
    except Exception as e:
        print(f"Goal check skipped: {e}")
    run()
