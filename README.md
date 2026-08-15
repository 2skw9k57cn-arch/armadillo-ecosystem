# Armadillo Ecosystem

A multi-agent autonomous trading system built on Virtuals Protocol.

## Agents

| Agent | Token | Role |
|-------|-------|------|
| **ArmaBase** | ARBA (`0x557642685ce68F3975458375B51553871807e1b5`) | Token safety, on-chain analytics |
| **Saint** | ARMAD (`0x69e71ce955373d7117394b0c7aaee6ef42cf6d51`) | Hyperliquid perps trader |
| **Scout** | ARRB (`0xdA3C5b4d05c40a9244E534a966A1424C51055950`) | Mirror analytics node, cross-chain operator |

## System Components

- **Engines**: `profit_engine.py`, `revenue_engine.py`, `growth_engine.py`, `learning_engine.py`, `volume_engine.py`, `treasury_engine.py`
- **Trading**: `sol_trading_bot.py`, `pumpfun_trade.py`, `pumpfun_loop.py`, `saint_perps.py`
- **Management**: `team_coordinator.py`, `cron_watchdog.py`, `oversight.py`
- **Utilities**: `buyback_burn.py`, `git_autosync.py`, `goal_tracker.py`

## Setup

```bash
pip install -r requirements.txt
```

Set the following environment variables before running:

```
PRIVATE_KEY        # Solana wallet private key
EVM_PRIVATE_KEY    # EVM wallet private key
RPC_URL            # Solana RPC endpoint
```

## Running

Each engine/agent is a standalone Python script. The system uses cron jobs to schedule 12 recurring tasks including profit collection, buyback/burn, and learning cycles.

```bash
python profit_engine.py
python volume_engine.py
python buyback_burn.py
```

## Architecture

The system accumulates SOL profits toward a $1M USD goal tracked by `goal_tracker.py`. Revenue flows through `revenue_engine.py` → `treasury_engine.py`. Buyback/burn operations are managed by `buyback_burn.py` on a scheduled basis.

## Hermes Virtual Mobile Bridge

`hermes_virtual_bridge.py` provides a single callable workflow for running selected Armadillo actions from Hermes, including phone-triggered flows through Virtuals.

### Runtime env

Set these in the Hermes runtime:

```bash
export HERMES_VIRTUAL_RPC_URL="https://your-rpc-endpoint"
# optional
export HERMES_VIRTUAL_TARGET="virtual_mobile_ui"  # or hermes_backend_trigger
export HERMES_VIRTUAL_TIMEOUT_SEC="120"
```

### Register capability version

```bash
python hermes_virtual_bridge.py register-capability --version v1 --activate
python hermes_virtual_bridge.py list-capabilities
```

### Invoke from mobile/backend target

```bash
# explicit target confirmation
python hermes_virtual_bridge.py invoke \
  --target virtual_mobile_ui \
  --action health_check

# async execution + phone-safe status polling
python hermes_virtual_bridge.py invoke \
  --target hermes_backend_trigger \
  --action watchdog_sync \
  --async

python hermes_virtual_bridge.py status --job-id <job_id>
```

### Actions exposed

- `health_check`
- `watchdog_sync`
- `treasury_cycle`
- `volume_cycle`
- `learning_cycle`

### Rollback / version switch

```bash
python hermes_virtual_bridge.py activate-version --version <previous_version>
```
