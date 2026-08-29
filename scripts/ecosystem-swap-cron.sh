#!/usr/bin/env bash
# Ecosystem Swap Loop v3 — Buy + Sell, NEVER STOPS
#
# Runs every 1 hour, forever. Every cycle does BOTH:
# 1. BUY: Spend $2.01 USDC into one ecosystem token (rotation)
# 2. SELL: Sell enough of ANY ecosystem token held (≥$2 value) back to USDC
#
# THE ECOSYSTEM NEVER STOPS. Two-way volume drives bonding curve graduation.

set -o pipefail
export LC_ALL=C

echo "=== Ecosystem Swap Loop v3 — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "NEVER STOPS — runs every hour, forever."

# Token addresses
OGSAINT_ADDR="0xfde1F1255683772D48b12b082fd3140713d6e40d"
ARMAD_ADDR="0x69E71cE955373D7117394B0C7aAEE6eF42cF6D51"
ARBA_ADDR="0x557642685ce68F3975458375B51553871807e1b5"
ARRB_ADDR="0xdA3C5b4d05c40a9244E534a966A1424C51055950"

ALL_TOKENS=("OGSAINT:${OGSAINT_ADDR}:8453" "ARMAD:${ARMAD_ADDR}:8453" "ARBA:${ARBA_ADDR}:8453" "ARRB:${ARRB_ADDR}:4663")

# Get wallet balances
BALANCE_JSON=$(acp wallet balance --json 2>/dev/null || echo '{}')

# Parse USDC and VIRTUAL
read -r USDC_BAL VIRTUAL_BAL <<< $(echo "$BALANCE_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
usdc = 0
virt = 0
for t in data.get('tokens', []):
    sym = t.get('tokenMetadata', {}).get('symbol', '')
    decimals = t.get('tokenMetadata', {}).get('decimals', 18) or 18
    raw = int(t.get('tokenBalance', '0x0'), 16)
    if sym == 'USDC': usdc += raw / (10**decimals)
    elif sym == 'VIRTUAL': virt += raw / (10**decimals)
print(f'{usdc:.6f} {virt:.6f}')
" 2>/dev/null)

echo "Balances: USDC=\$${USDC_BAL} VIRTUAL=${VIRTUAL_BAL}"

DAY=$(date -u +%j)

# ============================================================
# BUY LEG — buy one ecosystem token with $2.01 USDC
# ============================================================
BUY_IDX=$((DAY % 4))
BUY_INFO="${ALL_TOKENS[$BUY_IDX]}"
BUY_NAME=$(echo "$BUY_INFO" | cut -d: -f1)
BUY_TOKEN=$(echo "$BUY_INFO" | cut -d: -f2)
BUY_CHAIN=$(echo "$BUY_INFO" | cut -d: -f3)

BUY_DONE="NO"
if python3 -c "exit(0 if float('${USDC_BAL}') >= 2.01 else 1)" 2>/dev/null; then
    echo "BUY: \$2.01 USDC → ${BUY_NAME}"
    RESULT=$(acp trade --token-in usdc --chain-in 8453 --amount-in 2.01 --token-out "${BUY_TOKEN}" --chain-out "${BUY_CHAIN}" --slippage 25 --json 2>&1)
    if echo "$RESULT" | grep -q '"status":"success"'; then
        AMOUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('output',{}).get('formatted','?'))" 2>/dev/null)
        echo "  ✅ Bought ${AMOUNT} ${BUY_NAME}"
        BUY_DONE="YES"
    else
        echo "  ❌ Buy failed — $(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')[:100])" 2>/dev/null)"
    fi
elif python3 -c "exit(0 if float('${VIRTUAL_BAL}') >= 4.0 else 1)" 2>/dev/null; then
    echo "BUY: Converting VIRTUAL → USDC → ${BUY_NAME}"
    acp trade --token-in virtual --chain-in 8453 --amount-in 4.0 --token-out usdc --chain-out 8453 --json 2>&1 >/dev/null
    RESULT=$(acp trade --token-in usdc --chain-in 8453 --amount-in 2.01 --token-out "${BUY_TOKEN}" --chain-out "${BUY_CHAIN}" --slippage 25 --json 2>&1)
    if echo "$RESULT" | grep -q '"status":"success"'; then
        echo "  ✅ Bought via VIRTUAL route"
        BUY_DONE="YES"
    else
        echo "  ❌ Buy failed via VIRTUAL route"
    fi
else
    echo "⚠ Cannot buy — need ≥\$2 USDC or ≥4 VIRTUAL. Will try to sell to get USDC."
fi

# ============================================================
# SELL LEG — sell ANY ecosystem token held (re-fetch balance)
# NEVER SKIP — always try to sell something
# ============================================================
BALANCE_JSON=$(acp wallet balance --json 2>/dev/null || echo '{}')

ECO_SELLABLE=$(echo "$BALANCE_JSON" | python3 /home/hermes/.hermes/scripts/parse-sellable.py 2>/dev/null)

SELL_START=$(( (DAY + 2) % 4 ))
SOLD_ANY="NO"

if [ -n "$ECO_SELLABLE" ]; then
    ALL_LINES=()
    while IFS= read -r line; do
        ALL_LINES+=("$line")
    done <<< "$ECO_SELLABLE"
    NUM=${#ALL_LINES[@]}

    for i in 0 1 2 3 4 5; do
        IDX=$(( (SELL_START + i) % NUM ))
        LINE="${ALL_LINES[$IDX]}"
        SELL_NAME=$(echo "$LINE" | cut -d'|' -f1)
        SELL_TOKEN=$(echo "$LINE" | cut -d'|' -f2)
        SELL_CHAIN=$(echo "$LINE" | cut -d'|' -f3)
        SELL_BAL=$(echo "$LINE" | cut -d'|' -f4)

        for PCT in 20 30 40 50 60 70 80; do
            SELL_AMOUNT=$(python3 -c "
bal = float('${SELL_BAL}')
pct = ${PCT}
print(f'{bal * pct / 100:.0f}')
" 2>/dev/null)
            if python3 -c "exit(0 if float('${SELL_AMOUNT}') > 0 else 1)" 2>/dev/null; then
                echo "SELL: ${SELL_AMOUNT} ${SELL_NAME} (${PCT}%) → USDC"
                SELL_RESULT=$(acp trade --token-in "${SELL_TOKEN}" --chain-in "${SELL_CHAIN}" --amount-in "${SELL_AMOUNT}" --token-out usdc --chain-out 8453 --slippage 25 --json 2>&1)
                if echo "$SELL_RESULT" | grep -q '"status":"success"'; then
                    GOT=$(echo "$SELL_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('output',{}).get('formatted','?'))" 2>/dev/null)
                    echo "  ✅ Sold ${SELL_AMOUNT} ${SELL_NAME} → ${GOT} USDC"
                    SOLD_ANY="YES"
                    break 2
                else
                    ERR=$(echo "$SELL_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')[:80])" 2>/dev/null)
                    if echo "$ERR" | grep -q "below the"; then
                        continue
                    else
                        echo "  ❌ Sell failed: ${ERR}"
                        break
                    fi
                fi
            fi
        done
    done
fi

if [ "$SOLD_ANY" = "NO" ]; then
    echo "⚠ No ecosystem token could be sold this cycle"
fi

# Summary
echo "---"
echo "Buy: ${BUY_DONE} | Sell: ${SOLD_ANY}"
echo "=== Loop complete — $(date -u +%Y-%m-%dT%H:%M:%SZ) — NEVER STOPS ==="
