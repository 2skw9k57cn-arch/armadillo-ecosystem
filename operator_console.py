#!/usr/bin/env python3
"""Streamlit operator console for the Armadillo control plane."""
import json
import os
import subprocess
from datetime import datetime, timezone

import streamlit as st

from control_plane import (
    ACTION_QUEUE_PATH,
    GOAL_STATE_PATH,
    LAUNCHER_STATE_PATH,
    LEARNED_PARAMS_PATH,
    OVERSIGHT_ALERTS_PATH,
    OVERSIGHT_STATE_PATH,
    PERFORMANCE_PATH,
    RISK_STATE_PATH,
    SAINT_LEARNING_PATH,
    TARGETS,
    TEAM_STATE_PATH,
    TRADE_LOG_PATH,
    WORKDIR,
    evaluate_risk,
    flatten_trades,
    load_json,
)


def fmt_age(value):
    if value is None:
        return "n/a"
    return f"{value:.0f} min"


def live_cron_rows():
    hermes = "/opt/hermes-agent/venv/bin/hermes"
    if not os.path.exists(hermes):
        return []
    try:
        result = subprocess.run([hermes, "cron", "list"], capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    rows = []
    current = {}
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            current[key.strip().lower()] = value.strip()
    if current:
        rows.append(current)
    return rows


def render():
    st.set_page_config(page_title="Armadillo Control Plane", layout="wide")
    st.title("Armadillo Control Plane")
    st.caption(f"Workspace: {WORKDIR}")

    if st.button("Refresh risk state", type="primary"):
        evaluate_risk(write_files=True)

    risk = load_json(RISK_STATE_PATH, None) or evaluate_risk(write_files=True)
    actions = load_json(ACTION_QUEUE_PATH, {"actions": []})
    performance = load_json(PERFORMANCE_PATH, {"scores": {}})
    oversight = load_json(OVERSIGHT_STATE_PATH, {})
    oversight_alerts = load_json(OVERSIGHT_ALERTS_PATH, {})
    launcher = load_json(LAUNCHER_STATE_PATH, {"runs": []})
    goal = load_json(GOAL_STATE_PATH, {})
    saint = load_json(SAINT_LEARNING_PATH, {})
    learned = load_json(LEARNED_PARAMS_PATH, {})
    team = load_json(TEAM_STATE_PATH, {})
    trades = flatten_trades(load_json(TRADE_LOG_PATH, []))

    summary = risk.get("summary", {})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Risk", f"{summary.get('emoji', '⚪')} {summary.get('status', 'unknown')}")
    m2.metric("Capital", f"${summary.get('total_usd', 0):.2f}")
    m3.metric("SOL gas", f"{summary.get('sol_balance', 0):.4f}")
    m4.metric("Interventions", summary.get("blocking_actions", 0) + summary.get("degraded_actions", 0))

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Control", "Strategies", "Alerts", "Trades", "Learning"])

    with tab1:
        left, right = st.columns((2, 1))
        with left:
            st.subheader("Global flags")
            if risk.get("global_flags"):
                for flag in risk["global_flags"]:
                    st.write(f"{flag['severity'].upper()} — {flag['message']}")
            else:
                st.success("No global risk flags.")

            st.subheader("Action queue")
            if actions.get("actions"):
                st.dataframe(actions["actions"], use_container_width=True)
            else:
                st.success("No queued actions.")

            st.subheader("Launcher history")
            st.dataframe(list(reversed(launcher.get("runs", [])[-20:])), use_container_width=True)

        with right:
            st.subheader("Goal progress")
            st.json(goal or {"status": "no goal state yet"})

            st.subheader("Configured strategies")
            st.json({name: cfg["script"] for name, cfg in TARGETS.items()})

            rows = live_cron_rows()
            st.subheader("Live cron view")
            if rows:
                st.dataframe(rows, use_container_width=True)
            else:
                st.info("Hermes cron output unavailable from this environment.")

    with tab2:
        st.subheader("Strategy decisions")
        strategy_rows = []
        for name, decision in risk.get("strategies", {}).items():
            score = performance.get("scores", {}).get(name, {})
            strategy_rows.append({
                "strategy": name,
                "decision": decision.get("decision"),
                "mode": decision.get("mode"),
                "size_mult": decision.get("recommended_size_mult"),
                "score": score.get("score"),
                "score_status": score.get("status"),
                "reasons": "; ".join(decision.get("reasons", [])),
            })
        st.dataframe(strategy_rows, use_container_width=True)

        st.subheader("Performance scores")
        st.json(performance.get("scores", {}))

    with tab3:
        st.subheader("Oversight alerts")
        st.json(oversight_alerts or {"alerts": []})

        st.subheader("Oversight summary")
        st.json(oversight.get("summary", {}))

        st.subheader("Team state")
        st.json({
            "last_run": team.get("last_run"),
            "cycles": team.get("cycles"),
            "revenue_generated": team.get("revenue_generated"),
            "profit_withdrawn": team.get("profit_withdrawn"),
            "history_count": len(team.get("history", [])) if isinstance(team.get("history"), list) else 0,
        })

    with tab4:
        st.subheader("Recent trade log")
        recent_trades = trades[-50:]
        st.dataframe(list(reversed(recent_trades)), use_container_width=True)

        realized = []
        for trade in trades:
            if isinstance(trade.get("pnl"), (int, float)):
                realized.append(float(trade["pnl"]))
            elif isinstance(trade.get("usd_out"), (int, float)) and isinstance(trade.get("entry_cost"), (int, float)):
                realized.append(float(trade["usd_out"]) - float(trade["entry_cost"]))
        st.metric("Observed realized PnL", f"${sum(realized):.2f}")

    with tab5:
        left, right = st.columns(2)
        with left:
            st.subheader("Saint learning")
            st.json({
                "total_trades": saint.get("total_trades", 0),
                "wins": saint.get("wins", 0),
                "losses": saint.get("losses", 0),
                "total_pnl": saint.get("total_pnl", 0),
                "loss_streak": saint.get("current_loss_streak", 0),
                "win_streak": saint.get("current_win_streak", 0),
                "adaptive_params": saint.get("adaptive_params", {}),
                "lessons": saint.get("lessons", [])[-5:],
            })
        with right:
            st.subheader("Loop learnings")
            token_items = learned.get("tokens", {}) if isinstance(learned, dict) else {}
            st.json({
                "generated_at": learned.get("generated_at") if isinstance(learned, dict) else None,
                "total_trades": learned.get("total_trades") if isinstance(learned, dict) else None,
                "win_rate": learned.get("win_rate") if isinstance(learned, dict) else None,
                "global_lessons": learned.get("global_lessons", [])[-5:] if isinstance(learned, dict) else [],
                "tracked_tokens": list(token_items.keys())[:10],
            })


if __name__ == "__main__":
    render()

