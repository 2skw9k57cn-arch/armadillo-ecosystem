#!/usr/bin/env python3
"""
Agent Communication Layer
=========================
Allows this Hermes agent to interact with all 4 Armadillo ecosystem agents
via the ACP job system. Supports:

  1. Hiring agents via their offerings (create jobs)
  2. Monitoring incoming/active jobs
  3. Sending messages in job rooms
  4. Submitting deliverables
  5. Checking job history
  6. Browsing available agents

Usage:
  python3 agent_comm.py status          — Show all agents + active jobs
  python3 agent_comm.py hire <agent> <offering> <requirements_json>
  python3 agent_comm.py message <job_id> <text>
  python3 agent_comm.py deliver <job_id> <text>
  python3 agent_comm.py history <job_id>
  python3 agent_comm.py browse [query]
  python3 agent_comm.py watch <job_id> [timeout]
  python3 agent_comm.py listen [output_file]

Agents:
  armabase  — Research/analysis hub (26 offerings)
  scout     — Ecosystem scout, ARRB sales, token research
  saint     — Perps trader, HL perp signals
  ogsaint   — Cross-agent coordination, market sentiment
"""

import json, os, sys, subprocess, time
from datetime import datetime

# ============ AGENT REGISTRY ============
AGENTS = {
    "armabase": {
        "id": "019fbb50-31de-7e2f-be3b-2225023960b3",
        "wallet": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc",
        "name": "ArmaBase",
    },
    "scout": {
        "id": "019fa674-7be2-72ce-956c-3a7f831e9102",
        "wallet": "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4",
        "name": "Armadillo Scout",
    },
    "saint": {
        "id": "019f9f75-493a-7011-b547-aa9c2df1a1ac",
        "wallet": "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d",
        "name": "Armadillo St.",
    },
    "ogsaint": {
        "id": "019f9f75-130e-75fc-9459-5358c8d25206",
        "wallet": "0x52a140c6dab119a6a050f857591bbf469c1856ce",
        "name": "Armadillo Saint",
    },
}

BASE_CHAIN = "8453"
CONFIG_PATH = os.path.expanduser("~/.config/acp/config.json")

def run(cmd, timeout=60):
    """Run acp command with proper env."""
    env = os.environ.copy()
    env["TS_KEYRING_BACKEND"] = "file"
    if isinstance(cmd, str):
        cmd = cmd.split()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    out = r.stdout.strip()
    err = r.stderr.strip()
    # Strip acp-wrapper noise
    if "[acp-wrapper]" in out:
        out = out.split("[acp-wrapper]")[0].strip()
    if "[acp-wrapper]" in err:
        err = err.split("[acp-wrapper]")[0].strip()
    return out, err, r.returncode

def use_agent(agent_key):
    """Switch active agent by key name."""
    agent = AGENTS.get(agent_key)
    if not agent:
        print(f"Unknown agent: {agent_key}. Valid: {', '.join(AGENTS.keys())}")
        return False
    out, err, rc = run(["acp", "agent", "use", "--agent-id", agent["id"]], timeout=15)
    # Also set config.json activeWallet
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        cfg["activeWallet"] = agent["wallet"]
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
    return rc == 0

def parse_json(out):
    """Try to parse JSON from acp output."""
    try:
        return json.loads(out)
    except Exception:
        return None

# ============ COMMANDS ============

def cmd_status():
    """Show all agents + their offerings + active jobs."""
    print("=" * 70)
    print(f"  ARMADILLO ECOSYSTEM STATUS — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    for key, agent in AGENTS.items():
        print(f"\n  [{key.upper()}] {agent['name']} ({agent['wallet'][:10]}...)")
        if use_agent(key):
            # Offerings
            out, _, _ = run(["acp", "offering", "list"], timeout=15)
            lines = [l for l in out.split("\n") if l.strip() and "ID" not in l]
            if lines:
                for l in lines[:5]:
                    parts = l.split("\t")
                    if len(parts) >= 2:
                        print(f"    Offering: {parts[1]} (${parts[2]})" if len(parts) > 2 else f"    {l}")
                if len(lines) > 5:
                    print(f"    ... +{len(lines) - 5} more offerings")

    # Active jobs
    print(f"\n  [ACTIVE JOBS]")
    out, _, _ = run(["acp", "job", "list"], timeout=15)
    if "No active jobs" in out or not out.strip():
        print("    None")
    else:
        print(f"    {out}")

    # Compute status (current agent)
    print(f"\n  [COMPUTE]")
    out, _, _ = run(["acp", "compute", "status"], timeout=15)
    if out.strip():
        print(f"    {out.strip()}")

    print("\n" + "=" * 70)

def cmd_hire(agent_key, offering_name, requirements_json):
    """Hire an agent by creating a job from their offering."""
    agent = AGENTS.get(agent_key)
    if not agent:
        print(f"Unknown agent: {agent_key}. Valid: {', '.join(AGENTS.keys())}")
        return

    # First, make sure we're using a different agent as the client (hire FROM someone)
    # Default: use armabase as the client if hiring others, or scout if hiring armabase
    if agent_key == "armabase":
        client = "scout"
    else:
        client = "armabase"

    print(f"Hiring {agent['name']} (offering: {offering_name}) as {AGENTS[client]['name']}...")
    use_agent(client)

    try:
        requirements = json.loads(requirements_json)
    except json.JSONDecodeError:
        print(f"Invalid requirements JSON: {requirements_json}")
        return

    out, err, rc = run([
        "acp", "client", "create-job",
        "--provider", agent["wallet"],
        "--offering-name", offering_name,
        "--requirements", json.dumps(requirements),
        "--chain-id", BASE_CHAIN,
    ], timeout=60)

    if rc == 0:
        data = parse_json(out)
        if data and data.get("jobId"):
            print(f"✅ Job created! Job ID: {data['jobId']}")
            print(f"   Chain: {BASE_CHAIN}")
            # Fund the job if we know the price
            if data.get("budget"):
                print(f"   Budget: ${data['budget']} USDC")
        else:
            print(f"✅ Job created: {out}")
    else:
        print(f"❌ Failed: {err or out}")

def cmd_message(job_id, text):
    """Send a message in a job room."""
    out, err, rc = run([
        "acp", "message", "send",
        "--job-id", str(job_id),
        "--chain-id", BASE_CHAIN,
        "--content", text,
    ], timeout=30)

    if rc == 0:
        print(f"✅ Message sent to job {job_id}")
    else:
        print(f"❌ Failed: {err or out}")

def cmd_deliver(job_id, text):
    """Submit a deliverable for a job."""
    out, err, rc = run([
        "acp", "provider", "submit",
        "--job-id", str(job_id),
        "--chain-id", BASE_CHAIN,
        "--deliverable", text,
    ], timeout=30)

    if rc == 0:
        print(f"✅ Deliverable submitted for job {job_id}")
    else:
        print(f"❌ Failed: {err or out}")

def cmd_history(job_id):
    """Get job history including messages."""
    out, err, rc = run([
        "acp", "job", "history",
        "--job-id", str(job_id),
        "--chain-id", BASE_CHAIN,
    ], timeout=30)

    if rc == 0:
        data = parse_json(out)
        if data:
            print(f"Job {job_id} History:")
            print(f"  Status: {data.get('status', 'unknown')}")
            msgs = data.get("messages", [])
            if msgs:
                print(f"  Messages ({len(msgs)}):")
                for m in msgs:
                    sender = m.get("sender", "?")
                    content = m.get("content", "")[:200]
                    ts = m.get("timestamp", "")
                    print(f"    [{ts}] {sender}: {content}")
            else:
                print("  No messages")
        else:
            print(out)
    else:
        print(f"❌ Failed: {err or out}")

def cmd_browse(query=""):
    """Browse available agents on the ACP marketplace."""
    cmd = ["acp", "browse"]
    if query:
        cmd.append(query)
    out, err, rc = run(cmd, timeout=30)

    if rc == 0:
        print(out)
    else:
        print(f"❌ Failed: {err or out}")

def cmd_watch(job_id, timeout_sec=None):
    """Block until a job needs action."""
    cmd = ["acp", "job", "watch", "--job-id", str(job_id)]
    if timeout_sec:
        cmd.extend(["--timeout", str(timeout_sec)])

    print(f"Watching job {job_id} for events...")
    out, err, rc = run(cmd, timeout=int(timeout_sec) + 30 if timeout_sec else 600)

    if rc == 0:
        print(f"Event: {out}")
    else:
        print(f"Watch ended: {err or out}")

def cmd_listen(output_file=None):
    """Stream job events as JSON lines (long-running)."""
    cmd = ["acp", "events", "listen"]
    if output_file:
        cmd.extend(["--output", output_file])

    print(f"Listening for events...{'→ ' + output_file if output_file else ''}")
    out, err, rc = run(cmd, timeout=3600)

    if out:
        print(out)
    if err and rc != 0:
        print(f"Error: {err}")

def cmd_offerings(agent_key):
    """List all offerings for an agent."""
    if not use_agent(agent_key):
        print(f"Failed to switch to {agent_key}")
        return

    out, _, _ = run(["acp", "offering", "list"], timeout=15)
    print(f"\n{AGENTS[agent_key]['name']} Offerings:")
    print(out)

# ============ MAIN ============

USAGE = """
Agent Communication Layer
==========================
Usage:
  python3 agent_comm.py status                          Show all agents + jobs
  python3 agent_comm.py offerings <agent>               List agent offerings
  python3 agent_comm.py hire <agent> <offering> <json>  Hire an agent
  python3 agent_comm.py message <job_id> <text>         Send message in job
  python3 agent_comm.py deliver <job_id> <text>         Submit deliverable
  python3 agent_comm.py history <job_id>                Job history + messages
  python3 agent_comm.py browse [query]                  Browse marketplace
  python3 agent_comm.py watch <job_id> [timeout]        Watch job for events
  python3 agent_comm.py listen [output_file]            Stream events

Agents: armabase, scout, saint, ogsaint
"""

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        cmd_status()
    elif cmd == "offerings":
        if len(sys.argv) < 3:
            print("Usage: offerings <agent>")
        else:
            cmd_offerings(sys.argv[2])
    elif cmd == "hire":
        if len(sys.argv) < 5:
            print("Usage: hire <agent> <offering_name> <requirements_json>")
        else:
            cmd_hire(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "message":
        if len(sys.argv) < 4:
            print("Usage: message <job_id> <text>")
        else:
            cmd_message(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "deliver":
        if len(sys.argv) < 4:
            print("Usage: deliver <job_id> <text>")
        else:
            cmd_deliver(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "history":
        if len(sys.argv) < 3:
            print("Usage: history <job_id>")
        else:
            cmd_history(sys.argv[2])
    elif cmd == "browse":
        cmd_browse(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "watch":
        if len(sys.argv) < 3:
            print("Usage: watch <job_id> [timeout_seconds]")
        else:
            cmd_watch(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif cmd == "listen":
        cmd_listen(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(f"Unknown command: {cmd}")
        print(USAGE)
