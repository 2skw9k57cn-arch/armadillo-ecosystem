#!/usr/bin/env bash
# Ecosystem Job Monitor — NEVER STOPS
#
# Drains EACH agent's ACP event file (the ones start-listeners.sh writes)
# and takes the next Virtuals ACP action for that agent:
#   provider: set-budget (0.01 USDC) then submit a short deliverable
#   client:   fund the quoted amount, then complete
#
# This is how ArmaBase / Scout / Saint / OGSAINT actually talk to each other.

set -o pipefail

FEE="0.01"

echo "=== Ecosystem Job Monitor — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

process_agent() {
    local name="$1"
    local agent_id="$2"
    local events_file="/tmp/acp-events-${name}.jsonl"

    echo ""
    echo "── $name ──"
    if [ ! -f "$events_file" ]; then
        echo "  (no event file yet: $events_file)"
        return
    fi

    acp agent use --agent-id "$agent_id" --json >/dev/null 2>&1 || {
        echo "  ✗ acp agent use failed"
        return
    }

    local events
    events=$(acp events drain --file "$events_file" --limit 20 --json 2>/dev/null || echo '{"events":[],"remaining":0}')

    echo "$events" | python3 -c "
import sys, json
raw = sys.stdin.read() or '{}'
try:
    data = json.loads(raw)
except Exception:
    data = {}
events = data.get('events', []) if isinstance(data, dict) else []
print('  Events drained: %s (remaining %s)' % (len(events), data.get('remaining', '?') if isinstance(data, dict) else '?'))
for e in events:
    if not isinstance(e, dict):
        continue
    print('  Job %s: status=%s tools=%s roles=%s' % (
        e.get('jobId', '?'), e.get('status'), e.get('availableTools'), e.get('roles')))
" 2>/dev/null

    echo "$events" | FEE="$FEE" AGENT_NAME="$name" python3 -c "
import sys, json, os

def amount_from(e):
    for path in (
        ('entry','event','amount'),
        ('entry','event','value'),
        ('entry','event','assetToken','amount'),
        ('entry','event','fare','amount'),
        ('amount',),
    ):
        cur = e
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and cur not in (None, ''):
            return str(cur)
    return os.environ.get('FEE', '0.01')

raw = sys.stdin.read() or '{}'
try:
    data = json.loads(raw)
except Exception:
    data = {}
events = data.get('events', []) if isinstance(data, dict) else []
agent = os.environ.get('AGENT_NAME', 'agent')
fee = os.environ.get('FEE', '0.01')
seen = set()
for e in events:
    if not isinstance(e, dict):
        continue
    job_id = str(e.get('jobId') or e.get('job_id') or '')
    if not job_id:
        continue
    chain_id = e.get('chainId') or e.get('chain_id') or 8453
    tools = e.get('availableTools') or e.get('available_tools') or []
    if isinstance(tools, str):
        tools = [tools]
    tools_l = [str(t) for t in tools]
    status = str(e.get('status') or '')
    dedupe = (job_id, tuple(sorted(tools_l)), status)
    if dedupe in seen:
        continue
    seen.add(dedupe)
    lower = [t.lower().replace('_', '') for t in tools_l]
    if 'setbudget' in lower:
        print('setbudget:%s:%s:%s' % (job_id, chain_id, fee), flush=True)
    elif 'submit' in lower:
        print('submit:%s:%s:%s' % (job_id, chain_id, agent), flush=True)
    elif 'fund' in lower:
        print('fund:%s:%s:%s' % (job_id, chain_id, amount_from(e)), flush=True)
    elif 'complete' in lower:
        print('complete:%s:%s:' % (job_id, chain_id), flush=True)
    elif status in ('completed', 'rejected'):
        print('%s:%s:%s:' % (status, job_id, chain_id), flush=True)
" 2>/dev/null | while IFS=: read -r ACTION JOB_ID CHAIN_ID EXTRA; do
        case "$ACTION" in
            setbudget)
                echo "  → $name set-budget job $JOB_ID amount $EXTRA"
                acp provider set-budget --job-id "$JOB_ID" --amount "$EXTRA" --chain-id "$CHAIN_ID" --json 2>&1
                ;;
            submit)
                echo "  → $name submit job $JOB_ID"
                acp provider submit --job-id "$JOB_ID" \
                    --deliverable "{\"agent\":\"$EXTRA\",\"status\":\"ok\",\"note\":\"Armadillo ecosystem ACP coordination\"}" \
                    --chain-id "$CHAIN_ID" --json 2>&1
                ;;
            fund)
                echo "  → $name fund job $JOB_ID amount $EXTRA"
                acp client fund --job-id "$JOB_ID" --amount "$EXTRA" --chain-id "$CHAIN_ID" --json 2>&1
                ;;
            complete)
                echo "  → $name complete job $JOB_ID"
                acp client complete --job-id "$JOB_ID" --chain-id "$CHAIN_ID" \
                    --reason "Deliverable accepted — ecosystem coordination complete" --json 2>&1
                ;;
            completed)
                echo "  ✓ Job $JOB_ID completed"
                ;;
            rejected)
                echo "  ✗ Job $JOB_ID was rejected"
                ;;
        esac
    done
}

process_agent "OGSAINT"      "019f9f75-130e-75fc-9459-5358c8d25206"
process_agent "ArmaBase"     "019fbb50-31de-7e2f-be3b-2225023960b3"
process_agent "Scout"        "019fa674-7be2-72ce-956c-3a7f831e9102"
process_agent "ARMAD-Saint"  "019f9f75-493a-7011-b547-aa9c2df1a1ac"

echo ""
echo "=== Job monitor complete — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
