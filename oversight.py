#!/usr/bin/env python3
"""
Armadillo Oversight — Master System Manager
=============================================
Single-point oversight for the entire Armadillo ecosystem.

Runs every 10 minutes via cron. Monitors:

  1. CRON HEALTH     — all 10 jobs alive? (delegates to cron_watchdog mesh)
  2. AGENT POLICIES  — all 3 agents autonomous? (detects policy regressions)
  3. WALLET BALANCES — all agents funded? flags any below $2 threshold
  4. HL POSITIONS    — Saint's perps tracked, SL/TP enforced, PnL logged
  5. TRADE FLOW      — pumpfun loop active? deployer watcher scanning?
  6. REVENUE FLYWHEEL— buyback-burn + revenue + growth engines producing?
  7. SOLANA WALLET   — sniper wallet has SOL for pump.fun buys?
  8. LEARNING DB     — saint_learning.json + learned_params.json healthy?
  9. COMPUTE BALANCE — agents have enough compute for ACP operations?
 10. SELF-HEALING    — auto-fixes everything it can, reports what it can't

Output: structured dashboard printed to stdout (delivered by cron).
Alerts: writes to /workspace/oversight_alerts.json for user review.
"""
import json, os, sys, subprocess, time, re
from datetime import datetime, timezone

PATH = "/opt/hermes-agent/venv/bin:/opt/hermes-agent:" + os.environ.get("PATH", "")
os.environ["PATH"] = PATH

HERMES = "/opt/hermes-agent/venv/bin/hermes"
WORKDIR = "/workspace"
ALERTS_PATH = os.path.join(WORKDIR, "oversight_alerts.json")
STATE_PATH = os.path.join(WORKDIR, "oversight_state.json")
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"
USER_SOL_WALLET = "EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd"

AGENTS = [
    {"name": "ArmaBase", "id": "019fbb50-31de-7e2f-be3b-2225023960b3",
     "wallet": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc",
     "role": "Revenue + buyback-burn + growth + pumpfun loop + deployer watching",
     "min_usd": 2.0, "expected_policy": ["ACP_ONLY", "No Policy"]},
    {"name": "Scout", "id": "019fa674-7be2-72ce-956c-3a7f831e9102",
     "wallet": "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4",
     "role": "Ecosystem scout + cross-chain swaps + marketplace jobs",
     "min_usd": 1.0, "expected_policy": ["ACP_ONLY", "No Policy"]},
    {"name": "Saint", "id": "019f9f75-493a-7011-b547-aa9c2df1a1ac",
     "wallet": "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d",
     "role": "Hyperliquid perps trading (self-learning TA engine)",
     "min_usd": 1.0, "expected_policy": ["No Policy", "ACP_ONLY"]},
]

# Active crons only — paused crons excluded for capital efficiency.
# Capital-guard in cron_watchdog.py auto-resumes paused crons when USDC arrives.
EXPECTED_CRONS = {
    "cron-watchdog": "15m", "deployer-watcher": "5m", "oversight": "30m",
    "sniper-guard": "10m",
    "saint-perps": "30m",
    "learning-engine": "1h",
    "treasury-engine": "15m", "volume-engine": "30m",
}
# Paused crons — not expected, won't be flagged as missing or recreated
PAUSED_CRONS = {
    "pumpfun-loop": "20m", "team-coordinator": "15m",
    "scout-ecosystem": "45m", "revenue-engine": "30m",
    "buyback-burn": "1h", "growth-engine": "2h",
}

# ─── Helpers ──────────────────────────────────────────────────────────────

def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def acp(args, timeout=15):
    """Run acp command and return (stdout, success)"""
    try:
        r = subprocess.run(["acp"] + args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode == 0
    except Exception as e:
        return str(e), False

def acp_json(args, timeout=15):
    """Run acp command and parse JSON output"""
    out, ok = acp(args, timeout)
    raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
    try:
        return json.loads(raw), True
    except Exception as e:
        return None, False

def hermes(args, timeout=15):
    """Run hermes command and return stdout"""
    try:
        r = subprocess.run([HERMES] + args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return ""

def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        return default if default is not None else {}

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

# ─── Checks ───────────────────────────────────────────────────────────────

def check_crons():
    """Check all active cron jobs are alive (paused crons excluded)"""
    out = hermes(["cron", "list"])
    live = set(re.findall(r'Name:\s+(\S+)', out))
    missing = set(EXPECTED_CRONS.keys()) - live

    # Self-heal via mesh
    if missing:
        sys.path.insert(0, WORKDIR)
        try:
            from cron_watchdog import ensure_all_crons
            recreated = ensure_all_crons()
            if recreated:
                missing = set(EXPECTED_CRONS.keys()) - set(re.findall(r'Name:\s+(\S+)', hermes(["cron", "list"])))
        except Exception:
            pass

    status = "🟢" if not missing else "🔴"
    detail = f"all {len(EXPECTED_CRONS)} active" if not missing else f"missing: {', '.join(sorted(missing))}"
    return {"check": "crons", "status": status, "detail": detail,
            "missing": sorted(missing), "total": len(live)}

def check_policies():
    """Check all agent signer policies allow autonomous operation"""
    results = []
    for agent in AGENTS:
        out, ok = acp(["agent", "use", "--agent-id", agent["id"], "--json"])
        d, ok = acp_json(["agent", "signer-policy", "--json"])
        if not ok or d is None:
            results.append({"agent": agent["name"], "status": "🔴", "policy": "unknown"})
            continue
        policy = d.get("policy", "?")
        pids = d.get("policyIds", [])

        if "no approval" in policy.lower() or pids == []:
            status = "🟢"
        elif "ACP_ONLY" in policy:
            status = "🟢"
        elif "deny" in policy.lower():
            status = "🔴"
        else:
            status = "🟡"

        results.append({"agent": agent["name"], "status": status, "policy": policy,
                        "autonomous": status == "🟢"})

    all_ok = all(r["status"] == "🟢" for r in results)
    return {"check": "policies", "status": "🟢" if all_ok else "🔴",
            "detail": ", ".join(f"{r['agent']}:{r['status']}" for r in results),
            "agents": results}

def check_balances():
    """Check all agent wallet balances above minimum thresholds"""
    results = []
    total_ecosystem = 0.0

    for agent in AGENTS:
        out, ok = acp(["agent", "use", "--agent-id", agent["id"], "--json"])
        d, ok = acp_json(["wallet", "balance", "--json"], timeout=20)
        if not ok or d is None:
            results.append({"agent": agent["name"], "status": "🔴", "usd": 0, "detail": "fetch failed"})
            continue

        total_usd = 0.0
        tokens = []
        for t in d.get("tokens", []):
            meta = t.get("tokenMetadata", {}) or {}
            sym = meta.get("symbol")
            if not sym:
                continue
            bal_raw = t.get("tokenBalance", "0")
            dec = meta.get("decimals", 18) or 18
            val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw)
            amt = val / (10 ** dec)
            price = 0
            for p in t.get("tokenPrices", []):
                if p.get("currency") == "usd":
                    price = float(p.get("value", 0))
            usd = amt * price
            if usd > 0.01:
                tokens.append({"sym": sym, "usd": usd, "amt": amt})
            total_usd += usd

        # Check HL for Saint
        hl_usd = 0
        hl_positions = 0
        if agent["name"] == "Saint":
            hd, _ = acp_json(["trade", "hl-status", "--json"], timeout=15)
            if hd:
                hl_usd = float(hd.get("accountValue", 0))
                hl_positions = len(hd.get("positions", []))
                total_usd += hl_usd

        total_ecosystem += total_usd
        status = "🟢" if total_usd >= agent["min_usd"] else "🔴"
        detail = f"${total_usd:.2f}"
        if hl_usd:
            detail += f" (HL ${hl_usd:.2f}, {hl_positions} pos)"
        top_tokens = ", ".join(f"{t['sym']} ${t['usd']:.2f}" for t in sorted(tokens, key=lambda x: -x["usd"])[:3])
        if top_tokens:
            detail += f" [{top_tokens}]"

        results.append({"agent": agent["name"], "status": status, "usd": total_usd,
                        "hl_usd": hl_usd, "hl_positions": hl_positions,
                        "detail": detail, "tokens": tokens})

    all_ok = all(r["status"] == "🟢" for r in results)
    return {"check": "balances", "status": "🟢" if all_ok else "🔴",
            "detail": f"ecosystem total ${total_ecosystem:.2f}",
            "total_usd": total_ecosystem, "agents": results}

def check_hl_positions():
    """Check Saint's HL positions — PnL, SL/TP proximity"""
    out, _ = acp(["agent", "use", "--agent-id", AGENTS[2]["id"], "--json"])
    d, ok = acp_json(["trade", "hl-status", "--json"], timeout=15)
    if not ok or d is None:
        return {"check": "hl_positions", "status": "🔴", "detail": "HL status failed"}

    positions = d.get("positions", [])
    account = float(d.get("accountValue", 0))
    withdrawable = float(d.get("withdrawable", 0))

    if not positions:
        return {"check": "hl_positions", "status": "🟡",
                "detail": f"${account:.2f} account, no positions (waiting for signals)",
                "account": account, "positions": []}

    pos_details = []
    total_pnl = 0.0
    for p in positions:
        coin = p.get("token", p.get("coin", "?"))
        size = float(p.get("size", p.get("szi", 0)))
        entry = float(p.get("entryPx", 0))
        pnl = float(p.get("unrealizedPnl", 0))
        lev = p.get("leverage", {})
        lev_val = lev.get("value", 1) if isinstance(lev, dict) else lev
        side = "long" if size > 0 else "short"
        total_pnl += pnl

        # Check against saint_positions.json SL/TP
        sl_hit = False
        tp_hit = False
        pos_log = load_json(os.path.join(WORKDIR, "saint_positions.json"), [])
        for logged in pos_log:
            if logged.get("symbol") == coin and logged.get("status") == "open":
                sl = logged.get("sl", 0)
                tp = logged.get("tp", 0)
                # We don't have current mark price from this API, just entry + PnL
                # If PnL < -3% of position value, flag as near SL
                pos_value = abs(size * entry)
                if pos_value > 0:
                    pnl_pct = pnl / pos_value * 100
                    if pnl_pct < -4:
                        sl_hit = True
                    elif pnl_pct > 8:
                        tp_hit = True
                break

        status = "🟢"
        if sl_hit:
            status = "🔴"
        elif tp_hit:
            status = "🟡"

        pos_details.append({"coin": coin, "side": side, "size": size,
                           "entry": entry, "pnl": pnl, "leverage": lev_val,
                           "status": status, "near_sl": sl_hit, "near_tp": tp_hit})

    overall = "🟢" if total_pnl >= -1 else ("🟡" if total_pnl >= -3 else "🔴")
    return {"check": "hl_positions", "status": overall,
            "detail": f"${account:.2f} | {len(positions)} pos | PnL ${total_pnl:+.2f} | withdraw ${withdrawable:.2f}",
            "account": account, "withdrawable": withdrawable,
            "total_pnl": total_pnl, "positions": pos_details}

def check_solana_wallet():
    """Check Solana sniper wallet has SOL for pump.fun buys"""
    try:
        import requests
        r = requests.post("https://api.mainnet-beta.solana.com", json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": ["CT5Z79b1ie7AkuQ2K5XRfM9XpP2g5nFh8j7Uw6xKpump"]
        }, timeout=10)
        sol = r.json()["result"]["value"] / 1e9
        status = "🟢" if sol >= 0.1 else ("🟡" if sol > 0 else "🔴")
        return {"check": "solana_wallet", "status": status,
                "detail": f"{sol:.4f} SOL (~${sol * 150:.2f})",
                "sol": sol}
    except Exception as e:
        return {"check": "solana_wallet", "status": "🔴", "detail": f"RPC error: {e}"}

def check_pumpfun_loop():
    """Check pumpfun loop config is valid and tokens are tradeable"""
    tokens = load_json(os.path.join(WORKDIR, "pumpfun_loop_tokens.json"), {})
    if not tokens:
        return {"check": "pumpfun_loop", "status": "🔴", "detail": "config missing"}
    count = len(tokens)
    # Verify each token has required fields
    valid = 0
    for sym, cfg in tokens.items():
        if "mint" in cfg and "decimals" in cfg and "min_trade_usdc" in cfg:
            valid += 1
    status = "🟢" if valid == count else "🟡"
    return {"check": "pumpfun_loop", "status": status,
            "detail": f"{valid}/{count} tokens valid ({', '.join(sorted(tokens.keys()))})",
            "tokens": sorted(tokens.keys())}

def check_revenue_flywheel():
    """Check revenue/buyback/growth engines are producing results"""
    revenue_log = load_json(os.path.join(WORKDIR, "revenue_log.json"), [])
    growth_log = load_json(os.path.join(WORKDIR, "growth_log.json"), [])
    team_state = load_json(os.path.join(WORKDIR, "team_state.json"), {})

    revenue_total = sum(e.get("amount", 0) for e in revenue_log) if isinstance(revenue_log, list) else 0
    buyback_cycles = team_state.get("armabase_buyback_cycles", 0)
    scout_jobs = team_state.get("scout_jobs_found", 0)
    scout_hired = team_state.get("scout_jobs_hired", 0)

    status = "🟢" if revenue_total > 0 or buyback_cycles > 0 else "🟡"
    return {"check": "revenue_flywheel", "status": status,
            "detail": f"revenue ${revenue_total:.2f}, buybacks {buyback_cycles}, "
                     f"scout jobs {scout_jobs}({scout_hired} hired)",
            "revenue_total": revenue_total, "buyback_cycles": buyback_cycles,
            "scout_jobs": scout_jobs, "scout_hired": scout_hired}

def check_learning_db():
    """Check Saint learning DB is healthy and tracking trades"""
    db = load_json(os.path.join(WORKDIR, "saint_learning.json"), {})
    if not db:
        return {"check": "learning_db", "status": "🔴", "detail": "saint_learning.json missing"}

    total = db.get("total_trades", 0)
    wins = db.get("wins", 0)
    losses = db.get("losses", 0)
    pnl = db.get("total_pnl", 0)
    streak = db.get("current_win_streak", 0)

    status = "🟢" if total > 0 else "🟡"
    return {"check": "learning_db", "status": status,
            "detail": f"{total} trades, {wins}W/{losses}L, PnL ${pnl:.2f}, streak {streak}",
            "total_trades": total, "wins": wins, "losses": losses, "pnl": pnl}

def check_compute():
    """Check compute balances for all agents"""
    results = []
    for agent in AGENTS:
        out, _ = acp(["agent", "use", "--agent-id", agent["id"], "--json"])
        d, ok = acp_json(["compute", "status", "--json"], timeout=10)
        if not ok or d is None:
            results.append({"agent": agent["name"], "status": "🟡", "balance": "?"})
            continue
        bal = d.get("balance", d.get("computeBalance", 0))
        try:
            bal_f = float(bal)
        except (TypeError, ValueError):
            bal_f = 0
            bal = "0"
        status = "🟢" if bal_f > 0.5 else ("🟡" if bal_f > 0.1 else "🔴")
        results.append({"agent": agent["name"], "status": status, "balance": bal})

    all_ok = all(r["status"] == "🟢" for r in results)
    return {"check": "compute", "status": "🟢" if all_ok else "🟡",
            "detail": ", ".join(f"{r['agent']}:{r['balance']}" for r in results),
            "agents": results}

# ─── Auto-Heal Actions ────────────────────────────────────────────────────

def auto_heal(checks):
    """Take automatic corrective action on detected problems"""
    actions = []

    for c in checks:
        if c["status"] == "🟢":
            continue

        if c["check"] == "crons" and c.get("missing"):
            # Already self-healed in check_crons(), just note it
            actions.append(f"🔧 Crons: attempted to recreate {', '.join(c['missing'])}")

        elif c["check"] == "balances":
            for a in c.get("agents", []):
                if a["status"] == "🔴":
                    # Try to route funds from richest agent
                    richest = max(c["agents"], key=lambda x: x.get("usd", 0))
                    if richest["usd"] > a["usd"] + 5:
                        actions.append(
                            f"💰 Balance: {a['agent']} low (${a['usd']:.2f}), "
                            f"could route from {richest['agent']} (${richest['usd']:.2f})"
                        )
                    else:
                        actions.append(
                            f"⚠️ Balance: {a['agent']} low (${a['usd']:.2f}), "
                            f"no agent has enough to cover — user needs to fund"
                        )

        elif c["check"] == "solana_wallet" and c.get("sol", 0) < 0.1:
            actions.append(
                f"⚠️ Solana: sniper wallet has {c['sol']:.4f} SOL — "
                f"user needs to send SOL to CT5Z79b1ie7A..."
            )

        elif c["check"] == "hl_positions":
            for p in c.get("positions", []):
                if p.get("near_sl"):
                    actions.append(
                        f"🚨 HL: {p['coin']} {p['side']} near SL (PnL ${p['pnl']:+.2f}) — "
                        f"saint_perps.py will close on next cycle"
                    )
                elif p.get("near_tp"):
                    actions.append(
                        f"🎯 HL: {p['coin']} {p['side']} near TP (PnL ${p['pnl']:+.2f}) — "
                        f"saint_perps.py will take profit on next cycle"
                    )

        elif c["check"] == "policies":
            for a in c.get("agents", []):
                if a["status"] == "🔴":
                    actions.append(
                        f"⛔ Policy: {a['agent']} on '{a['policy']}' — "
                        f"user must change to ACP_ONLY via dashboard"
                    )

    return actions

# ─── Main ─────────────────────────────────────────────────────────────────

def run():
    print("=" * 72)
    print(f"  ARMA BASE OVERSIGHT DASHBOARD — {now()}")
    print("=" * 72)

    # Run all checks
    checks = []

    # 1. Cron health (self-heals inline)
    print("\n📋 Checking cron health...")
    checks.append(check_crons())

    # 2. Agent policies
    print("📋 Checking agent policies...")
    checks.append(check_policies())

    # 3. Wallet balances
    print("📋 Checking wallet balances...")
    checks.append(check_balances())

    # 4. HL positions
    print("📋 Checking Saint HL positions...")
    checks.append(check_hl_positions())

    # 5. Solana wallet
    print("📋 Checking Solana sniper wallet...")
    checks.append(check_solana_wallet())

    # 6. Pump.fun loop
    print("📋 Checking pump.fun loop config...")
    checks.append(check_pumpfun_loop())

    # 7. Revenue flywheel
    print("📋 Checking revenue flywheel...")
    checks.append(check_revenue_flywheel())

    # 8. Learning DB
    print("📋 Checking learning databases...")
    checks.append(check_learning_db())

    # 9. Compute
    print("📋 Checking compute balances...")
    checks.append(check_compute())

    # Auto-heal
    print("\n📋 Running auto-heal analysis...")
    actions = auto_heal(checks)

    # Print dashboard
    print("\n" + "=" * 72)
    print("  SYSTEM STATUS")
    print("=" * 72)

    red_count = sum(1 for c in checks if c["status"] == "🔴")
    yellow_count = sum(1 for c in checks if c["status"] == "🟡")
    green_count = sum(1 for c in checks if c["status"] == "🟢")

    for c in checks:
        print(f"  {c['status']} {c['check']:20s} {c['detail']}")

    print(f"\n  Summary: {green_count} green, {yellow_count} yellow, {red_count} red")

    if actions:
        print(f"\n{'─' * 72}")
        print("  AUTO-HEAL ACTIONS")
        print("─" * 72)
        for a in actions:
            print(f"  {a}")
    else:
        print(f"\n  ✅ No auto-heal actions needed")

    # Save state + alerts
    state = {
        "timestamp": now(),
        "checks": checks,
        "actions": actions,
        "summary": {"green": green_count, "yellow": yellow_count, "red": red_count},
    }
    save_json(STATE_PATH, state)

    # Save alerts for user review (only red/yellow issues)
    alerts = [c for c in checks if c["status"] != "🟢"]
    save_json(ALERTS_PATH, {
        "timestamp": now(),
        "alerts": alerts,
        "actions": actions,
    })

    # Print summary line for cron delivery
    print(f"\n{'=' * 72}")
    if red_count > 0:
        print(f"  ⚠️ {red_count} CRITICAL issue(s) need attention")
    elif yellow_count > 0:
        print(f"  ℹ️ {yellow_count} warning(s), system operational")
    else:
        print(f"  ✅ All systems green — ecosystem healthy")
    print("=" * 72)

    return 0 if red_count == 0 else 1


# ─── Self-Healing Cron Mesh ──────────────────────────────────────────────
import sys as _sys, os as _os
from goal_tracker import check_goal
_sys.path.insert(0, "/workspace")
try:
    from cron_watchdog import ensure_all_crons
    _recreated = ensure_all_crons()
    if _recreated:
        print(f"  🔧 Self-healed crons: {', '.join(sorted(_recreated))}")
except Exception as _e:
    pass
# ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Check $1M goal — halt if achieved
    try:
        if check_goal():
            sys.exit(0)
    except Exception as e:
        print(f"Goal check skipped: {e}")
    sys.exit(run())
