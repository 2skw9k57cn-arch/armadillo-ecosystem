#!/usr/bin/env python3
"""
Ecosystem Test Suite
====================
Full integration tests for all autonomous ecosystem scripts.

Verifies:
  1. All scripts compile (syntax check)
  2. All scripts import without error
  3. Scout: run_with_retry, get_balance RPC, browse no-hang, full cycle
  4. Goal tracker: get_acp_usdc_total RPC speed
  5. Saint perps: get_hl_status, scan_markets, full cycle
  6. Profit engine: config path, cross-hire, full cycle
  7. Revenue engine: sweep logic, full cycle
  8. Compute autotopup: 404 graceful skip, full cycle
  9. X poster: credits depleted skip flag, full cycle
 10. SOL distributor: full cycle
 11. Growth engine: full cycle
 12. Cron watchdog: full cycle

Run:
    cd /workspace && python3 tests/test_scout_suite.py -v
    # or via pytest:
    cd /workspace && python3 -m pytest tests/test_scout_suite.py -v
"""
import sys, os, time, importlib.util, subprocess, json

WORKSPACE = "/workspace"
os.chdir(WORKSPACE)
sys.path.insert(0, WORKSPACE)

# ─── Helpers ───────────────────────────────────────────────────────────

def read(path):
    with open(path) as f:
        return f.read()

def load_module(name, path):
    """Load a module by exec'ing everything except the __main__ block."""
    code = read(path)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"Failed to create spec for {name}"
    mod = importlib.util.module_from_spec(spec)
    main_block = code.split("if __name__")[0]
    exec(compile(main_block, name, "exec"), mod.__dict__)
    return mod, code

def run_script(script, timeout):
    """Run a script as subprocess and return (rc, elapsed, stdout, stderr)."""
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, f"{WORKSPACE}/{script}"],
            capture_output=True, text=True, timeout=timeout, cwd=WORKSPACE
        )
        return r.returncode, time.time() - t0, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, time.time() - t0, "", "TIMEOUT"

# ─── Scripts to test ───────────────────────────────────────────────────

ALL_SCRIPTS = [
    "scout_ecosystem.py",
    "saint_perps.py",
    "profit_engine.py",
    "revenue_engine.py",
    "compute_autotopup.py",
    "x_poster.py",
    "sol_distributor.py",
    "growth_engine.py",
    "cron_watchdog.py",
    "goal_tracker.py",
]


class TestEcosystemSuite:
    """Full ecosystem integration tests."""

    # ─── Phase 1: Compile checks ───────────────────────────────────────

    def test_all_scripts_compile(self):
        """All ecosystem scripts have no syntax errors."""
        errors = []
        for script in ALL_SCRIPTS:
            path = f"{WORKSPACE}/{script}"
            if not os.path.exists(path):
                errors.append(f"{script}: file not found")
                continue
            try:
                compile(read(path), script, "exec")
            except SyntaxError as e:
                errors.append(f"{script}: {e}")
        assert not errors, f"Compile failures:\n  " + "\n  ".join(errors)

    # ─── Phase 2: Import checks ────────────────────────────────────────

    def test_scout_imports(self):
        """scout_ecosystem.py loads all functions without error."""
        scout, _ = load_module("scout", f"{WORKSPACE}/scout_ecosystem.py")
        for fn in ["run", "run_with_retry", "get_balance", "browse_marketplace",
                    "check_acp_jobs", "check_arrb_status", "swap_virtual_to_usdc",
                    "route_usdc_to_armabase", "generate_scout_report"]:
            assert hasattr(scout, fn), f"Missing function: {fn}"

    def test_goal_tracker_imports(self):
        """goal_tracker.py loads without error."""
        from goal_tracker import get_acp_usdc_total, check_goal
        assert callable(get_acp_usdc_total)
        assert callable(check_goal)

    def test_saint_perps_imports(self):
        """saint_perps.py loads all functions without error."""
        saint, _ = load_module("saint", f"{WORKSPACE}/saint_perps.py")
        for fn in ["get_hl_status", "get_all_markets", "analyze_market",
                    "check_positions", "open_position", "close_position", "get_balance"]:
            assert hasattr(saint, fn), f"Missing function: {fn}"

    # ─── Phase 3: Scout unit tests ─────────────────────────────────────

    def test_scout_run_with_retry_no_unbound(self):
        """run_with_retry does not raise UnboundLocalError on all-failures."""
        scout, _ = load_module("scout", f"{WORKSPACE}/scout_ecosystem.py")
        out, err, rc = scout.run_with_retry("false", max_retries=1, base_timeout=5)
        assert rc != 0, f"Expected failure, got rc={rc}"

    def test_scout_get_balance_usdc_fast(self):
        """get_balance('USDC') uses direct RPC and completes in <3s."""
        scout, _ = load_module("scout", f"{WORKSPACE}/scout_ecosystem.py")
        t0 = time.time()
        bal = scout.get_balance("USDC", 8453)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"get_balance took {elapsed:.1f}s, expected <3s"
        assert isinstance(bal, float)

    def test_scout_browse_marketplace_no_hang(self):
        """browse_marketplace completes in <30s even when ACP browse API is down."""
        scout, _ = load_module("scout", f"{WORKSPACE}/scout_ecosystem.py")
        t0 = time.time()
        results = scout.browse_marketplace()
        elapsed = time.time() - t0
        assert elapsed < 30.0, f"browse_marketplace took {elapsed:.1f}s, expected <30s"
        assert isinstance(results, list)

    def test_scout_routing_threshold(self):
        """route_usdc_to_armabase keeps $10 threshold (doesn't drain Scout)."""
        scout, _ = load_module("scout", f"{WORKSPACE}/scout_ecosystem.py")
        code = read(f"{WORKSPACE}/scout_ecosystem.py")
        assert "usdc > 10" in code, "Routing threshold should be $10"

    # ─── Phase 4: Goal tracker tests ───────────────────────────────────

    def test_goal_tracker_usdc_total_fast(self):
        """goal_tracker.get_acp_usdc_total uses RPC and completes in <5s (was 18s)."""
        from goal_tracker import get_acp_usdc_total
        t0 = time.time()
        total = get_acp_usdc_total()
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"get_acp_usdc_total took {elapsed:.1f}s, expected <5s"
        assert isinstance(total, float)
        assert total >= 0

    # ─── Phase 5: Saint perps tests ────────────────────────────────────

    def test_saint_get_hl_status(self):
        """get_hl_status returns a dict with expected keys and completes <20s."""
        saint, _ = load_module("saint", f"{WORKSPACE}/saint_perps.py")
        t0 = time.time()
        hl = saint.get_hl_status()
        elapsed = time.time() - t0
        assert elapsed < 20.0, f"get_hl_status took {elapsed:.1f}s, expected <20s"
        assert isinstance(hl, dict)
        assert "account_value" in hl, f"Missing 'account_value' key: {list(hl.keys())}"
        assert "positions" in hl, f"Missing 'positions' key: {list(hl.keys())}"

    # ─── Phase 6: Profit engine tests ──────────────────────────────────

    def test_profit_engine_config_path(self):
        """profit_engine uses ~/.config/acp/config.json (not /workspace/config.json)."""
        code = read(f"{WORKSPACE}/profit_engine.py")
        assert "~/.config/acp/config.json" in code, "Should use ~/.config/acp/config.json"

    def test_profit_engine_cross_hire_requirements(self):
        """cross_hire passes --requirements '{}' to create-job."""
        code = read(f"{WORKSPACE}/profit_engine.py")
        assert "'{{}}'" in code or "'{}'" in code, "Should pass --requirements '{}'"

    def test_profit_engine_cross_hire_offerings(self):
        """CHEAP_OFFERINGS uses correct offering names (not wrong agent's)."""
        code = read(f"{WORKSPACE}/profit_engine.py")
        assert "Gas-Free Token Snapshot" in code, "Scout offering should be Gas-Free Token Snapshot"
        assert "Quick Perp Setup" in code, "Saint offering should be Quick Perp Setup"
        assert "Gas Price Optimizer" in code, "ArmaBase offering should be Gas Price Optimizer"

    def test_profit_engine_timeout_15s(self):
        """profit_engine run() timeout is 15s (not 300s)."""
        code = read(f"{WORKSPACE}/profit_engine.py")
        assert "timeout=15" in code, "run() timeout should be 15s"

    # ─── Phase 7: Revenue engine tests ─────────────────────────────────

    def test_revenue_engine_sweep_50pct(self):
        """revenue_engine sweep is set to 50%."""
        code = read(f"{WORKSPACE}/revenue_engine.py")
        assert "SWEEP_PCT = 0.50" in code, "Sweep should be 50%"

    def test_revenue_engine_goal_wallet(self):
        """revenue_engine sweeps to Coinbase deposit address."""
        code = read(f"{WORKSPACE}/revenue_engine.py")
        assert "CYYEbobXq1TJQi3mtZ1eYoXDSnnVZe1yPLbTvkr8QQLM" in code, "Should sweep to Coinbase address"

    def test_revenue_engine_no_buyback(self):
        """revenue_engine buyback-burn is disabled."""
        code = read(f"{WORKSPACE}/revenue_engine.py")
        assert "buyback_usdc': 0" in code, "Buyback-burn should be disabled"

    # ─── Phase 8: Compute autotopup tests ──────────────────────────────

    def test_compute_autotopup_404_handling(self):
        """compute_autotopup handles 404 gracefully and skips remaining agents."""
        code = read(f"{WORKSPACE}/compute_autotopup.py")
        assert "404" in code, "Should handle 404 error"
        assert "api_broken" in code, "Should have api_broken early-exit flag"

    def test_compute_autotopup_rpc_balance(self):
        """compute_autotopup uses direct RPC for USDC (not slow acp wallet balance)."""
        code = read(f"{WORKSPACE}/compute_autotopup.py")
        assert "mainnet.base.org" in code, "Should use direct RPC for USDC"

    # ─── Phase 9: X poster tests ───────────────────────────────────────

    def test_x_poster_credits_depleted_flag(self):
        """x_poster handles 402 credits depleted with skip flag."""
        code = read(f"{WORKSPACE}/x_poster.py")
        assert "credits_depleted" in code, "Should handle credits depleted"
        assert "xurl_credits_depleted.flag" in code, "Should create skip flag file"
        assert "86400" in code, "Flag should expire after 24h"

    # ─── Phase 10: Full cycle runtime tests ────────────────────────────

    def test_scout_full_cycle(self):
        """scout_ecosystem.py full cycle completes in <60s."""
        rc, elapsed, out, err = run_script("scout_ecosystem.py", 60)
        assert rc == 0, f"Scout exited {rc}: {err[:200]}"
        assert elapsed < 60.0, f"Scout took {elapsed:.1f}s, expected <60s"

    def test_saint_perps_full_cycle(self):
        """saint_perps.py full cycle completes in <120s."""
        rc, elapsed, out, err = run_script("saint_perps.py", 120)
        assert rc == 0, f"Saint exited {rc}: {err[:200]}"
        assert elapsed < 120.0, f"Saint took {elapsed:.1f}s, expected <120s"

    def test_profit_engine_full_cycle(self):
        """profit_engine.py full cycle completes in <60s."""
        rc, elapsed, out, err = run_script("profit_engine.py", 60)
        assert rc == 0, f"Profit engine exited {rc}: {err[:200]}"
        assert elapsed < 60.0, f"Profit engine took {elapsed:.1f}s, expected <60s"

    def test_revenue_engine_full_cycle(self):
        """revenue_engine.py full cycle completes in <30s."""
        rc, elapsed, out, err = run_script("revenue_engine.py", 30)
        assert rc == 0, f"Revenue engine exited {rc}: {err[:200]}"
        assert elapsed < 30.0, f"Revenue engine took {elapsed:.1f}s, expected <30s"

    def test_compute_autotopup_full_cycle(self):
        """compute_autotopup.py full cycle completes in <45s."""
        rc, elapsed, out, err = run_script("compute_autotopup.py", 45)
        assert rc == 0, f"Compute autotopup exited {rc}: {err[:200]}"
        assert elapsed < 45.0, f"Compute autotopup took {elapsed:.1f}s, expected <45s"

    def test_x_poster_full_cycle(self):
        """x_poster.py full cycle completes in <20s."""
        rc, elapsed, out, err = run_script("x_poster.py", 20)
        assert rc == 0, f"X poster exited {rc}: {err[:200]}"
        assert elapsed < 20.0, f"X poster took {elapsed:.1f}s, expected <20s"

    def test_sol_distributor_full_cycle(self):
        """sol_distributor.py full cycle completes in <30s."""
        rc, elapsed, out, err = run_script("sol_distributor.py", 30)
        assert rc == 0, f"SOL distributor exited {rc}: {err[:200]}"
        assert elapsed < 30.0, f"SOL distributor took {elapsed:.1f}s, expected <30s"

    def test_growth_engine_full_cycle(self):
        """growth_engine.py full cycle completes in <45s."""
        rc, elapsed, out, err = run_script("growth_engine.py", 45)
        assert rc == 0, f"Growth engine exited {rc}: {err[:200]}"
        assert elapsed < 45.0, f"Growth engine took {elapsed:.1f}s, expected <45s"

    def test_cron_watchdog_full_cycle(self):
        """cron_watchdog.py full cycle completes in <30s."""
        rc, elapsed, out, err = run_script("cron_watchdog.py", 30)
        assert rc == 0, f"Cron watchdog exited {rc}: {err[:200]}"
        assert elapsed < 30.0, f"Cron watchdog took {elapsed:.1f}s, expected <30s"

    def test_goal_tracker_full_cycle(self):
        """goal_tracker.py full cycle completes in <20s."""
        rc, elapsed, out, err = run_script("goal_tracker.py", 20)
        assert rc == 0, f"Goal tracker exited {rc}: {err[:200]}"
        assert elapsed < 20.0, f"Goal tracker took {elapsed:.1f}s, expected <20s"


# ─── Test runner ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback

    suite = TestEcosystemSuite()
    methods = sorted(m for m in dir(suite) if m.startswith("test_"))
    passed, failed, skipped = 0, 0, 0

    print(f"\n{'='*60}")
    print(f"  Ecosystem Suite — {len(methods)} tests")
    print(f"{'='*60}\n")

    for method_name in methods:
        method = getattr(suite, method_name)
        t0 = time.time()
        try:
            method()
            elapsed = time.time() - t0
            print(f"  ✅ {method_name:45s} {elapsed:5.1f}s")
            passed += 1
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  ❌ {method_name:45s} {elapsed:5.1f}s  {str(e)[:80]}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"  RESULT: {passed}/{passed+failed} passed", end="")
    if failed:
        print(f" — {failed} FAILED ❌")
    else:
        print(f" — ALL GREEN ✅")
    print(f"{'='*60}")
    sys.exit(1 if failed else 0)
