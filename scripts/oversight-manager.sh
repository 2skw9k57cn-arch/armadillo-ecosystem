#!/usr/bin/env bash
# Oversight Manager — AI-driven ecosystem coordinator
#
# Runs every 2 hours with full LLM reasoning. Acts as the "brain" of the ecosystem:
# 1. Checks all 4 agents' wallet balances and trading status
# 2. Reviews each agent's profit-engine history for wins/losses
# 3. Checks all cron jobs are firing and producing results
# 4. Detects problems: stuck loops, failed swaps, depleted wallets, broken scripts
# 5. REWRITES scripts and RECREATES cron jobs when something is broken
# 6. Redeploys swap logic with fixes
# 7. Rebalances capital between agents if one is depleted
# 8. Adjusts swap loop parameters based on performance
#
# This script collects state. The AI agent then decides and executes fixes.

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

HERMES="export PYTHONPATH='$HOME/.hermes/home/.local/lib/python3.11/site-packages:$PYTHONPATH'; /opt/hermes-agent/hermes"

echo "=== OVERSIGHT MANAGER — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# --- ALL CRON JOBS ---
echo "--- CRON STATUS ---"
eval "$HERMES cron list" 2>&1

# --- ALL AGENT WALLETS ---
echo ""
echo "--- AGENT WALLETS ---"
AGENTS=(
    "019f9f75-130e-75fc-9459-5358c8d25206:OGSAINT"
    "019fbb50-31de-7e2f-be3b-2225023960b3:ArmaBase"
    "019fa674-7be2-72ce-956c-3a7f831e9102:Scout"
    "019f9f75-493a-7011-b547-aa9c2df1a1ac:ARMAD-Saint"
)

for AGENT_INFO in "${AGENTS[@]}"; do
    AGENT_ID=$(echo "$AGENT_INFO" | cut -d: -f1)
    NAME=$(echo "$AGENT_INFO" | cut -d: -f2)
    acp agent use --agent-id "$AGENT_ID" --json >/dev/null 2>&1
    BAL=$(acp wallet balance --chain-id 8453 --json 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
usdc = 0
virt = 0
ecosystem = []
for t in data.get('tokens', []):
    meta = t.get('tokenMetadata', {})
    sym = meta.get('symbol', '')
    decimals = meta.get('decimals', 18) or 18
    raw = int(t.get('tokenBalance', '0x0'), 16)
    human = raw / (10**decimals)
    if sym == 'USDC': usdc = human
    elif sym == 'VIRTUAL': virt = human
    elif sym in ('OGSAINT','ARMAD','ARBA','ARRB') and human > 0:
        ecosystem.append(f'{sym}={human:,.0f}')
can_trade = 'YES' if usdc >= 2.01 else 'NO'
print(f'  {\"$NAME\":<14} USDC=\${usdc:.2f} VIRTUAL={virt:.2f} can_trade={can_trade} | {\" | \".join(ecosystem)}')
" 2>&1)
    echo "$BAL"
done

# Switch back to OGSAINT for any operations
acp agent use --agent-id 019f9f75-130e-75fc-9459-5358c8d25206 --json >/dev/null 2>&1

# --- PROFIT ENGINE HISTORIES ---
echo ""
echo "--- PROFIT ENGINE PERFORMANCE ---"
for NAME in OGSAINT ArmaBase Scout ARMAD-Saint; do
    HIST="$HOME/.hermes/data/profit-engine-${NAME}.jsonl"
    if [ -f "$HIST" ] && [ -s "$HIST" ]; then
        LINES=$(wc -l < "$HIST")
        LAST=$(tail -1 "$HIST" 2>/dev/null)
        echo "  $NAME: $LINES decisions logged"
        echo "$LAST" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read().strip())
    print(f'    Last: {d.get(\"action\",\"?\")} → {d.get(\"result\",\"?\")} P&L={d.get(\"pnl\",\"?\")}')
except: print('    Last: parse error')
" 2>&1
    else
        echo "  $NAME: no history (engine hasn't run or hasn't logged)"
    fi
done

# --- SWAP LOOP SCRIPT INTEGRITY ---
echo ""
echo "--- SCRIPT STATUS ---"
for SCRIPT in ecosystem-swap-cron.sh cross-agent-swap.sh profit-engine.sh profit-engine-agent.sh; do
    PATH_CHECK="$HOME/.hermes/scripts/$SCRIPT"
    if [ -f "$PATH_CHECK" ]; then
        LINES=$(wc -l < "$PATH_CHECK")
        EXEC=$(test -x "$PATH_CHECK" && echo "executable" || echo "NOT executable")
        echo "  $SCRIPT: ${LINES} lines, $EXEC"
    else
        echo "  $SCRIPT: ❌ MISSING"
    fi
done

# --- MARKETPLACE JOB STATUS ---
echo ""
echo "--- MARKETPLACE JOBS ---"
# Dynamically check recent jobs — IDs change over time
# Check last 10 job IDs for any open/funded/submitted status
for JOB in 72026 72027 72028; do
    acp job history --job-id $JOB --chain-id 8453 --json 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f'  Job {$JOB}: status={d.get(\"status\",\"?\")}, budget={d.get(\"budget\",\"none\")}')
except: print(f'  Job {$JOB}: completed or not found')
" 2>&1
done

# --- ECOSYSTEM TOKEN GRADUATION PROGRESS ---
echo ""
echo "--- GRADUATION PROGRESS ---"
for VID_INFO in "123348:OGSAINT" "127458:ARBA" "123305:ARMAD" "124146:ARRB"; do
    VID=$(echo "$VID_INFO" | cut -d: -f1)
    SYM=$(echo "$VID_INFO" | cut -d: -f2)
    curl -s "https://api.virtuals.io/api/virtuals/$VID" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin).get('data', {})
    status = d.get('status', '?')
    vol = d.get('netVolume24h', 0)
    print(f'  $SYM: {status}, vol24h={vol}')
except: print(f'  $SYM: fetch failed')
" 2>&1
done

# --- VOLUME ANALYSIS ---
echo ""
echo "--- VOLUME TREND ---"
echo "  Check if netVolume24h is increasing across runs (indicates loops are working)"

echo ""
echo "=========================================="
echo "OVERSIGHT INSTRUCTIONS FOR AI AGENT"
echo "=========================================="
echo ""
echo "You are the OVERSIGHT MANAGER for the Armadillo ecosystem (4 agents, 4 tokens)."
echo "THE ECOSYSTEM NEVER STOPS. You run every 30 minutes, forever."
echo "Your job is to keep everything running and fix problems autonomously."
echo "Every cycle you MUST take action — never do nothing."
echo ""
echo "DIAGNOSTIC CHECKS:"
echo "1. BROKEN CRONS — If any cron is missing or failed, recreate it using hermes cron create + edit --repeat -1"
echo "2. DEPLETED AGENTS — If any agent has <\$2 USDC AND no sellable tokens, either:"
echo "   a) Sell their ecosystem tokens to USDC, OR"
echo "   b) Transfer USDC from a richer agent using acp wallet send-transaction, OR"
echo "   c) Log the problem for manual funding"
echo "3. STUCK MARKETPLACE JOBS — If jobs are still 'open' with no budget set after 24h, cancel and recreate with different offerings"
echo "4. FAILED SWAP LOOPS — If a swap loop script has errors, READ the script, identify the bug, REWRITE it with patch/write_file, and verify"
echo "5. PROFIT ENGINE NOT LOGGING — If any agent's profit engine has no history, check if the cron is firing and the script runs"
echo "6. SELL LEG FAILURES — If sells keep failing due to \$2 minimum, adjust sell percentage in the script"
echo ""
echo "REPAIR ACTIONS YOU CAN TAKE:"
echo "- Rewrite any script in ~/.hermes/scripts/ using write_file or patch"
echo "- Create/edit/delete cron jobs using hermes cron create/edit/delete"
echo "- Switch agents with acp agent use and execute trades"
echo "- Send USDC between agent wallets using acp wallet send-transaction"
echo "- Cancel and recreate marketplace jobs"
echo "- Adjust swap loop parameters (percentages, slippage, intervals)"
echo "- Send notifications: bash ~/.hermes/scripts/notify.sh <LEVEL> <TITLE> <MESSAGE>"
echo "  Levels: INFO | WARN | ALERT | CRITICAL"
echo "  Example: bash ~/.hermes/scripts/notify.sh CRITICAL 'Swap loop broken' 'Script returned exit 1 three times in a row'"
echo ""
echo "LOG your oversight actions to $HOME/.hermes/data/oversight-log.jsonl"
echo "echo '{\"ts\":\"'$(date -u +%FT%TZ)'\",\"action\":\"...\",\"target\":\"...\",\"result\":\"...\"}' >> $HOME/.hermes/data/oversight-log.jsonl"
echo ""
echo "AGENT DETAILS:"
echo "  OGSAINT:     019f9f75-130e-75fc-9459-5358c8d25206 (this agent, Base)"
echo "  ArmaBase:    019fbb50-31de-7e2f-be3b-2225023960b3 (Base)"
echo "  Scout:       019fa674-7be2-72ce-956c-3a7f831e9102 (Base + Robinhood)"
echo "  ARMAD-Saint: 019f9f75-493a-7011-b547-aa9c2df1a1ac (Base)"
echo ""
echo "TOKEN ADDRESSES:"
echo "  OGSAINT: 0xfde1F1255683772D48b12b082fd3140713d6e40d (Base 8453)"
echo "  ARMAD:   0x69E71cE955373D7117394B0C7aAEE6eF42cF6D51 (Base 8453)"
echo "  ARBA:    0x557642685ce68F3975458375B51553871807e1b5 (Base 8453)"
echo "  ARRB:    0xdA3C5b4d05c40a9244E534a966A1424C51055950 (Robinhood 4663)"
echo ""
echo "HERMES CLI: export PYTHONPATH='$HOME/.hermes/home/.local/lib/python3.11/site-packages:\$PYTHONPATH'; /opt/hermes-agent/hermes"
echo ""
echo "=== END OVERSIGHT STATE — MANAGER TAKE OVER ==="
