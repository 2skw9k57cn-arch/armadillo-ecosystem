#!/usr/bin/env bash
# Self-Learning Profit Engine — Multi-Agent
# Runs as ANY ecosystem agent with full LLM reasoning for autonomous profit-seeking.
#
# Usage: profit-engine-agent.sh <agent_id> <agent_name>
#
# Each cycle:
# 1. Switches to target agent
# 2. Collects wallet, HL markets, marketplace opportunities, ecosystem prices
# 3. Agent analyzes and decides: buy/sell ecosystem tokens, HL perps, marketplace jobs
# 4. Executes trades, logs results to learning history
#
# The script collects state, then the AI agent (cron) takes over for decisions.

set -eo pipefail
export LC_ALL=C


# Resolve home dir robustly (works in cron context where $HOME may be unset)
if [ -n "${HOME:-}" ] && [ -d "$HOME" ] && [ "$HOME" != "/" ]; then
    HERMES_HOME="$HOME"
else
    HERMES_HOME=$(getent passwd "$(whoami)" 2>/dev/null | cut -d: -f6)
    HERMES_HOME="${HERMES_HOME:-/home/hermes}"
fi
export PYTHONPATH="${HERMES_HOME}/.hermes/home/.local/lib/python3.11/site-packages:${PYTHONPATH:-}"

AGENT_ID="${1:-019f9f75-130e-75fc-9459-5358c8d25206}"
AGENT_NAME="${2:-OGSAINT}"
HISTORY_FILE="$HOME/.hermes/data/profit-engine-${AGENT_NAME}.jsonl"

echo "=== ${AGENT_NAME} Profit Engine — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Switch to target agent
acp agent use --agent-id "${AGENT_ID}" --json >/dev/null 2>&1

# --- WALLET STATE ---
echo "--- WALLET ---"
acp wallet balance --json 2>/dev/null | python3 $HOME/.hermes/scripts/parse-wallet.py 2>&1

# --- ECOSYSTEM TOKEN PRICES ---
echo "--- ECOSYSTEM STATUS ---"
for VID_INFO in "123348:OGSAINT" "127458:ARBA" "123305:ARMAD" "124146:ARRB"; do
    VID=$(echo "$VID_INFO" | cut -d: -f1)
    SYM=$(echo "$VID_INFO" | cut -d: -f2)
    curl -s "https://api.virtuals.io/api/virtuals/$VID" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin).get('data', {})
    print(f'  {d.get(\"symbol\",\"$SYM\")}: status={d.get(\"status\")}, vol24h={d.get(\"netVolume24h\",0)}')
except: print(f'  $SYM: fetch failed')
" 2>&1
done

# --- HL MARKET SCAN ---
echo "--- HL OPPORTUNITIES ---"
curl -s "https://api.hyperliquid.xyz/info" -X POST -H "Content-Type: application/json" -d '{"type":"metaAndAssetCtxs"}' 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    universe = data[0] if isinstance(data, list) else {}
    ctxs = data[1] if isinstance(data, list) and len(data) > 1 else []
    uni_list = universe.get('universe', []) if isinstance(universe, dict) else universe
    opportunities = []
    for i, ctx in enumerate(ctxs):
        if i >= len(uni_list): break
        u = uni_list[i]
        name = u.get('name', '?')
        funding = float(ctx.get('funding', '0') or '0')
        vol24h = float(ctx.get('dayNtlVlm', '0') or '0')
        price = ctx.get('markPx', '?')
        oi = float(ctx.get('openInterest', '0') or '0')
        if vol24h > 5000000 or abs(funding) > 0.0005:
            opportunities.append((name, price, funding, vol24h, oi))
    opportunities.sort(key=lambda x: x[3], reverse=True)
    for name, price, fund, vol, oi in opportunities[:8]:
        sig = '🔥HIGH FUNDING' if abs(fund) > 0.001 else ''
        print(f'  {name:12s} \${price:>10} funding={fund:.6f} vol=\${vol:>12,.0f} {sig}')
except Exception as e:
    print(f'  Error: {e}')
" 2>&1

# --- MARKETPLACE OPPORTUNITIES ---
echo "--- MARKETPLACE JOBS ---"
acp browse "trading analysis crypto research market sentiment token" --top-k 5 --online all --json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    agents = data.get('data', [])
    count = 0
    for a in agents[:5]:
        name = a.get('name', '?')
        for o in a.get('offerings', [])[:2]:
            price = o.get('priceValue', '?')
            oname = o.get('name', '?')
            print(f'  {name}: {oname} @ \${price}')
            count += 1
    if count == 0: print('  No jobs found')
except: print('  Browse failed')
" 2>&1

# --- PENDING EVENTS ---
echo "--- PENDING EVENTS ---"
EVENTS_FILE="/tmp/acp-events-${AGENT_NAME}.jsonl"
if [ -f "$EVENTS_FILE" ]; then
    acp events drain --file "$EVENTS_FILE" --limit 10 --json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    events = data.get('events', [])
    print(f'  {len(events)} events')
    for e in events:
        print(f'    Job {e.get(\"jobId\")}: {e.get(\"status\")} tools={e.get(\"availableTools\",[])}')
except: print('  No events')
" 2>&1
else
    echo "  No event listener for this agent"
fi

# --- LEARNING HISTORY ---
echo "--- LEARNING HISTORY ---"
if [ -f "$HISTORY_FILE" ]; then
    tail -5 "$HISTORY_FILE" 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line.strip())
        print(f\"  {d.get('ts','?')[:16]}: {d.get('action','?')} → {d.get('result','?')} P&L={d.get('pnl','?')}\")
    except: pass
" 2>&1
else
    echo "  No history yet"
fi

echo ""
echo "=== AGENT INSTRUCTIONS FOR ${AGENT_NAME} ==="
echo "You are ${AGENT_NAME}, an autonomous profit-seeking agent in the Armadillo ecosystem."
echo "THE ECOSYSTEM NEVER STOPS. You run every hour, forever. Every cycle you MUST:"
echo "1. BUY an ecosystem token (rotate: OGSAINT/ARMAD/ARBA/ARRB) if you have ≥\$2 USDC"
echo "2. SELL a different ecosystem token back to USDC (need ≥\$2 value, sell 20-80%)"
echo "3. HL PERP TRADES — If funding is extreme (>|0.001|), open a counter-position. Max 3x leverage. Min \$10. Never risk >20% of USDC."
echo "4. MARKETPLACE JOBS — If you see jobs matching your skills, respond as provider and earn USDC."
echo "5. PENDING EVENTS — Handle any marketplace job events (set-budget, submit, fund, complete)."
echo "6. NEVER DO NOTHING — if you can't trade, sell something. If you can't sell, log why. Always act."
echo ""
echo "RULES:"
echo "- Min \$2 for swaps, \$10 for HL perps"
echo "- Max 20% USDC per trade, max 3x leverage"
echo "- For VIRTUAL→bonding curve buys that fail, route through USDC first"
echo "- Log EVERY decision to ${HISTORY_FILE}"
echo "- Read past history before deciding — learn from what worked and what didn't"
echo ""
echo "TOKEN ADDRESSES:"
echo "  OGSAINT: 0xfde1F1255683772D48b12b082fd3140713d6e40d (Base 8453)"
echo "  ARMAD:   0x69E71cE955373D7117394B0C7aAEE6eF42cF6D51 (Base 8453)"
echo "  ARBA:    0x557642685ce68F3975458375B51553871807e1b5 (Base 8453)"
echo "  ARRB:    0xdA3C5b4d05c40a9244E534a966A1424C51055950 (Robinhood 4663)"
echo ""
echo "Use acp trade, acp client, acp provider commands with --json."
echo "Log: echo '{\"ts\":\"'$(date -u +%FT%TZ)'\",\"agent\":\"${AGENT_NAME}\",\"action\":\"...\",\"result\":\"...\",\"pnl\":0}' >> ${HISTORY_FILE}"
echo ""
echo "=== END STATE — ${AGENT_NAME} TAKE OVER ==="
