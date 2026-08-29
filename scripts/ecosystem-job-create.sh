#!/usr/bin/env bash
# Ecosystem Job Creation — NEVER STOPS
#
# Runs every 3 hours, forever. Creates ACP marketplace jobs targeting
# other ecosystem agents to drive activity and earn USDC.
# THE ECOSYSTEM NEVER STOPS.

set -o pipefail

echo "=== Ecosystem Job Creation — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""

# Start event listener if not already running
# Use pgrep -x on the exact binary name to avoid false positives from multiline
# output or the grep process matching itself.
LISTENER_COUNT=$(pgrep -f "acp events listen" 2>/dev/null | grep -c . || true)
if [ ! -f /tmp/acp-events.jsonl ] || [ "${LISTENER_COUNT}" -eq 0 ]; then
    acp events listen --output /tmp/acp-events.jsonl --json &
    EVENT_LISTENER_PID=$!
    echo "Started event listener (PID: $EVENT_LISTENER_PID)"
    sleep 2
else
    echo "Event listener already running"
fi
echo ""

# --- Job 1: Hire ArmaBase for OGSAINT Token Analysis ---
echo "--- Job 1: Hire ArmaBase — Quick Token Brief for OGSAINT ---"
JOB1=$(acp client create-job \
    --provider 0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc \
    --offering-name "Quick Token Brief" \
    --requirements '{"token_address":"0xfde1F1255683772D48b12b082fd3140713d6e40d","chain":"base"}' \
    --chain-id 8453 --json 2>&1)
echo "$JOB1"
JOB1_ID=$(echo "$JOB1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null || echo "")
if [ -n "$JOB1_ID" ]; then
    echo "✓ Job created: $JOB1_ID"
    echo "$JOB1_ID" > /tmp/acp-job-arbasaint.txt
else
    echo "✗ Job creation failed"
fi
echo ""

# --- Job 2: Hire Armadillo Scout for OGSAINT Snapshot ---
echo "--- Job 2: Hire Armadillo Scout — Quick Token Snapshot for OGSAINT ---"
JOB2=$(acp client create-job \
    --provider 0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4 \
    --offering-name "Quick Token Snapshot" \
    --requirements '{"token_address":"0xfde1F1255683772D48b12b082fd3140713d6e40d","chain":"base"}' \
    --chain-id 8453 --json 2>&1)
echo "$JOB2"
JOB2_ID=$(echo "$JOB2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null || echo "")
if [ -n "$JOB2_ID" ]; then
    echo "✓ Job created: $JOB2_ID"
    echo "$JOB2_ID" > /tmp/acp-job-scoutsaint.txt
else
    echo "✗ Job creation failed"
fi
echo ""

# --- Job 3: Hire Armadillo Saint (ARMAD) for OGSAINT perp signal ---
echo "--- Job 3: Hire Armadillo Saint (ARMAD) — Perp Signal mentioning OGSAINT ecosystem ---"
JOB3=$(acp client create-job \
    --provider 0x73d1486635fe66b3fff1db69a289a3e3fa625f8d \
    --offering-name "hyperliquid_perp_signal" \
    --requirements '{"pair":"BTC","timeframe":"4h","risk_preference":"balanced","extra_notes":"Context: OGSAINT ecosystem agent coordinating cross-token activity. Consider market conditions relevant to Virtuals Protocol ecosystem tokens."}' \
    --chain-id 8453 --json 2>&1)
echo "$JOB3"
JOB3_ID=$(echo "$JOB3" | python3 -c "import sys,json; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null || echo "")
if [ -n "$JOB3_ID" ]; then
    echo "✓ Job created: $JOB3_ID"
    echo "$JOB3_ID" > /tmp/acp-job-armadsaint.txt
else
    echo "✗ Job creation failed"
fi
echo ""

echo "=== Job creation complete — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""
echo "Job IDs:"
[ -f /tmp/acp-job-arbasaint.txt ] && echo "  ArmaBase: $(cat /tmp/acp-job-arbasaint.txt)"
[ -f /tmp/acp-job-scoutsaint.txt ] && echo "  Scout: $(cat /tmp/acp-job-scoutsaint.txt)"
[ -f /tmp/acp-job-armadsaint.txt ] && echo "  ARMAD-Saint: $(cat /tmp/acp-job-armadsaint.txt)"
echo ""
echo "Next: monitor events.jsonl for budget_set events, then fund the jobs."
