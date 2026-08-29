#!/usr/bin/env bash
# Start ACP event listeners for all 4 agents — NEVER STOPS
# The watchdog restarts this every 5 minutes if it dies.
# Each listener runs in its own subshell with the correct agent context

# OGSAINT
(
  acp agent use --agent-id 019f9f75-130e-75fc-9459-5358c8d25206 --json >/dev/null 2>&1
  exec acp events listen --output /tmp/acp-events-OGSAINT.jsonl --json
) &

# ArmaBase
(
  acp agent use --agent-id 019fbb50-31de-7e2f-be3b-2225023960b3 --json >/dev/null 2>&1
  exec acp events listen --output /tmp/acp-events-ArmaBase.jsonl --json
) &

# Scout
(
  acp agent use --agent-id 019fa674-7be2-72ce-956c-3a7f831e9102 --json >/dev/null 2>&1
  exec acp events listen --output /tmp/acp-events-Scout.jsonl --json
) &

# ARMAD-Saint
(
  acp agent use --agent-id 019f9f75-493a-7011-b547-aa9c2df1a1ac --json >/dev/null 2>&1
  exec acp events listen --output /tmp/acp-events-ARMAD-Saint.jsonl --json
) &

# Keep script alive
wait
