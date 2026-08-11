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

# All 12 jobs: (schedule, name, script) — MUST use "every" prefix for recurring
ALL_JOBS = [
    ("every 3m",  "cron-watchdog",    "cron_watchdog.py"),
    ("every 5m",  "deployer-watcher", "deployer_watcher.py"),
    ("every 10m", "oversight",        "oversight.py"),
    ("every 10m", "sniper-guard",     "sniper_guard.py"),
    ("every 20m", "pumpfun-loop",     "pumpfun_multi_loop.py"),
    ("every 15m", "team-coordinator", "team_coordinator.py"),
    ("every 30m", "saint-perps",      "saint_perps.py"),
    ("every 45m", "scout-ecosystem",  "scout_ecosystem.py"),
    ("every 30m", "revenue-engine",   "revenue_engine.py"),
    ("every 1h",  "learning-engine",  "learning_engine.py"),
    ("every 1h",  "buyback-burn",     "buyback_burn.py"),
    ("every 2h",  "growth-engine",    "growth_engine.py"),
]


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
    Checks if all 10 crons exist, recreates any missing ones (including watchdog).
    Returns the set of jobs that were recreated.
    """
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


def run():
    from datetime import datetime
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    existing = get_existing_jobs()
    needed = {name for _, name, _ in ALL_JOBS}
    missing = needed - existing

    if not missing:
        print(f"✅ All {len(needed)} cron jobs present — nothing to do [{now}]")
        return

    print(f"⚠️ Missing {len(missing)} jobs: {', '.join(sorted(missing))} [{now}]")
    print(f"   Self-generating...")

    recreated = ensure_all_crons()

    if recreated:
        print(f"   ✅ Recreated: {', '.join(sorted(recreated))}")
    else:
        print(f"   ❌ Failed to recreate any jobs")

    # Verify
    final = get_existing_jobs()
    still_missing = needed - final
    if still_missing:
        print(f"   ⚠️ Still missing: {', '.join(sorted(still_missing))}")
    else:
        print(f"   ✅ All {len(needed)} jobs now active")


if __name__ == "__main__":
    # Check $1M goal — halt everything if achieved
    try:
        from goal_tracker import check_goal
        if check_goal():
            sys.exit(0)
    except Exception as e:
        print(f"Goal check skipped: {e}")
    run()
