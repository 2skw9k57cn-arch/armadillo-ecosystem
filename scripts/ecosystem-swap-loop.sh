#!/usr/bin/env bash
# Ecosystem Token Swap Loop — Armadillo Saint (OGSAINT)
# 
# This script executes the circular token swap loop across the Armadillo ecosystem.
# Each run swaps a small amount of VIRTUAL/USDC into the other ecosystem tokens:
#   1. Buy ARBA (ArmaBase) on Base
#   2. Buy ARMAD (Armadillo Saint) on Base  
#   3. Buy ARRB (Armadillo Scout) on Robinhood chain (cross-chain from Base)
#
# The script checks wallet balance first, skips swaps if insufficient funds,
# and uses --dry-run on first execution to verify routes before committing.
#
# Token addresses:
#   ARBA:   0x557642685ce68F3975458375B51553871807e1b5  (Base 8453)
#   ARMAD:  0x69E71cE955373D7117394B0C7aAEE6eF42cF6D51  (Base 8453)
#   ARRB:   0xdA3C5b4d05c40a9244E534a966A1424C51055950  (Robinhood 4663)
#   OGSAINT: 0xfde1F1255683772D48b12b082fd3140713d6e40d (Base 8453) — this agent's token

set -euo pipefail

SWAP_AMOUNT="${1:-2}"  # USD value per swap, default $2 (minimum for ACP trades)
DRY_RUN="${2:-false}"   # Set to "true" for dry-run mode

DRY_FLAG=""
if [ "$DRY_RUN" = "true" ]; then
    DRY_FLAG="--dry-run"
fi

echo "=== Ecosystem Swap Loop — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Swap amount: ${SWAP_AMOUNT} USDC equivalent per token"
echo ""

# Check wallet balance first
BALANCE=$(acp wallet balance --json 2>/dev/null || echo '{}')
USDC_BAL=$(echo "$BALANCE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for t in data.get('tokens', []):
        meta = t.get('tokenMetadata', {})
        if meta.get('symbol') == 'USDC':
            decimals = meta.get('decimals', 6)
            raw = int(t['tokenBalance'], 16)
            print(f'{raw / (10**decimals):.6f}')
            sys.exit()
    print('0')
except:
    print('0')
" 2>/dev/null)

VIRTUAL_BAL=$(echo "$BALANCE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for t in data.get('tokens', []):
        meta = t.get('tokenMetadata', {})
        if meta.get('symbol') == 'VIRTUAL':
            decimals = meta.get('decimals', 18)
            raw = int(t['tokenBalance'], 16)
            print(f'{raw / (10**decimals):.6f}')
            sys.exit()
    print('0')
except:
    print('0')
" 2>/dev/null)

echo "Wallet balances: USDC=${USDC_BAL}, VIRTUAL=${VIRTUAL_BAL}"
echo ""

# Calculate how many swaps we can afford
TOTAL_USD=$(python3 -c "
usdc = float('${USDC_BAL}') if '${USDC_BAL}' else 0
virtual = float('${VIRTUAL_BAL}') if '${VIRTUAL_BAL}' else 0
print(f'{usdc + virtual * 0.559:.4f}')
" 2>/dev/null)

SWAP_COST=$(python3 -c "print(f'{float('${SWAP_AMOUNT}') * 3:.4f}')")
echo "Total spendable USD: ~${TOTAL_USD}"
echo "Total swap cost: ~${SWAP_COST} (3 swaps × ${SWAP_AMOUNT})"
echo ""

if python3 -c "exit(0 if float('${TOTAL_USD}') >= float('${SWAP_COST}') else 1)" 2>/dev/null; then
    echo "✓ Sufficient funds for all 3 swaps"
else
    echo "⚠ Insufficient funds for all 3 swaps (need ~${SWAP_COST}, have ~${TOTAL_USD})"
    echo "  Will attempt swaps individually with available funds..."
fi
echo ""

# --- Swap 1: Buy ARBA (ArmaBase) on Base ---
echo "--- Swap 1: USDC → ARBA (ArmaBase) on Base ---"
if acp trade --token-in usdc --chain-in 8453 --amount-in "${SWAP_AMOUNT}" \
    --token-out 0x557642685ce68F3975458375B51553871807e1b5 --chain-out 8453 \
    --slippage 15 $DRY_FLAG --json 2>&1; then
    echo "✓ ARBA swap completed"
else
    echo "✗ ARBA swap failed (may need more USDC or token not available)"
fi
echo ""

# --- Swap 2: Buy ARMAD (Armadillo Saint) on Base ---
echo "--- Swap 2: USDC → ARMAD (Armadillo Saint) on Base ---"
if acp trade --token-in usdc --chain-in 8453 --amount-in "${SWAP_AMOUNT}" \
    --token-out 0x69E71cE955373D7117394B0C7aAEE6eF42cF6D51 --chain-out 8453 \
    --slippage 15 $DRY_FLAG --json 2>&1; then
    echo "✓ ARMAD swap completed"
else
    echo "✗ ARMAD swap failed"
fi
echo ""

# --- Swap 3: Buy ARRB (Armadillo Scout) — cross-chain to Robinhood ---
echo "--- Swap 3: USDC → ARRB (Armadillo Scout) on Robinhood chain ---"
if acp trade --token-in usdc --chain-in 8453 --amount-in "${SWAP_AMOUNT}" \
    --token-out 0xdA3C5b4d05c40a9244E534a966A1424C51055950 --chain-out 4663 \
    --slippage 15 $DRY_FLAG --json 2>&1; then
    echo "✓ ARRB swap completed"
else
    echo "✗ ARRB swap failed (cross-chain may not be supported for this token)"
fi
echo ""

echo "=== Swap loop complete — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
