#!/usr/bin/env bash
# Self-Learning Profit Engine v1
#
# This is an AI-AGENT cron job (not a script). It runs with full LLM reasoning.
# Each cycle, the agent:
# 1. Checks wallet state and all ecosystem token prices
# 2. Scans Hyperliquid for perp opportunities (BTC, ETH, etc.)
# 3. Scans ACP marketplace for jobs it can fulfill for profit
# 4. Scans for trending Virtuals tokens it could trade
# 5. Analyzes arbitrage between ecosystem tokens
# 6. Makes trading decisions and executes them
# 7. Logs results to a learning file for pattern recognition
#
# The agent should read this file each run and follow the instructions.

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

echo "=== Self-Learning Profit Engine — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# --- STATE COLLECTION ---
# 1. Wallet balance
echo "--- WALLET STATE ---"
acp wallet balance --json 2>/dev/null || echo '{"error":"balance fetch failed"}'

# 2. Ecosystem token prices via Virtuals API
echo "--- ECOSYSTEM TOKENS ---"
for VID_INFO in "123348:OGSAINT" "127458:ARBA" "123305:ARMAD" "124146:ARRB"; do
    VID=$(echo "$VID_INFO" | cut -d: -f1)
    SYM=$(echo "$VID_INFO" | cut -d: -f2)
    curl -s "https://api.virtuals.io/api/virtuals/$VID" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin).get('data', {})
    print(f'{d.get(\"symbol\",\"?\")}: status={d.get(\"status\")}, price=\${d.get(\"tokenPrice\",\"?\")}, mcap={d.get(\"marketCap\",\"?\")}, vol24h={d.get(\"netVolume24h\",0)}, curveProgress={d.get(\"curveProgress\",\"?\")}%')
except: print('$SYM: fetch failed')
" 2>&1
done

# 3. Hyperliquid account status + positions
echo "--- HYPERLIQUID STATUS ---"
acp trade hl-status --json 2>/dev/null || echo '{"error":"hl status failed"}'

# 4. HL market data — check top perps for opportunities
echo "--- HL MARKETS (top volume) ---"
curl -s "https://api.hyperliquid.xyz/info" -X POST -H "Content-Type: application/json" -d '{"type":"metaAndAssetCtxs"}' 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    universe = data[0] if isinstance(data, list) else {}
    ctxs = data[1] if isinstance(data, list) and len(data) > 1 else []
    # universe is a dict with 'universe' key containing the list
    uni_list = universe.get('universe', []) if isinstance(universe, dict) else universe
    opportunities = []
    for i, ctx in enumerate(ctxs):
        if i >= len(uni_list): break
        u = uni_list[i]
        name = u.get('name', '?')
        funding = float(ctx.get('funding', '0') or '0')
        vol24h = float(ctx.get('dayNtlVlm', '0') or '0')
        price = ctx.get('markPx', '?')
        open_interest = float(ctx.get('openInterest', '0') or '0')
        if vol24h > 1000000 or abs(funding) > 0.001:
            opportunities.append({
                'name': name,
                'price': price,
                'funding': funding,
                'vol24h': vol24h,
                'oi': open_interest
            })
    opportunities.sort(key=lambda x: x['vol24h'], reverse=True)
    for o in opportunities[:10]:
        print(f\"  {o['name']:12s} price=\${o['price']} funding={o['funding']:.6f} vol24h=\${o['vol24h']:,.0f} oi={o['oi']:,.0f}\")
    if not opportunities:
        print('  No notable opportunities found')
except Exception as e:
    print(f'  Error: {e}')
" 2>&1

# 5. ACP Marketplace — browse for jobs this agent could fulfill
echo "--- MARKETPLACE OPPORTUNITIES ---"
acp browse "trading analysis token research market sentiment" --top-k 5 --online all --json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    agents = data.get('data', [])
    for a in agents[:5]:
        name = a.get('name', '?')
        offerings = a.get('offerings', [])
        for o in offerings[:2]:
            price = o.get('priceValue', '?')
            oname = o.get('name', '?')
            print(f'  {name}: {oname} @ \${price}')
except: print('  Browse failed')
" 2>&1

# 6. Check for pending marketplace events (job responses)
echo "--- PENDING EVENTS ---"
acp events drain --file /tmp/acp-events.jsonl --limit 20 --json 2>/dev/null || echo '{"events":[]}'

# 7. Read learning history for context
echo "--- LEARNING HISTORY ---"
if [ -f $HOME/.hermes/data/profit-engine-history.jsonl ]; then
    tail -20 $HOME/.hermes/data/profit-engine-history.jsonl 2>/dev/null | python3 -c "
import sys, json
lines = sys.stdin.readlines()
print(f'  {len(lines)} past decisions on record')
# Show last 5
for line in lines[-5:]:
    try:
        d = json.loads(line.strip())
        print(f\"  {d.get('ts','?')}: {d.get('action','?')} — {d.get('result','?')} — P&L: {d.get('pnl','?')}\")
    except: pass
" 2>&1
else
    echo "  No history yet — first run"
fi

echo ""
echo "=== ANALYSIS INSTRUCTIONS FOR AGENT ==="
echo "You are the Armadillo Saint (OGSAINT) profit engine."
echo "Based on the data above, decide what to do THIS cycle:"
echo ""
echo "OPPORTUNITY TYPES (rank by expected value):"
echo "1. HYPERLIQUID PERP TRADES — if funding is extreme (>0.01% or <-0.01%), consider a counter-trade. If high volume + momentum, consider a position. Min $10 order."
echo "2. ECOSYSTEM SWAP ARBITRAGE — if any ecosystem token price differs between bonding curve and any DEX, arb it."
echo "3. MARKETPLACE JOBS — if any agent is hiring for skills you have (trading analysis, market sentiment), respond and earn USDC."
echo "4. TOKEN SWING TRADES — buy ecosystem tokens low, sell high on bonding curve swings."
echo "5. EVENT RESPONSES — if marketplace jobs have events pending (budget_set needs funding, submitted needs completion), handle them."
echo ""
echo "RULES:"
echo "- Never risk more than 20% of available USDC on a single trade"
echo "- Always log decisions to $HOME/.hermes/data/profit-engine-history.jsonl"
echo "- If no good opportunity exists, DO NOTHING — preserving capital is a valid decision"
echo "- For HL perps, use max 3x leverage and always set stop-loss mentally"
echo "- Min trade size is $2 for swaps, $10 for HL perps"
echo ""
echo "EXECUTE the chosen action(s) using acp trade / acp client / acp provider commands."
echo "LOG the result with: echo '{\"ts\":\"$(date -u +%FT%TZ)\",\"action\":\"...\",\"result\":\"...\",\"pnl\":...}' >> $HOME/.hermes/data/profit-engine-history.jsonl"
echo ""
echo "=== END STATE COLLECTION — AGENT TAKE OVER ==="
