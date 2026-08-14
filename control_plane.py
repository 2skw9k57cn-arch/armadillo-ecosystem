#!/usr/bin/env python3
"""
Lightweight control plane for the Armadillo ecosystem.

Evaluates recent state, produces strategy decisions, and writes:
  - /workspace/risk_state.json
  - /workspace/action_queue.json
  - /workspace/performance_scores.json
"""
import json
import os
from datetime import datetime, timedelta, timezone


WORKDIR = os.environ.get("ARMADILLO_WORKDIR", "/workspace")
SCRIPT_ROOT = os.environ.get("ARMADILLO_SCRIPT_ROOT", os.path.dirname(os.path.abspath(__file__)))

RISK_STATE_PATH = os.path.join(WORKDIR, "risk_state.json")
ACTION_QUEUE_PATH = os.path.join(WORKDIR, "action_queue.json")
PERFORMANCE_PATH = os.path.join(WORKDIR, "performance_scores.json")
LAUNCHER_STATE_PATH = os.path.join(WORKDIR, "launcher_state.json")
OVERSIGHT_STATE_PATH = os.path.join(WORKDIR, "oversight_state.json")
OVERSIGHT_ALERTS_PATH = os.path.join(WORKDIR, "oversight_alerts.json")
TEAM_STATE_PATH = os.path.join(WORKDIR, "team_state.json")
TRADE_LOG_PATH = os.path.join(WORKDIR, "sol_trade_log.json")
LEARNED_PARAMS_PATH = os.path.join(WORKDIR, "learned_params.json")
SAINT_LEARNING_PATH = os.path.join(WORKDIR, "saint_learning.json")
GOAL_STATE_PATH = os.path.join(WORKDIR, "goal_state.json")
DEPLOYER_SEEN_PATH = os.path.join(WORKDIR, "deployer_seen_tokens.json")
SNIPER_BAGS_PATH = os.path.join(WORKDIR, "sniper_bags.json")

MANUAL_APPROVAL_PATTERNS = (
    "manual approval",
    "pending approval",
    "approve-transaction",
    "wallet_preparecalls",
)
ROUTE_FAILURE_PATTERNS = (
    "transfer_failed",
    "compute_topup_failed",
    "bonding curve paused",
    "price impact too high",
    "rpc error",
    "quote error",
    "send:",
)

TARGETS = {
    "deployer-watcher": {
        "label": "Deployer watcher",
        "script": "deployer_watcher.py",
        "requires_sol": 0.01,
        "capital_floor": 1.0,
        "supports_probation": True,
    },
    "sniper-guard": {
        "label": "Sniper guard",
        "script": "sniper_guard.py",
        "requires_sol": 0.005,
        "capital_floor": 0.5,
        "supports_probation": False,
    },
    "saint-perps": {
        "label": "Saint perps",
        "script": "saint_perps.py",
        "capital_floor": 3.0,
        "supports_probation": True,
    },
    "pumpfun-loop": {
        "label": "Pump.fun loop",
        "script": "pumpfun_multi_loop.py",
        "requires_sol": 0.01,
        "capital_floor": 3.0,
        "supports_probation": True,
    },
    "team-coordinator": {
        "label": "Team coordinator",
        "script": "team_coordinator.py",
        "capital_floor": 5.0,
        "supports_probation": False,
    },
    "profit-engine": {
        "label": "Profit engine",
        "script": "profit_engine.py",
        "capital_floor": 2.1,
        "supports_probation": True,
    },
    "scout-ecosystem": {
        "label": "Scout ecosystem",
        "script": "scout_ecosystem.py",
        "capital_floor": 2.0,
        "supports_probation": True,
    },
    "revenue-engine": {
        "label": "Revenue engine",
        "script": "revenue_engine.py",
        "capital_floor": 2.0,
        "supports_probation": False,
    },
    "growth-engine": {
        "label": "Growth engine",
        "script": "growth_engine.py",
        "capital_floor": 3.0,
        "supports_probation": True,
    },
    "volume-engine": {
        "label": "Volume engine",
        "script": "volume_engine.py",
        "capital_floor": 1.0,
        "supports_probation": True,
    },
}


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def parse_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def age_minutes_from_timestamp(value):
    ts = parse_timestamp(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc() - ts.astimezone(timezone.utc)).total_seconds() / 60.0)


def file_age_minutes(path):
    try:
        return max(0.0, (now_utc() - datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return None


def clamp(value, low, high):
    return max(low, min(high, value))


def flatten_trades(raw):
    if isinstance(raw, dict):
        trades = raw.get("trades", [])
        return trades if isinstance(trades, list) else []
    if isinstance(raw, list):
        return raw
    return []


def trade_pnl(entry):
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("pnl"), (int, float)):
        return float(entry["pnl"])
    usd_out = entry.get("usd_out")
    entry_cost = entry.get("entry_cost")
    if isinstance(usd_out, (int, float)) and isinstance(entry_cost, (int, float)):
        return float(usd_out) - float(entry_cost)
    return None


def recent_trades(trades, hours=24, fallback=50):
    cutoff = now_utc() - timedelta(hours=hours)
    with_time = []
    without_time = []
    for entry in trades:
        ts = parse_timestamp(entry.get("time") or entry.get("timestamp"))
        if ts is None:
            without_time.append(entry)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(timezone.utc) >= cutoff:
            with_time.append(entry)
    return with_time if with_time else trades[-fallback:]


def summarize_sol_trades():
    trades = flatten_trades(load_json(TRADE_LOG_PATH, []))
    recent = recent_trades(trades, hours=24, fallback=60)
    realized = [trade_pnl(t) for t in recent]
    realized = [p for p in realized if p is not None]
    losses = sum(1 for p in realized if p < 0)
    wins = sum(1 for p in realized if p > 0)
    route_failures = 0
    for trade in recent:
        text = json.dumps(trade).lower()
        if any(pattern in text for pattern in ROUTE_FAILURE_PATTERNS):
            route_failures += 1
    return {
        "total_trades": len(trades),
        "recent_trades": len(recent),
        "realized_pnl": round(sum(realized), 4),
        "wins": wins,
        "losses": losses,
        "loss_streak": recent_loss_streak(realized),
        "route_failures": route_failures,
    }


def recent_loss_streak(pnls):
    streak = 0
    for pnl in reversed(pnls):
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def summarize_saint_learning():
    db = load_json(SAINT_LEARNING_PATH, {})
    total = int(db.get("total_trades", 0) or 0)
    wins = int(db.get("wins", 0) or 0)
    losses = int(db.get("losses", 0) or 0)
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "total_pnl": float(db.get("total_pnl", 0.0) or 0.0),
        "loss_streak": int(db.get("current_loss_streak", 0) or 0),
        "win_streak": int(db.get("current_win_streak", 0) or 0),
        "confidence_threshold": int(db.get("adaptive_params", {}).get("confidence_threshold", 50) or 50),
        "lessons": db.get("lessons", [])[-5:],
    }


def summarize_team_routes():
    state = load_json(TEAM_STATE_PATH, {})
    history = state.get("history", [])[-40:] if isinstance(state.get("history"), list) else []
    pending_approvals = 0
    transfer_failures = 0
    compute_failures = 0
    for entry in history:
        action = str(entry.get("action", ""))
        detail_text = json.dumps(entry.get("details", {})).lower()
        if action == "transfer_pending_approval" or any(p in detail_text for p in MANUAL_APPROVAL_PATTERNS):
            pending_approvals += 1
        if action == "transfer_failed":
            transfer_failures += 1
        if action == "compute_topup_failed":
            compute_failures += 1
    return {
        "pending_approvals": pending_approvals,
        "transfer_failures": transfer_failures,
        "compute_failures": compute_failures,
        "last_run_age_min": age_minutes_from_timestamp(state.get("last_run")),
    }


def find_check(checks, name):
    for check in checks:
        if check.get("check") == name:
            return check
    return {}


def load_oversight_snapshot():
    state = load_json(OVERSIGHT_STATE_PATH, {})
    checks = state.get("checks", []) if isinstance(state.get("checks"), list) else []
    balances = find_check(checks, "balances")
    solana = find_check(checks, "solana_wallet")
    compute = find_check(checks, "compute")
    return {
        "state": state,
        "checks": checks,
        "balances": balances,
        "solana": solana,
        "compute": compute,
        "alerts": load_json(OVERSIGHT_ALERTS_PATH, {}).get("alerts", []),
        "actions": load_json(OVERSIGHT_ALERTS_PATH, {}).get("actions", []),
    }


def build_performance_scores():
    sol = summarize_sol_trades()
    saint = summarize_saint_learning()
    routes = summarize_team_routes()
    deployer = load_json(DEPLOYER_SEEN_PATH, {"total_detected": 0, "total_sniped": 0})
    sniper = load_json(SNIPER_BAGS_PATH, {})

    saint_score = 50
    saint_score += clamp((saint["win_rate"] - 50.0) * 0.6, -20, 20)
    saint_score += clamp(saint["total_pnl"] * 2.0, -15, 15)
    saint_score -= saint["loss_streak"] * 8
    saint_score = int(clamp(round(saint_score), 0, 100))

    loop_score = 45
    loop_score += clamp(sol["realized_pnl"] * 3.0, -15, 15)
    loop_score += clamp((sol["wins"] - sol["losses"]) * 4, -20, 20)
    loop_score -= sol["route_failures"] * 5
    loop_score = int(clamp(round(loop_score), 0, 100))

    deployer_score = 40
    deployer_score += min(20, int(deployer.get("total_detected", 0) or 0))
    deployer_score += min(20, int(deployer.get("total_sniped", 0) or 0) * 2)
    deployer_score -= routes["pending_approvals"] * 4
    deployer_score = int(clamp(deployer_score, 0, 100))

    sniper_score = 45
    sniper_score += clamp(float(sniper.get("total_realized_pnl", 0.0) or 0.0) * 3.0, -15, 15)
    sniper_score += min(15, len(sniper.get("sells", [])[-10:]))
    sniper_score = int(clamp(round(sniper_score), 0, 100))

    volume_score = 45 + min(20, sol["recent_trades"])
    volume_score -= sol["route_failures"] * 4
    volume_score = int(clamp(volume_score, 0, 100))

    scores = {
        "saint-perps": {
            "score": saint_score,
            "status": score_status(saint_score),
            "metrics": saint,
        },
        "pumpfun-loop": {
            "score": loop_score,
            "status": score_status(loop_score),
            "metrics": sol,
        },
        "deployer-watcher": {
            "score": deployer_score,
            "status": score_status(deployer_score),
            "metrics": {
                "total_detected": int(deployer.get("total_detected", 0) or 0),
                "total_sniped": int(deployer.get("total_sniped", 0) or 0),
                "pending_approvals": routes["pending_approvals"],
            },
        },
        "sniper-guard": {
            "score": sniper_score,
            "status": score_status(sniper_score),
            "metrics": {
                "realized_pnl": float(sniper.get("total_realized_pnl", 0.0) or 0.0),
                "recent_sells": len(sniper.get("sells", [])[-10:]),
            },
        },
        "volume-engine": {
            "score": volume_score,
            "status": score_status(volume_score),
            "metrics": {
                "recent_trades": sol["recent_trades"],
                "route_failures": sol["route_failures"],
            },
        },
    }
    return scores


def score_status(score):
    if score >= 70:
        return "strong"
    if score >= 45:
        return "probation"
    return "weak"


def evaluate_risk(write_files=True):
    oversight = load_oversight_snapshot()
    balances = oversight["balances"]
    solana = oversight["solana"]
    routes = summarize_team_routes()
    scores = build_performance_scores()
    sol_summary = summarize_sol_trades()
    saint_summary = summarize_saint_learning()
    launcher_state = load_json(LAUNCHER_STATE_PATH, {"runs": []})

    total_usd = float(balances.get("total_usd", 0.0) or 0.0)
    sol_balance = float(solana.get("sol", 0.0) or 0.0)
    oversight_age = file_age_minutes(OVERSIGHT_STATE_PATH)
    learning_age = file_age_minutes(SAINT_LEARNING_PATH)
    trade_log_age = file_age_minutes(TRADE_LOG_PATH)

    global_flags = []
    if total_usd < 3.0:
        global_flags.append({
            "severity": "red",
            "code": "low_capital",
            "message": f"Ecosystem capital is low (${total_usd:.2f}); keep high-burn loops paused.",
        })
    elif total_usd < 8.0:
        global_flags.append({
            "severity": "yellow",
            "code": "thin_capital",
            "message": f"Ecosystem capital is thin (${total_usd:.2f}); prefer smaller, higher-conviction runs.",
        })

    if sol_balance < 0.005:
        global_flags.append({
            "severity": "red",
            "code": "low_sol_gas",
            "message": f"Solana gas reserve is nearly empty ({sol_balance:.4f} SOL).",
        })
    elif sol_balance < 0.02:
        global_flags.append({
            "severity": "yellow",
            "code": "thin_sol_gas",
            "message": f"Solana gas reserve is thin ({sol_balance:.4f} SOL).",
        })

    if routes["pending_approvals"] > 0:
        global_flags.append({
            "severity": "yellow",
            "code": "manual_approval_backlog",
            "message": f"{routes['pending_approvals']} route(s) are waiting on manual approval.",
        })

    if routes["transfer_failures"] >= 2:
        global_flags.append({
            "severity": "red",
            "code": "route_failures",
            "message": f"{routes['transfer_failures']} recent internal transfer failures detected.",
        })

    if oversight_age is None or oversight_age > 120:
        global_flags.append({
            "severity": "yellow",
            "code": "stale_oversight",
            "message": "Oversight snapshot is stale; treat automation decisions cautiously.",
        })

    if learning_age is None or learning_age > 720:
        global_flags.append({
            "severity": "yellow",
            "code": "stale_learning",
            "message": "Learning state is stale; refresh before increasing risk.",
        })

    if trade_log_age is not None and trade_log_age > 720:
        global_flags.append({
            "severity": "yellow",
            "code": "stale_trade_log",
            "message": "Trade log is stale; recent performance may be under-observed.",
        })

    strategy_decisions = {}
    actions = []
    for name, cfg in TARGETS.items():
        strategy_score = scores.get(name, {"score": 50, "status": "probation", "metrics": {}})
        decision = {
            "strategy": name,
            "label": cfg["label"],
            "decision": "allow",
            "mode": "live",
            "priority": "normal",
            "recommended_size_mult": 1.0,
            "reasons": [],
            "script": cfg["script"],
        }

        if total_usd < cfg.get("capital_floor", 0):
            decision["decision"] = "block"
            decision["mode"] = "paused"
            decision["priority"] = "high"
            decision["reasons"].append(f"capital below ${cfg['capital_floor']:.2f} floor")

        if cfg.get("requires_sol") and sol_balance < cfg["requires_sol"]:
            decision["decision"] = "block"
            decision["mode"] = "paused"
            decision["priority"] = "high"
            decision["reasons"].append(f"sol reserve below {cfg['requires_sol']:.4f} SOL")

        if routes["pending_approvals"] > 0 and name in {"team-coordinator", "revenue-engine", "growth-engine"}:
            if decision["decision"] != "block":
                decision["decision"] = "degrade"
                decision["mode"] = "manual-review"
                decision["priority"] = "high"
            decision["reasons"].append("manual approval backlog on internal routing")

        if name == "saint-perps":
            if saint_summary["loss_streak"] >= 4:
                decision["decision"] = "block"
                decision["mode"] = "paused"
                decision["priority"] = "high"
                decision["recommended_size_mult"] = 0.0
                decision["reasons"].append("four straight HL losses")
            elif saint_summary["loss_streak"] >= 3:
                if decision["decision"] != "block":
                    decision["decision"] = "degrade"
                    decision["mode"] = "reduced-risk"
                decision["recommended_size_mult"] = 0.3
                decision["reasons"].append("loss streak triggered reduced HL sizing")
            elif saint_summary["total_trades"] < 3 and cfg["supports_probation"]:
                if decision["decision"] == "allow":
                    decision["decision"] = "paper_only"
                    decision["mode"] = "probation"
                    decision["recommended_size_mult"] = 0.0
                decision["reasons"].append("not enough HL trade history for live scaling")

        if name in {"pumpfun-loop", "profit-engine", "growth-engine", "volume-engine"}:
            if sol_summary["loss_streak"] >= 4 and sol_summary["realized_pnl"] < 0:
                decision["decision"] = "block"
                decision["mode"] = "paused"
                decision["priority"] = "high"
                decision["recommended_size_mult"] = 0.0
                decision["reasons"].append("recent Solana loop losses exceeded tolerance")
            elif sol_summary["loss_streak"] >= 2 or sol_summary["route_failures"] >= 2:
                if decision["decision"] == "allow":
                    decision["decision"] = "degrade"
                    decision["mode"] = "reduced-risk"
                decision["recommended_size_mult"] = 0.5
                decision["reasons"].append("recent Solana execution quality is weak")
            elif sol_summary["recent_trades"] < 4 and cfg["supports_probation"]:
                if decision["decision"] == "allow":
                    decision["decision"] = "paper_only"
                    decision["mode"] = "probation"
                    decision["recommended_size_mult"] = 0.0
                decision["reasons"].append("insufficient recent Solana trade evidence")

        if strategy_score["status"] == "weak" and decision["decision"] == "allow":
            decision["decision"] = "degrade"
            decision["mode"] = "reduced-risk"
            decision["recommended_size_mult"] = min(decision["recommended_size_mult"], 0.5)
            decision["reasons"].append("performance score is weak")

        strategy_decisions[name] = decision
        if decision["decision"] != "allow":
            actions.append({
                "strategy": name,
                "action": decision["decision"],
                "mode": decision["mode"],
                "priority": decision["priority"],
                "reasons": decision["reasons"],
            })

    launcher_runs = launcher_state.get("runs", [])
    recent_failures = [r for r in launcher_runs[-20:] if int(r.get("returncode", 0) or 0) != 0]
    if len(recent_failures) >= 3:
        global_flags.append({
            "severity": "yellow",
            "code": "launcher_failures",
            "message": f"{len(recent_failures)} recent launcher failures detected.",
        })

    severity_rank = {"green": 0, "yellow": 1, "red": 2}
    worst = "green"
    for flag in global_flags:
        if severity_rank[flag["severity"]] > severity_rank[worst]:
            worst = flag["severity"]
    for decision in strategy_decisions.values():
        if decision["decision"] == "block":
            worst = "red"
            break
        if decision["decision"] in {"degrade", "paper_only"} and worst == "green":
            worst = "yellow"

    risk_state = {
        "generated_at": now_iso(),
        "workdir": WORKDIR,
        "summary": {
            "status": worst,
            "emoji": {"green": "🟢", "yellow": "🟡", "red": "🔴"}[worst],
            "total_usd": round(total_usd, 4),
            "sol_balance": round(sol_balance, 6),
            "blocking_actions": sum(1 for d in strategy_decisions.values() if d["decision"] == "block"),
            "degraded_actions": sum(1 for d in strategy_decisions.values() if d["decision"] in {"degrade", "paper_only"}),
        },
        "global_flags": global_flags,
        "strategies": strategy_decisions,
        "performance_scores": scores,
        "oversight_age_min": oversight_age,
        "learning_age_min": learning_age,
        "trade_log_age_min": trade_log_age,
    }

    action_queue = {
        "generated_at": risk_state["generated_at"],
        "summary": risk_state["summary"],
        "actions": actions,
    }

    if write_files:
        save_json(RISK_STATE_PATH, risk_state)
        save_json(ACTION_QUEUE_PATH, action_queue)
        save_json(PERFORMANCE_PATH, {
            "generated_at": risk_state["generated_at"],
            "scores": scores,
        })
    return risk_state


def record_launcher_event(strategy, decision, mode, returncode=0, detail=""):
    state = load_json(LAUNCHER_STATE_PATH, {"runs": []})
    entry = {
        "timestamp": now_iso(),
        "strategy": strategy,
        "decision": decision,
        "mode": mode,
        "returncode": int(returncode),
        "detail": str(detail)[:400],
    }
    state["runs"].append(entry)
    state["runs"] = state["runs"][-100:]
    save_json(LAUNCHER_STATE_PATH, state)
    return entry

