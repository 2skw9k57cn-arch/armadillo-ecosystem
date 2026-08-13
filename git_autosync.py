#!/usr/bin/env python3
"""
Git Auto-Sync — detects any file changes (add/modify/delete) in /workspace
and commits + pushes to GitHub automatically.

Runs as a cron job. Only acts when there are actual changes.
"""
import subprocess
import os
import sys
from datetime import datetime

REPO_DIR = "/workspace"
GIT_REMOTE = "origin"

def run_git(*args, timeout=30):
    """Run a git command in the repo dir."""
    r = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, timeout=timeout, cwd=REPO_DIR
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def has_changes():
    """Check if there are any staged, unstaged, or untracked changes."""
    code, out, _ = run_git("status", "--porcelain")
    return bool(out.strip()), out.strip()

def commit_and_push():
    """Stage all changes, commit with timestamp, push to origin."""
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    # Stage only known file types (never commit arbitrary error output or temp files)
    run_git("add", "--", "*.py", "*.json", "*.md", "*.txt", "*.png", "*.webp", ".gitignore")
    
    # Check what's staged
    code, staged, _ = run_git("diff", "--cached", "--stat")
    if not staged:
        print(f"No staged changes — nothing to commit [{now}]")
        return False
    
    # Commit
    commit_msg = f"Auto-sync: file changes detected [{now}]"
    code, out, err = run_git("commit", "-m", commit_msg)
    if code != 0:
        print(f"Commit failed: {err}")
        return False
    
    # Push
    code, out, err = run_git("push", GIT_REMOTE, "main", timeout=60)
    if code != 0:
        print(f"Push failed: {err}")
        return False
    
    # Show what changed
    print(f"✅ Auto-synced to GitHub [{now}]")
    for line in staged.split('\n')[:10]:
        print(f"   {line.strip()}")
    if len(staged.split('\n')) > 10:
        print(f"   ... and {len(staged.split(chr(10))) - 10} more")
    return True

def main():
    # Ensure git config is set
    run_git("config", "user.email", "saint-ogsaint@armadillo.ecosystem")
    run_git("config", "user.name", "Saint-OGSAINT")
    
    changed, details = has_changes()
    if not changed:
        print(f"✅ No changes — repo in sync [{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}]")
        return
    
    print(f"📋 Changes detected:")
    for line in details.split('\n')[:10]:
        print(f"   {line.strip()}")
    if len(details.split('\n')) > 10:
        print(f"   ... and {len(details.split(chr(10))) - 10} more")
    
    commit_and_push()

if __name__ == "__main__":
    main()
