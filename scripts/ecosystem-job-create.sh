#!/usr/bin/env bash
# Ecosystem Job Creation — NEVER STOPS
#
# OGSAINT (client) hires ArmaBase, Scout, and ARMAD-Saint over Virtuals ACP.
# Listeners must already be running via start-listeners.sh / watchdog
# (per-agent files under /tmp/acp-events-*.jsonl). Do not start a fifth
# listener on /tmp/acp-events.jsonl — that file is never drained.

set -o pipefail

OGSAINT_ID="019f9f75-130e-75fc-9459-5358c8d25206"
ARMA_BASE_WALLET="0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"
SCOUT_WALLET="0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4"
SAINT_WALLET="0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"
CHAIN_ID=8453

echo "=== Ecosystem Job Creation — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""

echo "Switching to OGSAINT (client)..."
acp agent use --agent-id "$OGSAINT_ID" --json >/dev/null 2>&1 || {
    echo "✗ acp agent use failed — is acp configured on this host?"
    exit 1
}

create_job() {
    local label="$1"
    local provider="$2"
    local offering="$3"
    local requirements="$4"
    local stamp="$5"

    echo "--- $label ---"
    local out
    out=$(acp client create-job \
        --provider "$provider" \
        --offering-name "$offering" \
        --requirements "$requirements" \
        --chain-id "$CHAIN_ID" --json 2>&1) || true
    echo "$out"
    local job_id
    job_id=$(echo "$out" | python3 -c "import sys,json,re
raw=sys.stdin.read()
job=''
for line in raw.splitlines():
    line=line.strip()
    if not line.startswith('{'):
        continue
    try:
        d=json.loads(line)
    except Exception:
        continue
    job=str(d.get('jobId') or d.get('job_id') or d.get('id') or '')
    if job:
        break
print(job)" 2>/dev/null || true)
    if [ -n "$job_id" ]; then
        echo "✓ Job created: $job_id"
        echo "$job_id" > "/tmp/acp-job-${stamp}.txt"
    else
        echo "✗ Job creation failed"
    fi
    echo ""
}

create_job \
    "Hire ArmaBase — Quick Token Brief for OGSAINT" \
    "$ARMA_BASE_WALLET" \
    "Quick Token Brief" \
    '{"token_address":"0xfde1F1255683772D48b12b082fd3140713d6e40d","chain":"base"}' \
    "arbasaint"

create_job \
    "Hire Armadillo Scout — Quick Token Snapshot for OGSAINT" \
    "$SCOUT_WALLET" \
    "Quick Token Snapshot" \
    '{"token_address":"0xfde1F1255683772D48b12b082fd3140713d6e40d","chain":"base"}' \
    "scoutsaint"

create_job \
    "Hire Armadillo Saint (ARMAD) — Perp Signal for OGSAINT" \
    "$SAINT_WALLET" \
    "hyperliquid_perp_signal" \
    '{"pair":"BTC","timeframe":"4h","risk_preference":"balanced","extra_notes":"Context: OGSAINT ecosystem agent coordinating cross-token activity. Consider market conditions relevant to Virtuals Protocol ecosystem tokens."}' \
    "armadsaint"

echo "=== Job creation complete — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""
echo "Job IDs:"
[ -f /tmp/acp-job-arbasaint.txt ] && echo "  ArmaBase: $(cat /tmp/acp-job-arbasaint.txt)"
[ -f /tmp/acp-job-scoutsaint.txt ] && echo "  Scout: $(cat /tmp/acp-job-scoutsaint.txt)"
[ -f /tmp/acp-job-armadsaint.txt ] && echo "  ARMAD-Saint: $(cat /tmp/acp-job-armadsaint.txt)"
echo ""
echo "Next: ecosystem-job-monitor.sh drains each agent's event file and both sides act (set-budget, fund, submit, complete)."
