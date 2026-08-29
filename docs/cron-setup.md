# Cron Setup Commands

All commands require the Hermes CLI:

```bash
export PYTHONPATH='/home/hermes/.hermes/home/.local/lib/python3.11/site-packages:$PYTHONPATH'
HERMES="/opt/hermes-agent/hermes"
```

## Create all cron jobs

> **Self-healing**: Once the watchdog cron is running it auto-pulls all other scripts
> from GitHub on every 15-minute cycle and injects corrections (e.g. missing `--repeat -1`,
> missing crons) without any manual intervention.

```bash
# 1. Watchdog (15m, script-only) — must be created first; it recreates everything else
$HERMES cron create "15m" "Run the ecosystem watchdog auto-healer. Execute: bash ~/.hermes/scripts/watchdog.sh" --name "watchdog" --no-agent --script watchdog.sh --workdir /workspace
$HERMES cron edit <ID> --repeat -1

# 2. Oversight Manager (2h, AI)
$HERMES cron create "2h" "Run the OVERSIGHT MANAGER. Execute: bash ~/.hermes/scripts/oversight-manager.sh. Analyze all agent states, fix problems, rewrite broken scripts, rebalance funds, log to ~/.hermes/data/oversight-log.jsonl" --name "oversight-manager" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1

# 3. Profit Engines (4h each, AI)
$HERMES cron create "4h" "Run profit engine for OGSAINT. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019f9f75-130e-75fc-9459-5358c8d25206 OGSAINT. Analyze output, execute trades, log to ~/.hermes/data/profit-engine-OGSAINT.jsonl" --name "profit-engine-ogsaint" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1

$HERMES cron create "4h" "Run profit engine for ArmaBase. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019fbb50-31de-7e2f-be3b-2225023960b3 ArmaBase. Analyze output, execute trades, log to ~/.hermes/data/profit-engine-ArmaBase.jsonl" --name "profit-engine-armabase" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1

$HERMES cron create "4h" "Run profit engine for Scout. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019fa674-7be2-72ce-956c-3a7f831e9102 Scout. Analyze output, execute trades, log to ~/.hermes/data/profit-engine-Scout.jsonl" --name "profit-engine-scout" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1

$HERMES cron create "4h" "Run profit engine for ARMAD-Saint. Execute: bash ~/.hermes/scripts/profit-engine-agent.sh 019f9f75-493a-7011-b547-aa9c2df1a1ac ARMAD-Saint. Analyze output, execute trades, log to ~/.hermes/data/profit-engine-ARMAD-Saint.jsonl" --name "profit-engine-armad" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1

# 4. Swap Loop (6h, script)
$HERMES cron create "6h" "Run ecosystem swap loop. Execute ~/.hermes/scripts/ecosystem-swap-cron.sh" --name "ecosystem-swap-loop" --no-agent --script ecosystem-swap-cron.sh --workdir /workspace
$HERMES cron edit <ID> --repeat -1

# 5. Job Monitor (30m, AI)
$HERMES cron create "30m" "Run bash ~/.hermes/scripts/ecosystem-job-monitor.sh. Drain each /tmp/acp-events-*.jsonl. Providers set-budget 0.01 USDC and submit; client funds and completes. Agents talk over Virtuals ACP." --name "ecosystem-job-monitor" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1

# 6. Job Creator (12h, AI)
$HERMES cron create "12h" "Create ACP marketplace jobs hiring other Armadillo ecosystem agents. Hire ArmaBase for Quick Token Brief on OGSAINT, Scout for Quick Token Snapshot, ARMAD Saint for perp signal. Use acp client create-job." --name "ecosystem-job-create" --skill acp-cli --workdir /workspace
$HERMES cron edit <ID> --repeat -1
```

> **After creating the watchdog**, it will automatically detect and recreate any other
> missing cron jobs on its next 15-minute cycle. You can also create them manually below.

## Manually create remaining cron jobs (optional — watchdog handles this)

See the commands in the section above for each cron. All require `--repeat -1` to run forever.

## Agent IDs

- OGSAINT: `019f9f75-130e-75fc-9459-5358c8d25206`
- ArmaBase: `019fbb50-31de-7e2f-be3b-2225023960b3`
- Scout: `019fa674-7be2-72ce-956c-3a7f831e9102`
- ARMAD-Saint: `019f9f75-493a-7011-b547-aa9c2df1a1ac`

## Token Addresses

- OGSAINT: `0xfde1F1255683772D48b12b082fd3140713d6e40d` (Base 8453)
- ARMAD: `0x69E71cE955373D7117394B0C7aAEE6eF42cF6D51` (Base 8453)
- ARBA: `0x557642685ce68F3975458375B51553871807e1b5` (Base 8453)
- ARRB: `0xdA3C5b4d05c40a9244E534a966A1424C51055950` (Robinhood 4663)
