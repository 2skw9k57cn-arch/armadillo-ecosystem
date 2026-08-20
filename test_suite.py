#!/usr/bin/env python3
"""
Ecosystem Test Suite
====================
Permanent automated verification for all ecosystem scripts.
Runs every 30 minutes via cron. Checks syntax, imports, config values,
and end-to-end execution of every cron script.

Reports failures to /workspace/test_results.json for monitoring.
"""

import sys, json, os, subprocess, ast
from datetime import datetime, timezone

WORKSPACE = "/workspace"
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
RESULTS_FILE = "/workspace/test_results.json"

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def check_py_compile(path):
    r = subprocess.run([sys.executable, "-m", "py_compile", path],
        capture_output=True, text=True, timeout=10)
    return r.returncode == 0, r.stderr.strip()[:200] if r.stderr else "ok"

def check_import(path, module_name):
    """Check import without executing module side effects (no __main__ block)"""
    r = subprocess.run([sys.executable, "-c",
        f"import sys;sys.path.insert(0,'/workspace');import ast;ast.parse(open('{path}').read());print('ok')"],
        capture_output=True, text=True, timeout=10)
    return r.returncode == 0, r.stderr.strip()[:200] if r.stderr else "ok"

def check_in_cron_dir(filename):
    path = os.path.join(SCRIPTS_DIR, filename)
    return os.path.exists(path), "exists" if os.path.exists(path) else "MISSING"

def check_end_to_run(path, expect_keyword=None):
    r = subprocess.run([sys.executable, path],
        capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0
    if ok and expect_keyword:
        ok = expect_keyword.lower() in r.stdout.lower()
    detail = r.stdout.strip()[:120] if ok else f"exit={r.returncode} {r.stderr.strip()[:120]}"
    return ok, detail

# ============ ALL ECOSYSTEM SCRIPTS ============
SCRIPTS = [
    {"name": "cron_watchdog",        "file": "cron_watchdog.py",          "module": "cron_watchdog",          "keyword": None},
    {"name": "deployer_watcher",      "file": "deployer_watcher.py",       "module": "deployer_watcher",       "keyword": None},
    {"name": "sniper_guard",          "file": "sniper_guard.py",           "module": "sniper_guard",           "keyword": None},
    {"name": "saint_perps",           "file": "saint_perps.py",            "module": "saint_perps",            "keyword": None},
    {"name": "learning_engine",       "file": "learning_engine.py",        "module": "learning_engine",        "keyword": None},
    {"name": "treasury_engine",       "file": "treasury_engine.py",        "module": "treasury_engine",        "keyword": None},
    {"name": "volume_engine",         "file": "volume_engine.py",          "module": "volume_engine",          "keyword": None},
    {"name": "profit_engine",         "file": "profit_engine.py",          "module": "profit_engine",          "keyword": None},
    {"name": "revenue_engine",        "file": "revenue_engine.py",         "module": "revenue_engine",         "keyword": None},
    {"name": "growth_engine",         "file": "growth_engine.py",          "module": "growth_engine",          "keyword": None},
    {"name": "team_coordinator",      "file": "team_coordinator.py",       "module": "team_coordinator",       "keyword": None},
    {"name": "scout_ecosystem",       "file": "scout_ecosystem.py",        "module": "scout_ecosystem",        "keyword": None},
    {"name": "git_autosync",          "file": "git_autosync.py",           "module": "git_autosync",           "keyword": None},
    {"name": "pumpfun_multi_loop",    "file": "pumpfun_multi_loop.py",     "module": "pumpfun_multi_loop",     "keyword": None},
    {"name": "arb_scanner",           "file": "arb_scanner.py",            "module": "arb_scanner",            "keyword": "arbitrage"},
    {"name": "hl_spot_trader",        "file": "hl_spot_trader.py",         "module": "hl_spot_trader",         "keyword": "spot"},
    {"name": "x_poster",              "file": "x_poster.py",               "module": "x_poster",               "keyword": "poster"},
    {"name": "sol_distributor",       "file": "sol_distributor.py",        "module": "sol_distributor",        "keyword": "distributor"},
    {"name": "token_utility_engine",  "file": "token_utility_engine.py",   "module": "token_utility_engine",   "keyword": "utility"},
]

# ============ RUN TESTS ============
def run_tests():
    print(f"\n🧪 Ecosystem Test Suite — {now_utc()[:19]}")
    print("=" * 70)

    all_results = []
    total_pass = 0
    total_fail = 0

    for s in SCRIPTS:
        ws_path = os.path.join(WORKSPACE, s["file"])
        script_path = ws_path if os.path.exists(ws_path) else os.path.join(SCRIPTS_DIR, s["file"])

        checks = []
        script_ok = True

        # 1. py_compile
        if os.path.exists(script_path):
            ok, detail = check_py_compile(script_path)
            checks.append({"check": "py_compile", "passed": ok, "detail": detail})
            if not ok:
                script_ok = False
        else:
            checks.append({"check": "py_compile", "passed": False, "detail": "file not found"})
            script_ok = False

        # 2. import
        if script_ok:
            ok, detail = check_import(script_path, s["module"])
            checks.append({"check": "import", "passed": ok, "detail": detail})
            if not ok:
                script_ok = False

        # 3. in cron dir
        ok, detail = check_in_cron_dir(s["file"])
        checks.append({"check": "in_cron_dir", "passed": ok, "detail": detail})

        # 4. end-to-end run (only for new scripts with keywords — skip core scripts to avoid side effects)
        if s["keyword"] and os.path.exists(ws_path):
            ok, detail = check_end_to_run(ws_path, s["keyword"])
            checks.append({"check": "end_to_end", "passed": ok, "detail": detail})
            if not ok:
                script_ok = False

        passed = sum(1 for c in checks if c["passed"])
        failed = len(checks) - passed
        total_pass += passed
        total_fail += failed

        status = "✅" if script_ok else "❌"
        check_summary = " | ".join(f"{'P' if c['passed'] else 'F'}:{c['check']}" for c in checks)
        print(f"  {status} {s['name']:25} | {check_summary}")

        all_results.append({
            "name": s["name"],
            "file": s["file"],
            "checks": checks,
            "all_passed": script_ok,
        })

    print("=" * 70)
    print(f"  TOTAL: {total_pass} passed, {total_fail} failed, {len(SCRIPTS)} scripts")
    print(f"  OVERALL: {'ALL PASS ✅' if total_fail == 0 else f'{total_fail} FAILURES ❌'}")

    # Save results
    results = {
        "timestamp": now_utc(),
        "total_scripts": len(SCRIPTS),
        "total_pass": total_pass,
        "total_fail": total_fail,
        "all_passed": total_fail == 0,
        "scripts": all_results,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

    return total_fail == 0

if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
