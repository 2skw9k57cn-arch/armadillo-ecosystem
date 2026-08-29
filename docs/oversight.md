# Armadillo Ecosystem

Autonomous multi-agent token ecosystem on Virtuals Protocol. 4 agents, 4 tokens, self-learning profit engines, and a permanent auto-healing watchdog.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    WATCHDOG (15m)                        │
│         Pure bash auto-healer — no AI needed             │
│   Restarts dead listeners, recreates missing crons,      │
│   checks scripts, detects depleted wallets               │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              OVERSIGHT MANAGER (2h)                      │
│           AI-driven ecosystem coordinator                │
│   Rewrites broken scripts, rebalances funds,             │
│   cancels stuck jobs, adjusts parameters                 │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────┬───────────┼───────────┬──────────────────────┐
│          │           │           │                       │
▼          ▼           ▼           ▼                       │
OGSAINT   ArmaBase    Scout    ARMAD-Saint          Swap Loop (6h)
4h AI      4h AI      4h AI      4h AI              Buy+Sell volume
engine     engine     engine     engine             
│          │           │           │                       │
└──────────┴───────────┴───────────┘                       │
                    │                                      │
           ┌────────▼────────┐                    ┌────────▼────────┐
           │  Job Monitor    │                    │  Job Creator    │
           │    (30m)        │                    │    (12h)        │
           │ Fund + complete │                    │ Hire agents     │
           └─────────────────┘                    └─────────────────┘
```

## Agents

| Agent | Token | Chain | Wallet |
|-------|-------|-------|--------|
| OGSAINT (this) | OGSAINT | Base 8453 | 0x52a140c6...56ce |
| ArmaBase | ARBA | Base 8453 | 0x12b5d81c...37dc |
| Scout | ARRB | Robinhood 4663 | 0xc274243b...c6f4 |
| ARMAD-Saint | ARMAD | Base 8453 | 0x73d14866...5f8d |

## Cron Jobs (9 total, all ∞)

| Job | Schedule | Type | Purpose |
|-----|----------|------|---------|
| watchdog | 15m | Script | Auto-heal listeners, crons, scripts |
| oversight-manager | 2h | AI | Monitor + repair ecosystem |
| profit-engine-ogsaint | 4h | AI | Autonomous trading for OGSAINT |
| profit-engine-armabase | 4h | AI | Autonomous trading for ArmaBase |
| profit-engine-scout | 4h | AI | Autonomous trading for Scout |
| profit-engine-armad | 4h | AI | Autonomous trading for ARMAD-Saint |
| ecosystem-swap-loop | 6h | Script | Buy+sell ecosystem tokens |
| ecosystem-job-monitor | 30m | AI | Fund + complete marketplace jobs |
| ecosystem-job-create | 12h | AI | Create marketplace jobs |

## Scripts

| Script | Purpose |
|--------|---------|
| `watchdog.sh` | Deterministic auto-healer (listeners, crons, scripts, wallets) |
| `oversight-manager.sh` | Collects ecosystem state for AI oversight agent |
| `profit-engine-agent.sh` | Collects wallet/market/job state for AI profit engine |
| `profit-engine.sh` | Original OGSAINT-only profit engine state collector |
| `ecosystem-swap-cron.sh` | Buy+sell rotation through 4 ecosystem tokens |
| `cross-agent-swap.sh` | Switch agent context and execute swaps |
| `ecosystem-job-monitor.sh` | Drain events, fund jobs, complete deliverables |
| `ecosystem-job-create.sh` | Create marketplace jobs for ecosystem agents |
| `start-listeners.sh` | Start ACP event listeners for all 4 agents |
| `parse-wallet.py` | Parse wallet balance JSON (avoids bash quoting issues) |
| `notify.sh` | Send owner alerts (Slack/Discord webhook, Telegram, email, log) |

## Notifications

The watchdog automatically notifies you whenever it detects a critical situation.
Configure one or more channels in `~/.hermes/.env` (never commit real credentials):

| Variable | Channel |
|----------|---------|
| `NOTIFY_WEBHOOK_URL` | Slack / Discord / Teams incoming webhook |
| `NOTIFY_TELEGRAM_TOKEN` + `NOTIFY_TELEGRAM_CHAT_ID` | Telegram bot |
| `NOTIFY_EMAIL` | Email via `sendmail` / `msmtp` |

All alerts are also written to `~/.hermes/data/notifications.jsonl` for the
AI oversight-manager to read and act on.

**Alert levels triggered automatically:**

| Situation | Level |
|-----------|-------|
| Wallet balance < $2 USDC | `CRITICAL` |
| ACP event listener(s) dead and restarted | `ALERT` |
| Cron job missing and recreated | `ALERT` |
| Profit engine stale > 5 h | `WARN` |
| Script file missing | `WARN` |

The AI oversight-manager can also call `notify.sh` directly when it detects
any other problems during its analysis cycle.

## Setup

1. **Install scripts**: Copy `scripts/` to `~/.hermes/scripts/`
2. **Create data dir**: `mkdir -p ~/.hermes/data`
3. **Approve signers**: `acp agent add-signer --agent-id <id> --policy unrestricted`
4. **Start listeners**: `bash ~/.hermes/scripts/start-listeners.sh &`
5. **Create crons**: See `docs/cron-setup.md` for exact commands
6. **Run watchdog**: `bash ~/.hermes/scripts/watchdog.sh`

## Key Constraints

- $2 USD minimum per swap (ACP CLI enforced)
- VIRTUAL→bonding curve buys must route through USDC (Privy signing limitation)
- All cron jobs must be set to `--repeat -1` (infinite)
- Event listeners die if shell exits — watchdog restarts them every 15m

## License

MIT
