#!/usr/bin/env python3
"""Risk-gated launcher for strategy scripts."""
import os
import subprocess
import sys

from control_plane import TARGETS, SCRIPT_ROOT, evaluate_risk, record_launcher_event
from goal_tracker import check_goal, check_maintenance_mode


def ensure_crons():
    try:
        from cron_watchdog import ensure_all_crons
        recreated = ensure_all_crons()
        if recreated:
            print(f"  🔧 Self-healed crons: {', '.join(sorted(recreated))}")
    except Exception as exc:
        print(f"  ⚠️ Cron self-heal skipped: {exc}")


def run_strategy(name):
    if name not in TARGETS:
        print(f"Unknown strategy: {name}")
        return 2

    ensure_crons()

    try:
        if check_goal():
            record_launcher_event(name, "halted", "goal-achieved", 0, "goal achieved")
            return 0
    except Exception as exc:
        print(f"Goal check skipped: {exc}")

    try:
        if check_maintenance_mode():
            record_launcher_event(name, "paused", "maintenance", 0, "maintenance mode")
            return 0
    except Exception as exc:
        print(f"Maintenance check skipped: {exc}")

    risk_state = evaluate_risk(write_files=True)
    decision = risk_state["strategies"][name]
    detail = "; ".join(decision["reasons"]) if decision["reasons"] else "allowed"

    print(f"\n🧭 Strategy launcher — {name}")
    print(f"   Decision: {decision['decision']} ({decision['mode']})")
    if decision["reasons"]:
        for reason in decision["reasons"]:
            print(f"   • {reason}")

    if decision["decision"] in {"block", "paper_only"}:
        record_launcher_event(name, decision["decision"], decision["mode"], 0, detail)
        print("   Live execution skipped.")
        return 0

    script_path = os.path.join(SCRIPT_ROOT, TARGETS[name]["script"])
    env = os.environ.copy()
    env["ARMADILLO_RISK_MODE"] = decision["mode"]
    env["ARMADILLO_POSITION_SCALE"] = str(decision.get("recommended_size_mult", 1.0))
    env["ARMADILLO_STRATEGY_NAME"] = name

    result = subprocess.run([sys.executable, script_path], env=env)
    record_launcher_event(name, decision["decision"], decision["mode"], result.returncode, detail)
    return result.returncode


def main(argv=None):
    argv = argv or sys.argv[1:]
    if len(argv) != 1:
        print("Usage: python strategy_launcher.py <strategy-name>")
        return 2
    return run_strategy(argv[0])


if __name__ == "__main__":
    sys.exit(main())

