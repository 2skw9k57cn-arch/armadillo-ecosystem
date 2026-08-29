#!/usr/bin/env bash
# ============================================================
# ARMADILLO ECOSYSTEM BOOTSTRAP — NEVER STOPS
# ============================================================
# One-command deployment: sets up all scripts, cron jobs,
# event listeners, and the self-healing watchdog.
#
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/2skw9k57cn-arch/Armadillo-/main/scripts/bootstrap.sh)
#
# Prerequisites:
#   - Hermes Agent installed at /opt/hermes-agent/hermes
#   - ACP CLI installed and authenticated (acp agent whoami works)
#   - PYTHONPATH set for hermes CLI
#
# After bootstrap, the ecosystem runs autonomously and NEVER STOPS.
# Watchdog (5m) auto-heals everything. Oversight (30m) rewrites broken
# components. Profit engines (1h ×4) trade buy+sell every cycle.
# ============================================================

set -eo pipefail
export LC_ALL=C

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date -u +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date -u +%H:%M:%S)]${NC} $1"; }
err() { echo -e "${RED}[$(date -u +%H:%M:%S)]${NC} $1"; }
info() { echo -e "${CYAN}[$(date -u +%H:%M:%S)]${NC} $1"; }

echo ""
echo "============================================================"
echo "   ARMADILLO ECOSYSTEM BOOTSTRAP — NEVER STOPS"
echo "============================================================"
echo ""

# -------------------------------------------------------
# 0. CHECK PREREQUISITES
# -------------------------------------------------------
info "Checking prerequisites..."

HERMES_BIN="/opt/hermes-agent/hermes"
SCRIPTS_DIR="$HOME/.hermes/scripts"
DATA_DIR="$HOME/.hermes/data"
# Detect the repo's scripts/ directory from wherever bootstrap.sh was called from
# Works with: git clone ... /tmp/armadillo-repo && bash /tmp/armadillo-repo/scripts/bootstrap.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_REPO="$SCRIPT_DIR"
REPO_BASE="https://raw.githubusercontent.com/2skw9k57cn-arch/Armadillo-/main/scripts"

export PYTHONPATH="/home/hermes/.hermes/home/.local/lib/python3.11/site-packages:$PYTHONPATH"
HERMES="export PYTHONPATH='/home/hermes/.hermes/home/.local/lib/python3.11/site-packages:\$PYTHONPATH'; /opt/hermes-agent/hermes"

# Check hermes CLI
if ! eval "$HERMES --version" >/dev/null 2>&1; then
    err "Hermes CLI not found at $HERMES_BIN"
    exit 1
fi
log "Hermes CLI: OK"

# Check ACP CLI
if ! acp agent whoami >/dev/null 2>&1; then
    err "ACP CLI not authenticated — run 'acp auth' first"
    exit 1
fi
log "ACP CLI: OK"

# Check git
if ! git --version >/dev/null 2>&1; then
    err "git not installed"
    exit 1
fi
log "git: OK"

# Identify this agent
AGENT_INFO=$(acp agent whoami --json 2>/dev/null || echo '{}')
AGENT_NAME=$(echo "$AGENT_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name','unknown'))" 2>/dev/null)
AGENT_ID=$(echo "$AGENT_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','unknown'))" 2>/dev/null)
WALLET_ADDR=$(echo "$AGENT_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('walletAddress','unknown'))" 2>/dev/null)

log "Agent: $AGENT_NAME (ID: $AGENT_ID)"
log "Wallet: $WALLET_ADDR"
echo ""

# -------------------------------------------------------
# 1. DOWNLOAD ALL SCRIPTS
# -------------------------------------------------------
info "Downloading ecosystem scripts..."

mkdir -p "$SCRIPTS_DIR" "$DATA_DIR"

SCRIPTS=(
    "watchdog.sh"
    "oversight-manager.sh"
    "profit-engine-agent.sh"
    "profit-engine.sh"
    "ecosystem-swap-cron.sh"
    "ecosystem-swap-loop.sh"
    "ecosystem-job-monitor.sh"
    "ecosystem-job-create.sh"
    "cross-agent-swap.sh"
    "start-listeners.sh"
    "parse-sellable.py"
    "parse-wallet.py"
)

DOWNLOADED=0
FAILED=0
for SCRIPT in "${SCRIPTS[@]}"; do
    # Try local repo first, then GitHub
    if [ -f "${LOCAL_REPO}/${SCRIPT}" ]; then
        cp "${LOCAL_REPO}/${SCRIPT}" "${SCRIPTS_DIR}/${SCRIPT}"
        chmod +x "${SCRIPTS_DIR}/${SCRIPT}" 2>/dev/null
        DOWNLOADED=$((DOWNLOADED + 1))
        log "  ✅ ${SCRIPT} (local)"
    elif curl -fsSL "${REPO_BASE}/${SCRIPT}" -o "${SCRIPTS_DIR}/${SCRIPT}" 2>/dev/null; then
        chmod +x "${SCRIPTS_DIR}/${SCRIPT}" 2>/dev/null
        DOWNLOADED=$((DOWNLOADED + 1))
        log "  ✅ ${SCRIPT} (remote)"
    else
        err "  ❌ ${SCRIPT} — not found locally or remotely"
        FAILED=$((FAILED + 1))
    fi
done

echo ""
log "Downloaded: ${DOWNLOADED}/${#SCRIPTS[@]} scripts"
if [ "$FAILED" -gt 0 ]; then
    warn "Failed: ${FAILED} scripts — some features may not work"
fi
echo ""

# -------------------------------------------------------
# 2. VERIFY ACP AGENT SIGNER
# -------------------------------------------------------
info "Checking agent signer..."

SIGNER_STATUS=$(acp agent signer-policy --json 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    signers = d.get('signers', [])
    if signers:
        s = signers[0]
        print(f'{s.get(\"status\",\"unknown\")}:{s.get(\"keyType\",\"P256\")}')
    else:
        print('none:none')
except: print('error:error')
" 2>/dev/null || echo "error:error")

if [ "$SIGNER_STATUS" = "error:error" ]; then
    warn "Cannot verify signer — trades may fail"
elif [ "$SIGNER_STATUS" = "none:none" ]; then
    warn "No signer approved — run 'acp agent add-signer' and approve via Virtuals dashboard"
else
    log "Signer: ${SIGNER_STATUS} ✅"
fi
echo ""

# -------------------------------------------------------
# 3. CLEAN UP ANY STALE CRON JOBS
# -------------------------------------------------------
info "Cleaning up stale cron jobs..."

EXISTING_JOBS=$(eval "timeout 20 $HERMES cron list" 2>/dev/null | grep -o '[a-f0-9]\{12\}' | head -30 || true)
if [ -n "$EXISTING_JOBS" ]; then
    for JID in $EXISTING_JOBS; do
        eval "timeout 20 $HERMES cron delete $JID" 2>/dev/null || true
    done
    log "Cleaned up existing jobs"
else
    log "No existing jobs to clean"
fi

# Also clean stale entries from jobs.json
python3 << 'PYEOF' 2>/dev/null
import json, subprocess, os
jobs_file = os.path.expanduser("~/.hermes/cron/jobs.json")
if not os.path.exists(jobs_file):
    exit(0)
with open(jobs_file) as f:
    data = json.load(f)
jobs = data if isinstance(data, list) else data.get('jobs', [])
stale = [j.get('id','') for j in jobs if j.get('repeat',{}).get('completed',0) > 0]
if stale:
    hermes = "export PYTHONPATH='/home/hermes/.hermes/home/.local/lib/python3.11/site-packages:$PYTHONPATH'; /opt/hermes-agent/hermes"
    for sid in stale:
        subprocess.run(f"{hermes} cron delete {sid} 2>/dev/null", shell=True)
    print(f"Cleaned {len(stale)} stale jobs from jobs.json")
PYEOF
echo ""

# -------------------------------------------------------
# 4. CREATE ALL CRON JOBS — NEVER STOPS
# -------------------------------------------------------
info "Creating cron jobs (all set to ∞ repeat)..."
echo ""

# Helper: create cron and set to infinite
create_cron() {
    local SCHEDULE="$1"
    local NAME="$2"
    local MODE="$3"
    local SCRIPT_NAME="$4"
    local PROMPT="$5"

    if [ "$MODE" = "script" ]; then
        OUTPUT=$(echo "" | eval "timeout 30 $HERMES cron create \"$SCHEDULE\" \"$PROMPT\" --name \"$NAME\" --no-agent --script \"$SCRIPT_NAME\" --workdir /workspace" 2>&1) || true
    else
        OUTPUT=$(echo "" | eval "timeout 30 $HERMES cron create \"$SCHEDULE\" \"$PROMPT\" --name \"$NAME\" --skill acp-cli --workdir /workspace" 2>&1) || true
    fi

    JOB_ID=$(echo "$OUTPUT" | grep "Created job:" | awk '{print $3}')

    if [ -n "$JOB_ID" ]; then
        eval "timeout 20 $HERMES cron edit $JOB_ID --repeat -1" 2>/dev/null || true
        log "  ✅ ${NAME} (${SCHEDULE}) ∞ — ${JOB_ID}"
    else
        err "  ❌ ${NAME} — creation failed (non-fatal)"
    fi
}

# 1. Watchdog (5m, script, deterministic self-healing)
create_cron "5m" "watchdog" "script" "watchdog.sh" \
    "Run the ecosystem watchdog auto-healer. Execute: bash ~/.hermes/scripts/watchdog.sh. NEVER STOPS — runs every 5m forever."

# 2. Job Monitor (10m, AI)
create_cron "10m" "ecosystem-job-monitor" "ai" "none" \
    "Check ACP marketplace events for all agents. Drain events. Fund budget_set jobs, complete submitted jobs. NEVER STOP — runs every 10m forever."

# 3. Oversight Manager (30m, AI)
create_cron "30m" "oversight-manager" "ai" "none" \
    "Run the OVERSIGHT MANAGER. Execute: bash ~/.hermes/scripts/oversight-manager.sh. Fix problems, rewrite broken scripts, rebalance funds. NEVER STOP — runs every 30m forever. The ecosystem NEVER STOPS."

# 4-7. Profit Engines (1h ×4, AI)
create_cron "1h" "profit-engine-ogsaint" "ai" "none" \
    "Run profit engine for OGSAINT. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019f9f75-130e-75fc-9459-5358c8d25206 OGSAINT. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."

create_cron "1h" "profit-engine-armabase" "ai" "none" \
    "Run profit engine for ArmaBase. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019fbb50-31de-7e2f-be3b-2225023960b3 ArmaBase. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."

create_cron "1h" "profit-engine-scout" "ai" "none" \
    "Run profit engine for Scout. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019fa674-7be2-72ce-956c-3a7f831e9102 Scout. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."

create_cron "1h" "profit-engine-armad" "ai" "none" \
    "Run profit engine for ARMAD-Saint. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019f9f75-493a-7011-b547-aa9c2df1a1ac ARMAD-Saint. Buy AND sell ecosystem tokens every cycle. NEVER STOP — runs every 1h forever."

# 8. Swap Loop (1h, script)
create_cron "1h" "ecosystem-swap-loop" "script" "ecosystem-swap-cron.sh" \
    "Run ecosystem swap loop — buy AND sell ecosystem tokens every hour FOREVER. NEVER STOPS."

# 9. Job Create (3h, AI)
create_cron "3h" "ecosystem-job-create" "ai" "none" \
    "Create ACP marketplace jobs hiring other Armadillo ecosystem agents. NEVER STOP — runs every 3h forever."

echo ""

# -------------------------------------------------------
# 5. START EVENT LISTENERS
# -------------------------------------------------------
info "Starting event listeners..."

# Kill any existing listeners
pkill -f "acp events listen" 2>/dev/null || true
sleep 1

# Start fresh listeners
if [ -f "${SCRIPTS_DIR}/start-listeners.sh" ]; then
    bash "${SCRIPTS_DIR}/start-listeners.sh" 2>/dev/null &
    sleep 2
fi

# Verify listeners
LIVE_LISTENERS=$(pgrep -c -f "acp events listen" 2>/dev/null || echo 0)
if [ "${LIVE_LISTENERS:-0}" -gt 0 ]; then
    log "Event listeners: ${LIVE_LISTENERS} alive ✅"
else
    warn "Event listeners not started — watchdog will restart them"
fi
echo ""

# -------------------------------------------------------
# 6. RUN WATCHDOG IMMEDIATELY
# -------------------------------------------------------
info "Running watchdog for initial health check..."
WATCHDOG_PATH="${SCRIPTS_DIR}/watchdog.sh"
if [ -f "$WATCHDOG_PATH" ]; then
    bash "$WATCHDOG_PATH" 2>&1 | tail -15
else
    warn "Watchdog script not found at $WATCHDOG_PATH — skipping initial check"
fi
echo ""

# -------------------------------------------------------
# 7. FINAL STATUS
# -------------------------------------------------------
info "Final cron status:"
eval "timeout 20 $HERMES cron list" 2>&1 | grep -E "Name:|Schedule:|Repeat:" | paste - - - | sed 's/Name:/\n  /g; s/Schedule:/|/g; s/Repeat:/|/g'
echo ""

ACTIVE=$(eval "timeout 20 $HERMES cron list" 2>/dev/null | grep -c "active" || echo "0")
log "Active cron jobs: ${ACTIVE}"

echo ""
echo "============================================================"
echo "   BOOTSTRAP COMPLETE — ECOSYSTEM NEVER STOPS"
echo "============================================================"
echo ""
echo "  Agent:     $AGENT_NAME"
echo "  Wallet:    $WALLET_ADDR"
echo "  Scripts:   ${DOWNLOADED}/${#SCRIPTS[@]} downloaded"
echo "  Crons:     ${ACTIVE} active (all ∞)"
echo "  Watchdog:  Runs every 5m — auto-heals everything"
echo "  Oversight: Runs every 30m — rewrites broken components"
echo "  Profit:    Runs every 1h ×4 — buy + sell every cycle"
echo ""
echo "  The ecosystem is now autonomous and self-healing."
echo "  It will NEVER STOP."
echo "============================================================"
