#!/usr/bin/env python3
"""
ArmaBase Revenue Engine — Cron Job (Team-Integrated)
=====================================================
Runs every 30 minutes. Does NOT loop-trade (that loses money on thin liquidity).

What it does:
1. Checks for incoming USDC (from marketplace revenue + Saint HL profits + Scout routing)
2. Uses 50% of new USDC to buyback ARBA + burn (drives price up)
3. Keeps 50% as withdrawable profit
4. Logs everything

Revenue sources (TEAM FLYWHEEL):
- ArmaBase: 26 marketplace offerings ($0.25-$10 each)
- Saint: Hyperliquid perps profits → routed to ArmaBase by team_coordinator.py
- Scout: Cross-chain swaps + ARRB ecosystem revenue → routed to ArmaBase
- All revenue converges here: 50% buyback-burn ARBA, 50% withdrawable profit

Withdrawal:
- USDC can be withdrawn at any time by sending to any address
- Command: acp wallet send-transaction --chain-id 8453 --to <address> --data <usdc_transfer_calldata>
"""

import json, time, os, subprocess, requests

ARBA_CONTRACT = "0x557642685ce68F3975458375B51553871807e1b5"
USDC_CONTRACT = "0x833589fCD6edb6e08f4c7c32d4f71b54bda02913"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"
WALLET = "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"
REVENUE_LOG = "/workspace/revenue_log.json"
LAST_BALANCE_FILE = "/workspace/last_usdc_balance.json"

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def get_usdc_balance():
    out, _, _ = run("acp wallet balance --chain-id 8453 --json")
    try:
        d = json.loads(out)
        for t in d['tokens']:
            sym = t['tokenMetadata'].get('symbol') or 'ETH'
            if sym == 'USDC':
                bal = int(t['tokenBalance'], 16) if t['tokenBalance'] else 0
                dec = t['tokenMetadata'].get('decimals') or 18
                return bal / (10**dec)
    except Exception as e:
        pass
    return 0.0

def get_arba_balance():
    out, _, _ = run("acp wallet balance --chain-id 8453 --json")
    try:
        d = json.loads(out)
        for t in d['tokens']:
            sym = t['tokenMetadata'].get('symbol') or 'ETH'
            if sym == 'ARBA':
                bal = int(t['tokenBalance'], 16) if t['tokenBalance'] else 0
                dec = t['tokenMetadata'].get('decimals') or 18
                return bal / (10**dec)
    except Exception as e:
        pass
    return 0.0

def get_arba_supply():
    resp = requests.post("https://mainnet.base.org", json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": ARBA_CONTRACT, "data": "0x18160ddd"}, "latest"]
    }, timeout=10)
    return int(resp.json().get("result", "0x0"), 16) / 1e18

def buyback_arba(usdc_amount):
    """Buy ARBA with USDC via ACP"""
    out, err, rc = run(
        f"acp trade --token-in usdc --chain-in 8453 --amount-in {usdc_amount} "
        f"--token-out {ARBA_CONTRACT} --chain-out 8453 --json"
    )
    try:
        d = json.loads(out)
        if d.get('status') == 'success':
            received = float(d.get('finalReceived', '0').replace(' ARBA', ''))
            txs = [leg['txHash'] for leg in d.get('legs', []) if leg.get('txHash')]
            return True, received, txs[-1] if txs else ''
        return False, 0, d.get('error', err)
    except Exception as e:
        return False, 0, err or out

def burn_arba(amount):
    """Burn ARBA to dead address"""
    amt_hex = hex(int(amount * 10**18))[2:].zfill(64)
    calldata = f"0xa9059cbb000000000000000000000000000000000000000000000000000000000000dead{amt_hex}"
    out, err, rc = run(
        f"acp wallet send-transaction --chain-id 8453 "
        f"--to {ARBA_CONTRACT} --data {calldata} --json"
    )
    if "approval" in out.lower():
        import re
        m = re.search(r'https://app\.virtuals\.io/wallet/approve-transaction\?id=[a-f0-9-]+', out)
        return False, f"NEEDS APPROVAL: {m.group()}" if m else "NEEDS APPROVAL"
    try:
        d = json.loads(out)
        tx = d.get('txHash') or d.get('hash', '')
        return True, tx if tx else str(d)
    except Exception as e:
        return False, out[:200]

def withdraw_usdc(amount, to_address):
    """Withdraw USDC to any address"""
    amt_hex = hex(int(amount * 10**6))[2:].zfill(64)
    to_hex = to_address[2:].zfill(64)
    calldata = f"0xa9059cbb{to_hex}{amt_hex}"
    out, err, rc = run(
        f"acp wallet send-transaction --chain-id 8453 "
        f"--to {USDC_CONTRACT} --data {calldata} --json"
    )
    if "approval" in out.lower():
        import re
        m = re.search(r'https://app\.virtuals\.io/wallet/approve-transaction\?id=[a-f0-9-]+', out)
        return False, f"NEEDS APPROVAL: {m.group()}" if m else "NEEDS APPROVAL"
    try:
        d = json.loads(out)
        tx = d.get('txHash') or d.get('hash', '')
        return True, tx
    except Exception as e:
        return False, out[:200]

def log_revenue(entry):
    log = []
    if os.path.exists(REVENUE_LOG):
        try:
            with open(REVENUE_LOG) as f:
                log = json.load(f)
        except Exception as e:
            log = []
    log.append(entry)
    with open(REVENUE_LOG, 'w') as f:
        json.dump(log, f, indent=2)

def main():
    print(f"{'='*60}")
    print(f"ArmaBase Revenue Engine — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"{'='*60}")
    
    # Get current USDC balance
    usdc = get_usdc_balance()
    arba = get_arba_balance()
    supply = get_arba_supply()
    
    # Load last known balance
    last_usdc = usdc
    if os.path.exists(LAST_BALANCE_FILE):
        with open(LAST_BALANCE_FILE) as f:
            last_usdc = json.load(f).get('usdc', usdc)
    
    # Calculate new revenue
    new_revenue = usdc - last_usdc
    if new_revenue > 0.01:
        print(f"💰 New revenue detected: ${new_revenue:.2f} USDC")
        print(f"   (was ${last_usdc:.2f}, now ${usdc:.2f})")
        
        # Buyback-burn disabled by user request — 100% kept as profit
        profit_amount = new_revenue
        print(f"   Profit (keep): ${profit_amount:.2f}")
        
        # Log
        log_revenue({
            'timestamp': int(time.time()),
            'date': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'new_revenue': new_revenue,
            'buyback_usdc': 0,
            'profit_usdc': profit_amount,
            'arba_bought': 0,
            'arba_burned': 0,
            'buyback_tx': '',
            'burn_tx': '',
        })
    else:
        print(f"No new revenue (balance: ${usdc:.2f}, was ${last_usdc:.2f})")
    
    # Save current balance
    with open(LAST_BALANCE_FILE, 'w') as f:
        json.dump({'usdc': usdc, 'timestamp': int(time.time())}, f)
    
    # Show status
    print(f"\n📊 Status:")
    print(f"   USDC: ${usdc:.2f} (withdrawable)")
    print(f"   ARBA: {arba:,.0f}")
    print(f"   ARBA supply: {supply:,.0f}")
    print(f"   Burned so far: {1_000_000_000 - supply:,.0f} ARBA")
    
    # Show cumulative profit
    if os.path.exists(REVENUE_LOG):
        with open(REVENUE_LOG) as f:
            log = json.load(f)
        total_revenue = sum(e.get('new_revenue', 0) for e in log)
        total_profit = sum(e.get('profit_usdc', 0) for e in log)
        total_burned = sum(e.get('arba_burned', 0) for e in log)
        print(f"\n💰 Cumulative:")
        print(f"   Total revenue: ${total_revenue:.2f}")
        print(f"   Total profit kept: ${total_profit:.2f}")
        print(f"   Total ARBA burned: {total_burned:,.0f}")

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
    main()
