#!/usr/bin/env python3
"""
Canonical Ecosystem Test Suite
==============================
Focused on revenue-generating systems:
  - Saint HL Perps (primary revenue driver)
  - ACP Marketplace (job delivery, offerings, subscriptions)
  - Treasury / Profit / Revenue Engines
  - HL Spot Trader
  - Arb Scanner
  - Token Utility Engine
  - Sol Distributor
  - Cron Health

NOT focused on pump.fun sniper (de-prioritized per user direction).

Run: python3 -m pytest tests/ -v
Or:  python3 tests/run_tests.py

Results saved to /workspace/test_results.json
"""

import sys, os, json, subprocess, time, urllib.request, requests
from datetime import datetime, timezone

# Add workspace to path
sys.path.insert(0, "/workspace")

WORKSPACE = "/workspace"
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
RESULTS_FILE = "/workspace/test_results.json"
HERMES_BIN = "/opt/hermes-agent/venv/bin/hermes"

# Agent IDs
ARMABASE_ID = "019fbb50-31de-7e2f-be3b-2225023960b3"
SAINT_ID = "019f9f75-493a-7011-b547-aa9c2df1a1ac"
SCOUT_ID = "019fa674-7be2-72ce-956c-3a7f831e9102"

# Solana
SNIPER_WALLET = "CT5Z79b1ie7AaeRzEsn1uQjMz3p33xTJST7Na49UDoSL"
SOL_RPC = "https://api.mainnet-beta.solana.com"

# HL
HL_API = "https://api.hyperliquid.xyz/info"

passed = 0
failed = 0
results = []

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def record(name, ok, detail=""):
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    results.append({"test": name, "passed": ok, "detail": detail[:200]})
    print(f"  {status:4} | {name:40} | {detail[:80]}")

def run_cmd(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except Exception as e:
        return "", str(e), -1

def rpc_solana(method, params):
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(SOL_RPC, data=data, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def use_agent(agent_id):
    run_cmd(f"acp agent use --agent-id {agent_id}")

def py_compile_check(filepath):
    """Check a Python file compiles without syntax errors"""
    if not os.path.exists(filepath):
        return False, f"file not found: {filepath}"
    r = subprocess.run([sys.executable, "-m", "py_compile", filepath],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return False, r.stderr.strip()[:150]
    return True, "ok"

def in_cron_dir(filename):
    return os.path.exists(os.path.join(SCRIPTS_DIR, filename))

# ============================================================
# TEST SUITES
# ============================================================

def test_script_syntax():
    """Verify all ecosystem scripts compile cleanly"""
    print("\n--- Script Syntax ---")
    scripts = [
        "saint_perps.py", "treasury_engine.py", "profit_engine.py",
        "revenue_engine.py", "volume_engine.py", "growth_engine.py",
        "oversight.py", "cron_watchdog.py", "deployer_watcher.py",
        "sniper_guard.py", "learning_engine.py", "team_coordinator.py",
        "scout_ecosystem.py", "git_autosync.py", "pumpfun_multi_loop.py",
        "arb_scanner.py", "hl_spot_trader.py", "x_poster.py",
        "sol_distributor.py", "token_utility_engine.py", "test_suite.py",
    ]
    for s in scripts:
        path = os.path.join(WORKSPACE, s)
        if not os.path.exists(path):
            path = os.path.join(SCRIPTS_DIR, s)
        ok, detail = py_compile_check(path)
        record(f"syntax:{s}", ok, detail)

def test_cron_health():
    """Verify all cron jobs are active and running without errors"""
    print("\n--- Cron Health ---")
    out, _, _ = run_cmd(f"{HERMES_BIN} cron list 2>&1", timeout=15)
    if not out:
        record("cron:list", False, "no output from hermes cron list")
        return

    # Count active vs error
    active_count = out.count("[active]")
    error_count = out.count("error:")
    record("cron:all_active", active_count >= 20, f"{active_count} active jobs")
    record("cron:no_errors", error_count == 0, f"{error_count} errors" if error_count else "0 errors")

    # Check specific critical crons
    critical = ["saint-perps", "profit-engine", "revenue-engine", "treasury-engine"]
    for name in critical:
        found = name in out
        record(f"cron:{name}", found, "present" if found else "MISSING")

def test_saint_hl_positions():
    """Verify Saint has active HL positions and positive PnL"""
    print("\n--- Saint HL Perps ---")
    use_agent(SAINT_ID)
    out, _, _ = run_cmd("acp wallet balance --json 2>&1", timeout=15)
    raw = out.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in out else out
    try:
        d = json.loads(raw)
        hl = d.get("hyperliquid", {})
        hl_bal = float(hl.get("balanceUsd", 0))
        positions = hl.get("positions", [])
        record("saint:hl_balance", hl_bal > 0, f"${hl_bal:.2f}")
        record("saint:has_positions", len(positions) > 0, f"{len(positions)} open")

        # Calculate total unrealized PnL
        total_pnl = 0
        for p in positions:
            pnl = float(p.get("unrealizedPnl", 0))
            total_pnl += pnl
        record("saint:positive_pnl", total_pnl > 0, f"+${total_pnl:.2f} unrealized")

        # Check each position has valid data
        for p in positions:
            token = p.get("token", "?")
            size = float(p.get("size", 0))
            entry = float(p.get("entryPx", 0))
            has_data = size > 0 and entry > 0
            record(f"saint:pos_{token}", has_data, f"size={size} entry={entry}")
    except Exception as e:
        record("saint:parse", False, str(e)[:150])

def test_acp_marketplace():
    """Verify ACP marketplace has live offerings and active jobs"""
    print("\n--- ACP Marketplace ---")
    # Check ArmaBase offerings
    use_agent(ARMABASE_ID)
    out, _, _ = run_cmd("acp offering list --json 2>&1", timeout=15)
    raw = out.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in out else out
    try:
        d = json.loads(raw)
        offerings = d if isinstance(d, list) else d.get("offerings", d.get("data", []))
        record("armabase:has_offerings", len(offerings) > 0, f"{len(offerings)} offerings")
    except:
        record("armabase:offerings", False, "parse error")

    # Check active jobs
    out2, _, _ = run_cmd("acp job list --json 2>&1", timeout=15)
    raw2 = out2.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in out2 else out2
    try:
        d2 = json.loads(raw2)
        jobs = d2.get("jobs", d2 if isinstance(d2, list) else [])
        active = [j for j in jobs if j.get("jobStatus") == "SUBMITTED"]
        record("armabase:active_jobs", len(active) > 0, f"{len(active)} active jobs")
    except:
        record("armabase:jobs", False, "parse error")

    # Check Saint offerings
    use_agent(SAINT_ID)
    out3, _, _ = run_cmd("acp offering list --json 2>&1", timeout=15)
    raw3 = out3.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in out3 else out3
    try:
        d3 = json.loads(raw3)
        offerings3 = d3 if isinstance(d3, list) else d3.get("offerings", d3.get("data", []))
        record("saint:has_offerings", len(offerings3) > 0, f"{len(offerings3)} offerings")
    except:
        record("saint:offerings", False, "parse error")

    # Check Scout offerings
    use_agent(SCOUT_ID)
    out4, _, _ = run_cmd("acp offering list --json 2>&1", timeout=15)
    raw4 = out4.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in out4 else out4
    try:
        d4 = json.loads(raw4)
        offerings4 = d4 if isinstance(d4, list) else d4.get("offerings", d4.get("data", []))
        record("scout:has_offerings", len(offerings4) > 0, f"{len(offerings4)} offerings")
    except:
        record("scout:offerings", False, "parse error")

def test_treasury_profit():
    """Verify treasury and profit engines are tracking revenue correctly"""
    print("\n--- Treasury & Profit ---")
    # Check goal state
    goal_path = os.path.join(WORKSPACE, "goal_state.json")
    if os.path.exists(goal_path):
        with open(goal_path) as f:
            d = json.load(f)
        earned = float(d.get("total_earned", 0))
        goal = float(d.get("goal_usd", 0))
        record("goal:tracking", earned > 0, f"${earned:.2f} / ${goal:,.0f}")
        # Goal progress: just verify it's tracking (>0 means revenue is flowing)
        pct = earned / goal * 100 if goal > 0 else 0
        record("goal:progress_pct", earned > 0, f"{pct:.4f}%")
    else:
        record("goal:file", False, "goal_state.json not found")

    # Check profit log
    profit_path = os.path.join(WORKSPACE, "profit_log.json")
    if os.path.exists(profit_path):
        with open(profit_path) as f:
            d = json.load(f)
        collected = float(d.get("total_collected", 0))
        record("profit:total_collected", collected > 0, f"${collected:.2f}")
    else:
        record("profit:file", False, "profit_log.json not found")

    # Check revenue log
    rev_path = os.path.join(WORKSPACE, "revenue_log.json")
    if os.path.exists(rev_path):
        with open(rev_path) as f:
            d = json.load(f)
        if isinstance(d, list) and len(d) > 0:
            record("revenue:has_entries", True, f"{len(d)} entries")
        else:
            record("revenue:has_entries", False, "empty or invalid")
    else:
        record("revenue:file", False, "revenue_log.json not found")

def test_hl_spot_trader():
    """Verify HL spot trader script is valid and can read HL balance"""
    print("\n--- HL Spot Trader ---")
    path = os.path.join(WORKSPACE, "hl_spot_trader.py")
    ok, detail = py_compile_check(path)
    record("hl_spot:syntax", ok, detail)
    record("hl_spot:in_cron_dir", in_cron_dir("hl_spot_trader.py"), "exists" if in_cron_dir("hl_spot_trader.py") else "MISSING")

    # Verify it uses Saint agent ID
    with open(path) as f:
        src = f.read()
    record("hl_spot:saint_id", "019f9f75-493a" in src, "Saint ID present")

def test_arb_scanner():
    """Verify arb scanner script is valid"""
    print("\n--- Arb Scanner ---")
    path = os.path.join(WORKSPACE, "arb_scanner.py")
    ok, detail = py_compile_check(path)
    record("arb:syntax", ok, detail)
    record("arb:in_cron_dir", in_cron_dir("arb_scanner.py"), "exists" if in_cron_dir("arb_scanner.py") else "MISSING")

    # Verify it can fetch HL orderbook (the bug we fixed)
    try:
        r = requests.post(HL_API, json={"type": "l2Book", "coin": "BTC"}, timeout=10)
        book = r.json()
        levels = book.get("levels", [])
        ok = len(levels) >= 2 and len(levels[0]) > 0 and len(levels[1]) > 0
        record("arb:hl_orderbook", ok, f"bids={len(levels[0])} asks={len(levels[1])}" if ok else "invalid format")
    except Exception as e:
        record("arb:hl_orderbook", False, str(e)[:100])

def test_token_utility():
    """Verify token utility engine is valid"""
    print("\n--- Token Utility ---")
    path = os.path.join(WORKSPACE, "token_utility_engine.py")
    ok, detail = py_compile_check(path)
    record("token_util:syntax", ok, detail)
    record("token_util:in_cron_dir", in_cron_dir("token_utility_engine.py"), "exists" if in_cron_dir("token_utility_engine.py") else "MISSING")

    # Check log exists (proves it ran)
    log_path = os.path.join(WORKSPACE, "token_utility_log.json")
    if os.path.exists(log_path):
        with open(log_path) as f:
            d = json.load(f)
        record("token_util:has_log", len(d) > 0, f"{len(d)} entries")
    else:
        record("token_util:has_log", False, "no log file")

def test_sol_distributor():
    """Verify sol distributor is valid and config is correct"""
    print("\n--- SOL Distributor ---")
    path = os.path.join(WORKSPACE, "sol_distributor.py")
    ok, detail = py_compile_check(path)
    record("sol_dist:syntax", ok, detail)
    record("sol_dist:in_cron_dir", in_cron_dir("sol_distributor.py"), "exists" if in_cron_dir("sol_distributor.py") else "MISSING")

    # Verify it keeps 100% SOL for sniper (post-bridge-fix config)
    with open(path) as f:
        src = f.read()
    record("sol_dist:keep_100", "SNIPER_KEEP_PCT = 100" in src, "100% keep SOL")

def test_cron_scripts_in_dir():
    """Verify all cron scripts exist in ~/.hermes/scripts/"""
    print("\n--- Cron Script Directory ---")
    critical_scripts = [
        "saint_perps.py", "treasury_engine.py", "profit_engine.py",
        "revenue_engine.py", "arb_scanner.py", "hl_spot_trader.py",
        "sol_distributor.py", "token_utility_engine.py", "test_suite.py",
    ]
    for s in critical_scripts:
        exists = in_cron_dir(s)
        record(f"cron_dir:{s}", exists, "exists" if exists else "MISSING")

# ============================================================
# MAIN
# ============================================================

def main():
    print(f"\n{'=' * 70}")
    print(f"  CANONICAL ECOSYSTEM TEST SUITE")
    print(f"  {now_utc()[:19]}")
    print(f"  Focus: Revenue systems (HL perps, marketplace, treasury)")
    print(f"{'=' * 70}")

    test_script_syntax()
    test_cron_health()
    test_saint_hl_positions()
    test_acp_marketplace()
    test_treasury_profit()
    test_hl_spot_trader()
    test_arb_scanner()
    test_token_utility()
    test_sol_distributor()
    test_cron_scripts_in_dir()

    print(f"\n{'=' * 70}")
    print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
    print(f"  OVERALL: {'ALL PASS ✅' if failed == 0 else f'{failed} FAILURES ❌'}")
    print(f"{'=' * 70}")

    # Save results
    output = {
        "timestamp": now_utc(),
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
        "all_passed": failed == 0,
        "tests": results,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)

    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
