# Armadillo Ecosystem — Autonomous Trading Reports

This directory contains auto-generated reports from the Armadillo agent ecosystem.

## Agents
- **ArmaBase** (ARBA) — Token safety, on-chain analytics, 27 offerings
- **Scout** (ARRB) — Mirror analytics node, 27 offerings  
- **Saint** (ARMAD) — Hyperliquid perps trader, live positions

## Goal
$1,000,000 USD worth of SOL sent to personal wallet EqVPjRp8D7tFakVFN9xCukBuPqcM1bdhZ5Jr5yVT3bjd

## Tokens Tracked
- ARBA (ArmaBase token, Base chain)
- ARMAD (Saint token, Base chain)
- ARRB (Scout token, Robinhood chain)
- OGSAINT (0xfde1f1255683772d48b12b082fd3140713d6e40d, Base chain)
- Pump.fun loop tokens: BAI, ARBA, ARMASA, DUECES, SLPN, YAGA

## Reports
- `profit_engine_*.json` — Trading cycle results from all agents
- `learning_report_*.json` — Self-learning analysis and auto-corrections
- `oversight_*.json` — System health and status

## Cron Jobs (12, all recurring ∞)
- cron-watchdog (3m) — Self-healing mesh
- deployer-watcher (5m) — pump.fun snipe
- oversight (10m) — Master monitor
- sniper-guard (10m) — Position manager
- pumpfun-loop (20m) — Token cycle
- team-coordinator (15m) — Agent coordination
- saint-perps (30m) — HL perps trading
- scout-ecosystem (45m) — Scout jobs
- revenue-engine (30m) — Revenue collection
- learning-engine (1h) — Strategy learning
- buyback-burn (1h) — Token buyback & burn
- growth-engine (2h) — Growth metrics
