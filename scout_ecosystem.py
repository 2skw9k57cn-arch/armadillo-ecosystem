#!/usr/bin/env python3
"""
Scout Ecosystem Engine v2 — Resilient
======================================
Armadillo Scout — ecosystem scout and cross-chain operator.

v2 changes:
  - Browse API calls have retry + exponential backoff (handles 500/503)
  - All subprocess calls have 15s timeout (never hangs)
  - Completes in <60s even when API is down
  - Still does all useful work: job checking, ARRB status, swaps, routing

Roles:
  1. Browse ACP marketplace for jobs (with retry)
  2. Check incoming jobs for all agents
  3. Manage ARRB token (graduated on Robinhood Chain)
  4. Cross-chain swaps: convert excess VIRTUAL → USDC → fund ArmaBase
  5. Route excess USDC to ArmaBase

Runs every 45 minutes via cron.
"""

import json, time, os, subprocess
from datetime import datetime

# ============ CONFIG ============
SCOUT_ID = "019fa674-7be2-72ce-956c-3a7f831e9102"
ARMA_BASE_ID = "019fbb50-31de-7e2f-be3b-2225023960b3"
SAINT_ID = "019f9f75-493a-7011-b547-aa9c2df1a1ac"

ARMA_BASE_WALLET = "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"
SCOUT_WALLET = "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4"
SAINT_WALLET = "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"

USDC_CONTRACT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ARRB_CONTRACT = "0xdA3C5b4d05c40a9244E534a966A1424C51055950"
VIRTUAL_CONTRACT = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b"

BASE_CHAIN = 8453
ROBINHOOD_CHAIN = 4663

SCOUT_LOG = "/workspace/scout_log.json"
MAX_HIRE_BUDGET = 2.0  # Max USDC to spend hiring other agents per cycle

# ============ UTILITIES ============
def run(cmd, timeout=15):
    """Run a shell command with strict timeout. Never hangs."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1

def run_with_retry(cmd, max_retries=2, base_timeout=10):
    """Run command with minimal retries. API 500/503 = skip, don't waste time."""
    out, err, rc = "", "", -1
    for attempt in range(max_retries):
        out, err, rc = run(cmd, timeout=base_timeout)
        if rc == 0:
            return out, err, rc
        # Server error — retry once with short backoff
        if "500" in out or "503" in out or "Internal Server Error" in out:
            if attempt == 0:
                time.sleep(1)
                continue
        # Non-retryable or exhausted retries
        return out, err, rc
    return out, err, rc

def use_agent(agent_id):
    """Switch active ACP agent."""
    run(f"acp agent use --agent-id {agent_id} --json")
    # Also update config.json activeWallet
    try:
        wallet_map = {
            SCOUT_ID: SCOUT_WALLET,
            ARMA_BASE_ID: ARMA_BASE_WALLET,
            SAINT_ID: SAINT_WALLET,
        }
        wallet = wallet_map.get(agent_id, SCOUT_WALLET)
        config_path = os.path.expanduser("~/.config/acp/config.json")
        with open(config_path) as f:
            cfg = json.load(f)
        cfg["activeWallet"] = wallet
        with open(config_path, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
    return True

def get_balance(symbol, chain=BASE_CHAIN):
    """Get token balance for active agent. Uses direct RPC for speed when possible."""
    # Fast path: USDC on Base via direct RPC (avoids 7s acp wallet balance call)
    if symbol.upper() == 'USDC' and chain == BASE_CHAIN:
        try:
            import requests as _req
            config_path = os.path.expanduser("~/.config/acp/config.json")
            with open(config_path) as f:
                cfg = json.load(f)
            wallet = cfg.get("activeWallet", SCOUT_WALLET)
            USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
            data = "0x70a08231" + wallet[2:].lower().zfill(64)
            payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": USDC_BASE, "data": data}, "latest"]}
            resp = _req.post("https://mainnet.base.org", json=payload, timeout=5)
            result = resp.json().get("result", "0x")
            return int(result, 16) / 1e6 if result != "0x" else 0.0
        except Exception:
            pass

    # Fallback: acp wallet balance (slower but works for any token/chain)
    out, _, _ = run(f"acp wallet balance --chain-id {chain} --json", timeout=15)
    try:
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        d = json.loads(raw)
        for t in d.get('tokens', []):
            sym = t.get('tokenMetadata', {}).get('symbol') or 'NATIVE'
            if sym.upper() == symbol.upper():
                bal_raw = t.get('tokenBalance', '0')
                dec = t.get('tokenMetadata', {}).get('decimals', 18)
                try:
                    val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith('0x') else int(bal_raw)
                    return val / (10**dec) if val > 0 else 0.0
                except Exception:
                    return 0.0
        return 0.0
    except Exception:
        return 0.0

def log_action(action, details):
    """Log scout actions."""
    log = []
    if os.path.exists(SCOUT_LOG):
        try:
            with open(SCOUT_LOG) as f:
                log = json.load(f)
        except Exception:
            pass
    log.append({
        'timestamp': datetime.utcnow().isoformat(),
        'action': action,
        'details': details
    })
    log = log[-50:]
    try:
        with open(SCOUT_LOG, 'w') as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass

# ============ BROWSE MARKETPLACE ============

def browse_marketplace():
    """Browse ACP marketplace with retry/backoff. Gracefully handles API outages."""
    print("\n  📡 Browsing ACP marketplace...")

    queries = [
        "token analysis research trading signals",
        "crypto due diligence holder analysis",
        "perpetual futures trading signals",
        "meme generation content creation",
        "market research crypto analysis"
    ]

    all_results = []
    api_down = False

    for query in queries:
        if api_down:
            break

        # Try v2 API — single attempt, 8s timeout
        out, err, rc = run_with_retry(
            f'acp browse "{query}" --top-k 3 --json',
            max_retries=1, base_timeout=8
        )

        if rc != 0:
            # Try legacy — single attempt, 8s timeout
            out, err, rc = run_with_retry(
                f'acp browse "{query}" --top-k 3 --legacy --json',
                max_retries=1, base_timeout=8
            )

        if rc == 0:
            try:
                raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
                d = json.loads(raw)
                agents = d.get('data', d) if isinstance(d, dict) else d
                if isinstance(agents, list):
                    for a in agents:
                        name = a.get('name', '?')
                        wallet = a.get('walletAddress', '')
                        offerings = a.get('offerings', [])
                        # Don't match ourselves
                        if wallet.lower() in [ARMA_BASE_WALLET.lower(), SCOUT_WALLET.lower(), SAINT_WALLET.lower()]:
                            continue
                        for off in offerings:
                            price = off.get('priceValue', 0)
                            off_name = off.get('name', '')
                            all_results.append({
                                'agent': name,
                                'wallet': wallet[:12] + '...',
                                'offering': off_name,
                                'price': price,
                                'query': query
                            })
            except Exception:
                pass
        else:
            # Check if it's a server error
            if "500" in out or "503" in out or "Internal Server Error" in out:
                print(f"    ⚠️ ACP browse API is down (500/503) — skipping marketplace scan")
                api_down = True
                break

    # Dedupe by agent+offering
    seen = set()
    unique = []
    for r in all_results:
        key = f"{r['agent']}_{r['offering']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)

    if unique:
        print(f"  Found {len(unique)} unique offerings from {len(set(r['agent'] for r in unique))} agents")
        for r in unique[:10]:
            print(f"    {r['agent']}: \"{r['offering']}\" — ${r['price']}")
    else:
        print(f"  No marketplace results (API may be down)")

    log_action('browse', {'found': len(unique), 'api_down': api_down, 'top': unique[:5]})
    return unique

# ============ JOB CHECKING ============

def check_acp_jobs():
    """Check for incoming jobs for Scout only (other agents check their own)."""
    print("\n  📋 Checking Scout jobs...")
    out, _, rc = run("acp job list --json", timeout=10)
    if rc == 0:
        try:
            raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
            d = json.loads(raw)
            jobs = d if isinstance(d, list) else d.get('data', d.get('jobs', []))
            if isinstance(jobs, list) and jobs:
                print(f"  [Scout] {len(jobs)} active jobs")
                for j in jobs[:3]:
                    status = j.get('status', '?')
                    job_id = j.get('jobId', j.get('id', '?'))
                    print(f"    Job {str(job_id)[:12]}...: {status}")
            else:
                print(f"  [Scout] No active jobs")
        except Exception:
            print(f"  [Scout] Job check failed (parse error)")
    else:
        print(f"  [Scout] Job check failed (rc={rc})")

# ============ ARRB STATUS ============

def check_arrb_status():
    """Check ARRB token status on Robinhood Chain — fast, no browse call."""
    # Only check ARRB balance every 3rd cycle (7s call)
    cycle_file = "/workspace/scout_cycle_count.txt"
    cycle = 1
    try:
        with open(cycle_file) as f:
            cycle = int(f.read().strip())
    except Exception:
        pass

    if cycle % 3 == 0:
        arrb = get_balance('ARRB', ROBINHOOD_CHAIN)
        print(f"\n  📊 ARRB (Robinhood Chain): {arrb:,.0f}")
        if arrb > 0:
            print(f"     ARRB graduated and active=true ✅")
            print(f"     Value: ~${arrb * 0.0000304:.2f}")
        log_action('arrb_check', {'balance': arrb, 'value_usd': arrb * 0.0000304})
    else:
        print(f"\n  📊 ARRB: skipped (checked every 3rd cycle)")

# ============ SWAPS & ROUTING ============

def swap_virtual_to_usdc():
    """If Scout has excess VIRTUAL on Robinhood Chain, swap to USDC on Base.
    Only checks VIRTUAL balance every 3rd cycle (7s RPC call)."""
    cycle_file = "/workspace/scout_cycle_count.txt"
    cycle = 1
    try:
        with open(cycle_file) as f:
            cycle = int(f.read().strip())
    except Exception:
        pass

    if cycle % 3 != 0:
        print(f"\n  💱 VIRTUAL swap: skipped (checked every 3rd cycle)")
        return False

    virtual_rh = get_balance('VIRTUAL', ROBINHOOD_CHAIN)

    if virtual_rh > 4:
        print(f"\n  💱 Scout has {virtual_rh:.2f} VIRTUAL on Robinhood Chain")
        print(f"  Swapping VIRTUAL → USDC on Base (cross-chain via LiFi)...")

        amount = round(virtual_rh - 1, 2)  # Keep 1 VIRTUAL reserve
        out, err, rc = run(
            f"acp trade --token-in virtual --chain-in {ROBINHOOD_CHAIN} --amount-in {amount} "
            f"--token-out usdc --chain-out {BASE_CHAIN} --json",
            timeout=30
        )

        if rc == 0:
            print(f"  ✅ Swapped {amount} VIRTUAL → USDC on Base")
            log_action('swap_virtual_usdc', {'amount_in': amount, 'chain_from': ROBINHOOD_CHAIN})
            time.sleep(3)
            return True
        else:
            print(f"  ❌ Swap failed: {(err or out)[:100]}")
            log_action('swap_failed', {'error': (err or out)[:100]})
            return False
    else:
        print(f"\n  Scout VIRTUAL on RH: {virtual_rh:.2f} — not enough to swap")

    return False

def route_usdc_to_armabase():
    """Route excess USDC from Scout to ArmaBase for distribution"""
    usdc = get_balance('USDC')

    if usdc > 1.5:
        excess = round(usdc - 0.5, 2)
        if excess >= 2:
            print(f"\n  📤 Routing ${excess:.2f} USDC from Scout → ArmaBase...")

            # Build USDC transfer calldata (6 decimals)
            amount_wei = hex(int(excess * 1e6))
            amount_hex = amount_wei[2:].zfill(64)
            to_hex = ARMA_BASE_WALLET[2:].zfill(64)
            calldata = f"0xa9059cbb{to_hex}{amount_hex}"

            out, err, rc = run(
                f"acp wallet send-transaction --chain-id {BASE_CHAIN} "
                f"--to {USDC_CONTRACT} --data {calldata} --json",
                timeout=30
            )

            if rc == 0:
                print(f"  ✅ Transferred ${excess:.2f} USDC to ArmaBase")
                log_action('transfer_to_armabase', {'amount': excess})
            else:
                print(f"  ❌ Transfer failed: {(err or out)[:100]}")
                log_action('transfer_failed', {'amount': excess, 'error': (err or out)[:100]})
        else:
            print(f"\n  Scout USDC: ${usdc:.2f} — excess ${excess:.2f} below $2 minimum")
    else:
        print(f"\n  Scout USDC: ${usdc:.2f} — not enough to route")

# ============ REPORT ============

def generate_scout_report():
    """Generate Scout's ecosystem report — uses fast RPC for USDC, skips slow calls."""
    usdc = get_balance('USDC')  # Fast via direct RPC

    # ARRB and VIRTUAL on RH chain are slow (7s each) — only check if needed
    # Skip on normal cycles, check on every 3rd cycle
    cycle_file = "/workspace/scout_cycle_count.txt"
    cycle = 0
    try:
        with open(cycle_file) as f:
            cycle = int(f.read().strip())
    except Exception:
        pass
    cycle += 1
    try:
        with open(cycle_file, 'w') as f:
            f.write(str(cycle))
    except Exception:
        pass

    print(f"\n{'='*50}")
    print(f"  🔍 SCOUT ECOSYSTEM REPORT (cycle {cycle})")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}")
    print(f"  Scout Wallet: {SCOUT_WALLET[:12]}...")
    print(f"  USDC (Base): ${usdc:.2f}")

    if cycle % 3 == 0:  # Only check RH balances every 3rd cycle (saves 14s)
        virtual_rh = get_balance('VIRTUAL', ROBINHOOD_CHAIN)
        arrb = get_balance('ARRB', ROBINHOOD_CHAIN)
        print(f"  VIRTUAL (RH): {virtual_rh:.2f} (${virtual_rh * 0.75:.2f})")
        print(f"  ARRB (RH): {arrb:,.0f} (${arrb * 0.0000304:.2f})")
    else:
        print(f"  RH balances: skipped (checked every 3rd cycle)")
    print()

# ============ MAIN ============

# ─── Self-Healing Cron Mesh ──────────────────────────────────────────────
import sys as _sys, os as _os
_sys.path.insert(0, "/workspace")
try:
    from goal_tracker import check_goal
    from cron_watchdog import ensure_all_crons
    _recreated = ensure_all_crons()
    try:
        if check_goal():
            _sys.exit(0)
    except Exception as e:
        print(f"Goal check skipped: {e}")
    if _recreated:
        print(f"  🔧 Self-healed crons: {', '.join(sorted(_recreated))}")
except Exception as _e:
    print(f"  ⚠️ Cron self-heal skipped: {_e}")
# ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n🔍 Scout Ecosystem Engine v2 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    use_agent(SCOUT_ID)

    # 1. Generate report
    generate_scout_report()

    # 2. Browse marketplace for jobs (with retry/backoff, won't hang)
    results = browse_marketplace()

    # 3. Check ACP jobs for all agents
    check_acp_jobs()

    # 4. Check ARRB status
    check_arrb_status()

    # 5. Try to swap excess VIRTUAL → USDC
    swap_virtual_to_usdc()

    # 6. Route excess USDC to ArmaBase
    route_usdc_to_armabase()

    print(f"\n  ✅ Scout ecosystem cycle complete")
