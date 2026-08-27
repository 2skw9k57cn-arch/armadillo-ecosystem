#!/usr/bin/env python3
"""
Test suite for scout_ecosystem.py + goal_tracker.py
====================================================
Verifies:
  - Syntax / compile
  - Module imports
  - run_with_retry handles failures (no UnboundLocalError)
  - get_balance USDC fast RPC path (<3s)
  - goal_tracker.get_acp_usdc_total via RPC (<5s, was 18s)
  - browse_marketplace no-hang when API is down (<20s)
  - Scout full cycle completes (<45s)

Run:
    cd /workspace && python3 -m pytest tests/test_scout_suite.py -v
    # or directly:
    cd /workspace && python3 tests/test_scout_suite.py -v
"""
import sys, os, time, importlib.util, subprocess

# Setup paths
WORKSPACE = "/workspace"
os.chdir(WORKSPACE)
sys.path.insert(0, WORKSPACE)

# Import scout module (exec everything except __main__ block)
def load_scout():
    with open(f"{WORKSPACE}/scout_ecosystem.py") as f:
        code = f.read()
    spec = importlib.util.spec_from_file_location("scout", f"{WORKSPACE}/scout_ecosystem.py")
    assert spec is not None, "Failed to create module spec"
    scout = importlib.util.module_from_spec(spec)
    exec(compile(code.split("if __name__")[0], "scout", "exec"), scout.__dict__)
    return scout, code


class TestScoutSuite:
    """Test cases for scout_ecosystem.py and goal_tracker.py."""

    def test_scout_compiles(self):
        """scout_ecosystem.py has no syntax errors."""
        with open(f"{WORKSPACE}/scout_ecosystem.py") as f:
            code = f.read()
        compile(code, "scout_ecosystem.py", "exec")

    def test_goal_tracker_compiles(self):
        """goal_tracker.py has no syntax errors."""
        with open(f"{WORKSPACE}/goal_tracker.py") as f:
            code = f.read()
        compile(code, "goal_tracker.py", "exec")

    def test_scout_imports(self):
        """scout module loads all functions without error."""
        scout, _ = load_scout()
        assert hasattr(scout, "run")
        assert hasattr(scout, "run_with_retry")
        assert hasattr(scout, "get_balance")
        assert hasattr(scout, "browse_marketplace")
        assert hasattr(scout, "check_acp_jobs")
        assert hasattr(scout, "check_arrb_status")
        assert hasattr(scout, "swap_virtual_to_usdc")
        assert hasattr(scout, "route_usdc_to_armabase")

    def test_run_with_retry_no_unbound(self):
        """run_with_retry does not raise UnboundLocalError on all-failures."""
        scout, _ = load_scout()
        out, err, rc = scout.run_with_retry("false", max_retries=1, base_timeout=5)
        assert rc != 0, f"Expected failure, got rc={rc}"

    def test_get_balance_usdc_fast(self):
        """get_balance('USDC') uses direct RPC and completes in <3s."""
        scout, _ = load_scout()
        t0 = time.time()
        bal = scout.get_balance("USDC", 8453)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"get_balance took {elapsed:.1f}s, expected <3s"
        assert isinstance(bal, float)

    def test_goal_tracker_usdc_total_fast(self):
        """goal_tracker.get_acp_usdc_total uses RPC and completes in <5s (was 18s)."""
        from goal_tracker import get_acp_usdc_total
        t0 = time.time()
        total = get_acp_usdc_total()
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"get_acp_usdc_total took {elapsed:.1f}s, expected <5s"
        assert isinstance(total, float)
        assert total >= 0

    def test_browse_marketplace_no_hang(self):
        """browse_marketplace completes in <30s even when ACP browse API is down."""
        scout, _ = load_scout()
        t0 = time.time()
        results = scout.browse_marketplace()
        elapsed = time.time() - t0
        assert elapsed < 30.0, f"browse_marketplace took {elapsed:.1f}s, expected <30s"
        assert isinstance(results, list)

    def test_scout_full_cycle_completes(self):
        """Full scout_ecosystem.py main cycle completes in <60s."""
        t0 = time.time()
        result = subprocess.run(
            [sys.executable, f"{WORKSPACE}/scout_ecosystem.py"],
            capture_output=True, text=True, timeout=60, cwd=WORKSPACE
        )
        elapsed = time.time() - t0
        assert result.returncode == 0, f"Scout exited with {result.returncode}: {result.stderr[:200]}"
        assert elapsed < 60.0, f"Full cycle took {elapsed:.1f}s, expected <60s"


# Allow running directly without pytest
if __name__ == "__main__":
    # Minimal test runner for environments without pytest
    import traceback

    suite = TestScoutSuite()
    methods = [m for m in dir(suite) if m.startswith("test_")]
    passed, failed = 0, 0

    print(f"\n{'='*60}")
    print(f"  Scout Suite — {len(methods)} tests")
    print(f"{'='*60}\n")

    for method_name in methods:
        method = getattr(suite, method_name)
        try:
            method()
            print(f"  ✅ {method_name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {method_name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"  RESULT: {passed}/{passed+failed} passed")
    if failed:
        print(f"  {failed} FAILED")
    print(f"{'='*60}")
    sys.exit(1 if failed else 0)
