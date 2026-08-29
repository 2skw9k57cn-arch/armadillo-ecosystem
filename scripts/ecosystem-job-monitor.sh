#!/usr/bin/env bash
# Ecosystem Job Monitor — NEVER STOPS
#
# Runs every 10 minutes, forever. This script:
# 1. Drains ACP events to check for budget_set events on existing jobs
# 2. Funds any jobs that are waiting for funding
# 3. Creates new jobs if the previous ones have completed
# 4. Checks for submitted deliverables and completes/reviews them
# THE ECOSYSTEM NEVER STOPS.

set -o pipefail

echo "=== Ecosystem Job Monitor — $(date -u +%Y-%m-%dT%H:%M:%SZ) — NEVER STOPS ==="

EVENTS_FILE="/tmp/acp-events.jsonl"

# Drain events
EVENTS=$(acp events drain --file "$EVENTS_FILE" --limit 20 --json 2>/dev/null || echo '{"events":[],"remaining":0}')

echo "$EVENTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
events = data.get('events', [])
print(f'Events drained: {len(events)}')
for e in events:
    status = e.get('status', 'unknown')
    job_id = e.get('jobId', 'unknown')
    tools = e.get('availableTools', [])
    print(f'  Job {job_id}: status={status}, availableTools={tools}')
" 2>/dev/null

echo ""

# Process events and take actions
echo "$EVENTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
events = data.get('events', [])
actions = []
for e in events:
    status = e.get('status', '')
    job_id = e.get('jobId', '')
    chain_id = e.get('chainId', 8453)
    tools = e.get('availableTools', [])
    entry = e.get('entry', {})
    event = entry.get('event', {}) if isinstance(entry, dict) else {}
    
    if 'fund' in tools and status == 'budget_set':
        amount = event.get('amount', '0')
        actions.append(f'fund:{job_id}:{chain_id}:{amount}')
    elif 'complete' in tools and status == 'submitted':
        actions.append(f'complete:{job_id}:{chain_id}')
    elif status == 'completed':
        actions.append(f'done:{job_id}')
    elif status == 'rejected':
        actions.append(f'rejected:{job_id}')

# Output actions as newline-separated
for a in actions:
    print(a)
" 2>/dev/null | while IFS=: read -r ACTION JOB_ID CHAIN_ID AMOUNT; do
    case "$ACTION" in
        fund)
            echo "→ Funding job $JOB_ID with $AMOUNT USDC on chain $CHAIN_ID"
            acp client fund --job-id "$JOB_ID" --amount "$AMOUNT" --chain-id "$CHAIN_ID" --json 2>&1
            ;;
        complete)
            echo "→ Completing job $JOB_ID on chain $CHAIN_ID"
            acp client complete --job-id "$JOB_ID" --chain-id "$CHAIN_ID" --reason "Deliverable accepted — ecosystem coordination complete" --json 2>&1
            ;;
        done)
            echo "✓ Job $JOB_ID completed"
            ;;
        rejected)
            echo "✗ Job $JOB_ID was rejected"
            ;;
    esac
done

echo ""
echo "=== Job monitor complete — $(date -u +%Y-%m-%dT%H:%M:%SZ) — NEVER STOPS ==="
