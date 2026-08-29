#!/usr/bin/env bash
# ============================================================
# ECOSYSTEM WATCHDOG — Permanent auto-healing daemon
# NEVER STOPS. Runs every 5 minutes. No AI needed. Pure deterministic fixes.
# This is the safety net that ensures the ecosystem NEVER STOPS.
# If the gateway restarts and wipes all crons, this recreates them all.
# ============================================================

set -eo pipefail
export LC_ALL=C

# ── Resolve HERMES_HOME robustly — works in both interactive and cron context ──
# 1. Try $HOME if it looks valid, 2. fall back to getent, 3. hardcode /home/hermes
if [ -n "${HOME:-}" ] && [ -d "$HOME" ] && [ "$HOME" != "/" ]; then
    HERMES_HOME="$HOME"
else
    HERMES_HOME=$(getent passwd "$(whoami)" 2>/dev/null | cut -d: -f6)
    HERMES_HOME="${HERMES_HOME:-/home/hermes}"
fi

HERMES_BIN="/opt/hermes-agent/hermes"
export PYTHONPATH="${HERMES_HOME}/.hermes/home/.local/lib/python3.11/site-packages:${PYTHONPATH:-}"

LOG="${HERMES_HOME}/.hermes/data/watchdog.log"
STATE_DIR="${HERMES_HOME}/.hermes/data"
PROGRESS_FILE="${STATE_DIR}/watchdog-progress.json"
SCRIPTS_DIR="${HERMES_HOME}/.hermes/scripts"
REPO_CACHE="${HERMES_HOME}/.hermes/repo-cache"
GITHUB_REPO="${GITHUB_REPO:-2skw9k57cn-arch/Armadillo-}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"

mkdir -p "$STATE_DIR" "$SCRIPTS_DIR" "$REPO_CACHE"

log() { echo "[$(date -u +%FT%TZ)] $1" | tee -a "$LOG"; }

# notify() — wrapper around notify.sh; safe even if the script isn't present yet
notify() {
    local level="$1" title="$2" msg="$3"
    local ns="${SCRIPTS_DIR}/notify.sh"
    if [ -x "$ns" ]; then
        bash "$ns" "$level" "$title" "$msg" 2>/dev/null || true
    else
        log "  [NOTIFY-FALLBACK] ${level}: ${title} — ${msg}"
    fi
}

# -------------------------------------------------------
# LOAD PROGRESS STATE
# -------------------------------------------------------
CYCLE=1
REGEN_TOTAL=0
if [ -f "$PROGRESS_FILE" ]; then
    CYCLE=$(python3 -c "
import json
try:
    d=json.load(open('$PROGRESS_FILE'))
    print(d.get('cycle',0)+1)
except: print(1)
" 2>/dev/null || echo 1)
    REGEN_TOTAL=$(python3 -c "
import json
try:
    d=json.load(open('$PROGRESS_FILE'))
    print(d.get('regen_total',0))
except: print(0)
" 2>/dev/null || echo 0)
fi
REGEN_THIS_RUN=0
PULL_UPDATED=0
PULL_OK=0
PULL_FAIL=0

log "=== WATCHDOG RUN #${CYCLE} (home=${HERMES_HOME}) ==="

# -------------------------------------------------------
# 0. GIT PULL + INJECT CORRECTIONS
# -------------------------------------------------------
# Uses git (with system credential store) instead of raw curl
# so this works with private repos.
REPO_DIR="${REPO_CACHE}/Armadillo-"
GIT_OK=0

if [ -d "${REPO_DIR}/.git" ]; then
    # Already cloned — pull latest
    if git -C "$REPO_DIR" pull --ff-only --quiet 2>/dev/null; then
        GIT_OK=1
        log "  ✅ git pull OK"
    else
        log "  ⚠ git pull failed — using cached scripts"
    fi
else
    # First time — clone
    REPO_URL="https://github.com/${GITHUB_REPO}.git"
    if git clone --depth 1 --branch "$GITHUB_BRANCH" "$REPO_URL" "$REPO_DIR" --quiet 2>/dev/null; then
        GIT_OK=1
        log "  ✅ git clone OK"
    else
        log "  ⚠ git clone failed — scripts will not be updated this cycle"
    fi
fi

if [ "$GIT_OK" = "1" ] && [ -d "${REPO_DIR}/scripts" ]; then
    for src in "${REPO_DIR}"/scripts/*.sh "${REPO_DIR}"/scripts/*.py; do
        [ -f "$src" ] || continue
        filename=$(basename "$src")
        dest="${SCRIPTS_DIR}/${filename}"
        if ! cmp -s "$src" "$dest" 2>/dev/null; then
            cp "$src" "$dest"
            chmod +x "$dest" 2>/dev/null || true
            log "  ↻ Injected updated ${filename}"
            PULL_UPDATED=$((PULL_UPDATED + 1))
        fi
        PULL_OK=$((PULL_OK + 1))
    done
    log "  Script sync: ${PULL_OK} verified, ${PULL_UPDATED} updated"
else
    PULL_FAIL=1
    log "  Script sync: skipped (git unavailable)"
fi

# -------------------------------------------------------
# 1. CHECK EVENT LISTENERS — restart if dead
# -------------------------------------------------------
LISTENER_SCRIPT="${SCRIPTS_DIR}/start-listeners.sh"
EXPECTED_LISTENER_FILES=(
    "/tmp/acp-events-OGSAINT.jsonl"
    "/tmp/acp-events-ArmaBase.jsonl"
    "/tmp/acp-events-Scout.jsonl"
    "/tmp/acp-events-ARMAD-Saint.jsonl"
)

LISTENERS_ALIVE=0
for f in "${EXPECTED_LISTENER_FILES[@]}"; do
    if [ -f "$f" ]; then
        AGE=$(python3 -c "
import os,time
try: print(int(time.time()-os.path.getmtime('$f')))
except: print(9999)
" 2>/dev/null || echo 9999)
        if [ "$AGE" -lt 600 ]; then
            LISTENERS_ALIVE=$((LISTENERS_ALIVE + 1))
        fi
    fi
done

if [ "$LISTENERS_ALIVE" -lt 4 ]; then
    log "  Only $LISTENERS_ALIVE/4 listeners alive — restarting"
    pkill -f "acp events listen" 2>/dev/null || true
    sleep 1
    nohup bash "$LISTENER_SCRIPT" >/dev/null 2>&1 &
    log "  Listeners restarted"
    notify "ALERT" "Listeners restarted" "Only ${LISTENERS_ALIVE}/4 ACP event listeners were alive — watchdog restarted them (cycle #${CYCLE})"
else
    log "  ✅ All 4 listeners alive"
fi

# -------------------------------------------------------
# 2. VERIFY CRON JOBS — recreate or patch if wrong
# Format: "name|schedule|type|script_or_none|prompt"
# -------------------------------------------------------
# Expected cron jobs and their configs
# NOTE: watchdog is listed FIRST so it gets recreated first after a gateway restart
EXPECTED_CRONS=(
    "watchdog|5m|script|watchdog.sh"
    "ecosystem-swap-loop|1h|script|ecosystem-swap-cron.sh"
    "ecosystem-job-create|3h|ai|none"
    "ecosystem-job-monitor|10m|ai|none"
    "profit-engine-ogsaint|1h|ai|none"
    "profit-engine-armabase|1h|ai|none"
    "profit-engine-scout|1h|ai|none"
    "profit-engine-armad|1h|ai|none"
    "oversight-manager|30m|ai|none"
)

CRON_OUTPUT=$("$HERMES_BIN" cron list 2>/dev/null || echo "ERROR")
if [ "$CRON_OUTPUT" = "ERROR" ]; then
    log "  ❌ hermes cron list failed — skipping cron verification"
fi

# Check each expected cron exists
# ALSO: clean up stale jobs (completed>0) that linger after gateway restarts
STALE_IDS=$(python3 << 'PYEOF' 2>/dev/null
import json
try:
    with open('/home/hermes/.hermes/cron/jobs.json') as f:
        data = json.load(f)
    jobs = data if isinstance(data, list) else data.get('jobs', [])
    for j in jobs:
        completed = j.get('repeat', {}).get('completed', 0)
        if completed > 0:
            print(j.get('id', ''))
except: pass
PYEOF
)

if [ -n "$STALE_IDS" ]; then
    log "  🧹 Cleaning up stale jobs (completed>0)"
    for SID in $STALE_IDS; do
        eval "$HERMES cron delete $SID" 2>/dev/null || true
        log "    Deleted stale: $SID"
    done
fi

# Re-fetch cron list after cleanup
CRON_OUTPUT=$(eval "timeout 20 $HERMES cron list" 2>/dev/null || echo "ERROR")

for cron_def in "${EXPECTED_CRONS[@]}"; do
    NAME=$(echo "$cron_def" | cut -d'|' -f1)
    if echo "$CRON_OUTPUT" | grep -q "Name:.*$NAME"; then
        log "  ✅ $NAME exists"
    else
        log "  ❌ $NAME MISSING — recreating"
        notify "ALERT" "Cron missing: ${NAME}" "Watchdog detected cron job '${NAME}' is missing — recreating (cycle #${CYCLE})"
        SCHEDULE=$(echo "$cron_def" | cut -d'|' -f2)
        MODE=$(echo "$cron_def" | cut -d'|' -f3)
        SFILE=$(echo "$cron_def" | cut -d'|' -f4)
        PROMPT=$(echo "$cron_def" | cut -d'|' -f5-)

        EXISTING_ID=$(cron_id_for "$CNAME")

        if [ -z "$EXISTING_ID" ]; then
            log "  ❌ $CNAME MISSING — creating (schedule=${SCHED})"
            if [ "$MODE" = "script" ]; then
                "$HERMES_BIN" cron create "$SCHED" "Run $CNAME" \
                    --name "$CNAME" --no-agent \
                    --script "$SFILE" --workdir /workspace 2>/dev/null || true
            else
                "$HERMES_BIN" cron create "$SCHED" "$PROMPT" \
                    --name "$CNAME" --skill acp-cli --workdir /workspace 2>/dev/null || true
            fi
            CRON_OUTPUT=$("$HERMES_BIN" cron list 2>/dev/null || echo "$CRON_OUTPUT")
            NEW_ID=$(cron_id_for "$CNAME")
            if [ -n "$NEW_ID" ]; then
                "$HERMES_BIN" cron edit "$NEW_ID" --repeat -1 2>/dev/null || true
                log "  ✅ $CNAME created (ID ${NEW_ID}), --repeat -1 set"
            else
                log "  ⚠ $CNAME creation may have failed — will retry next cycle"
            fi
            REGEN_THIS_RUN=$((REGEN_THIS_RUN + 1))
        else
            # AI-driven cron — needs a prompt
            case "$NAME" in
                ecosystem-job-create)
                    PROMPT="Create ACP marketplace jobs hiring other Armadillo ecosystem agents. NEVER STOP — runs every 3h forever."
                    ;;
                ecosystem-job-monitor)
                    PROMPT="Check ACP marketplace events for all agents. Drain events. Fund budget_set jobs, complete submitted jobs. NEVER STOP — runs every 10m forever."
                    ;;
                profit-engine-ogsaint)
                    PROMPT="Run profit engine for OGSAINT. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019f9f75-130e-75fc-9459-5358c8d25206 OGSAINT. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."
                    ;;
                profit-engine-armabase)
                    PROMPT="Run profit engine for ArmaBase. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019fbb50-31de-7e2f-be3b-2225023960b3 ArmaBase. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."
                    ;;
                profit-engine-scout)
                    PROMPT="Run profit engine for Scout. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019fa674-7be2-72ce-956c-3a7f831e9102 Scout. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."
                    ;;
                profit-engine-armad)
                    PROMPT="Run profit engine for ARMAD-Saint. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019f9f75-493a-7011-b547-aa9c2df1a1ac ARMAD-Saint. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."
                    ;;
                oversight-manager)
                    PROMPT="Run the OVERSIGHT MANAGER. Execute: bash ~/.hermes/scripts/oversight-manager.sh. Fix problems, rewrite broken scripts, rebalance funds. NEVER STOP — runs every 30m forever. The ecosystem NEVER STOPS."
                    ;;
                *)
                    PROMPT="Run $NAME"
                    ;;
            esac
            echo "" | eval "timeout 30 $HERMES cron create \"$SCHEDULE\" \"$PROMPT\" --name \"$NAME\" --skill acp-cli --workdir /workspace" 2>/dev/null || true
        fi
    done
fi

# -------------------------------------------------------
# 3. VERIFY SCRIPTS — present and executable
# -------------------------------------------------------
for script in watchdog.sh oversight-manager.sh profit-engine-agent.sh profit-engine.sh \
              ecosystem-swap-cron.sh cross-agent-swap.sh ecosystem-job-monitor.sh \
              ecosystem-job-create.sh start-listeners.sh parse-wallet.py parse-sellable.py \
              notify.sh; do
    sp="${SCRIPTS_DIR}/${script}"
    if [ ! -f "$sp" ]; then
        log "  ❌ ${script} MISSING"
        notify "WARN" "Script missing: ${script}" "Expected script ${script} not found in ${SCRIPTS_DIR} (cycle #${CYCLE})"
    elif [ ! -x "$sp" ]; then
        chmod +x "$sp"
        log "  ↻ ${script} made executable"
    fi
done

# -------------------------------------------------------
# 4. CHECK AGENT WALLETS — detect depleted agents
# -------------------------------------------------------
AGENTS=(
    "019f9f75-130e-75fc-9459-5358c8d25206:OGSAINT"
    "019fbb50-31de-7e2f-be3b-2225023960b3:ArmaBase"
    "019fa674-7be2-72ce-956c-3a7f831e9102:Scout"
    "019f9f75-493a-7011-b547-aa9c2df1a1ac:ARMAD-Saint"
)
DEPLETED_FILE="${STATE_DIR}/depleted-agents.json"
echo "[" > "${DEPLETED_FILE}.tmp"
FIRST=1
for agent_info in "${AGENTS[@]}"; do
    AID=$(echo "$agent_info" | cut -d: -f1)
    ANAME=$(echo "$agent_info" | cut -d: -f2)
    acp agent use --agent-id "$AID" --json >/dev/null 2>&1 || true
    USDC=$(acp wallet balance --chain-id 8453 --json 2>/dev/null | python3 -c "
import sys,json
try:
    data=json.load(sys.stdin)
    total=0
    for t in data.get('tokens',[]):
        if t.get('tokenMetadata',{}).get('symbol')=='USDC':
            dec=t.get('tokenMetadata',{}).get('decimals',6)
            total+=int(t['tokenBalance'],16)/(10**dec)
    print(f'{total:.2f}')
except: print('0')
" 2>/dev/null || echo "0")
    if python3 -c "exit(0 if float('${USDC}')<2.0 else 1)" 2>/dev/null; then
        log "  ⚠ ${ANAME} depleted: \$${USDC} USDC"
        notify "CRITICAL" "Wallet depleted: ${ANAME}" "Agent ${ANAME} has only \$${USDC} USDC — below the \$2.00 trading minimum (cycle #${CYCLE})"
        [ $FIRST -eq 0 ] && echo "," >> "${DEPLETED_FILE}.tmp"
        echo "{\"agent\":\"${ANAME}\",\"agentId\":\"${AID}\",\"usdc\":${USDC}}" >> "${DEPLETED_FILE}.tmp"
        FIRST=0
    fi
done
acp agent use --agent-id 019f9f75-130e-75fc-9459-5358c8d25206 --json >/dev/null 2>&1 || true
echo "]" >> "${DEPLETED_FILE}.tmp"
mv "${DEPLETED_FILE}.tmp" "$DEPLETED_FILE"

# -------------------------------------------------------
# 5. CHECK PROFIT ENGINE LOGS — stale > 5h (18000s)
# -------------------------------------------------------
for ENAME in OGSAINT ArmaBase Scout ARMAD-Saint; do
    HIST="${STATE_DIR}/profit-engine-${ENAME}.jsonl"
    if [ ! -f "$HIST" ] || [ ! -s "$HIST" ]; then
        log "  ⚠ ${ENAME} profit engine: no history yet"
    else
        LINES=$(wc -l < "$HIST")
        LAST_AGE=$(python3 -c "
import os,time
try: print(int(time.time()-os.path.getmtime('$HIST')))
except: print(9999)
" 2>/dev/null || echo 9999)
        if [ "$LAST_AGE" -gt 18000 ]; then
            log "  ⚠ ${ENAME} STALE (last=${LAST_AGE}s, decisions=${LINES})"
            notify "WARN" "Profit engine stale: ${ENAME}" "No activity in ${LAST_AGE}s (${LINES} total decisions logged) — engine may be stuck (cycle #${CYCLE})"
        else
            log "  ✅ ${ENAME} active (${LINES} decisions, last=${LAST_AGE}s ago)"
        fi
    fi
done

# -------------------------------------------------------
# 6. WRITE PROGRESS SNAPSHOT
# -------------------------------------------------------
REGEN_TOTAL=$((REGEN_TOTAL + REGEN_THIS_RUN))
NOW=$(date -u +%FT%TZ)
python3 - <<PYEOF
import json
pf = '${PROGRESS_FILE}'
prev = {}
try:
    prev = json.load(open(pf))
except Exception:
    pass
data = {
    'cycle': ${CYCLE},
    'last_run': '${NOW}',
    'hermes_home': '${HERMES_HOME}',
    'regen_this_run': ${REGEN_THIS_RUN},
    'regen_total': ${REGEN_TOTAL},
    'scripts_updated_this_run': ${PULL_UPDATED},
    'listeners_alive': ${LISTENERS_ALIVE},
    'history': (prev.get('history', []) + [{
        'cycle': ${CYCLE}, 'ts': '${NOW}',
        'regen': ${REGEN_THIS_RUN}, 'scripts_updated': ${PULL_UPDATED},
    }])[-50:],
}
json.dump(data, open(pf, 'w'), indent=2)
lu = sum(h.get('scripts_updated', 0) for h in data['history'])
print(f"Progress: cycle={data['cycle']}, corrections_total={data['regen_total']}, injections_total={lu}, home={data['hermes_home']}")
PYEOF

# -------------------------------------------------------
# 7. CLEANUP
# -------------------------------------------------------
find /tmp -name "acp-events-*.tmp" -mtime +1 -delete 2>/dev/null || true

log "=== WATCHDOG COMPLETE (cycle #${CYCLE}, ${REGEN_THIS_RUN} corrections) ==="
echo "Watchdog OK — cycle #${CYCLE} | log: ${LOG}"
