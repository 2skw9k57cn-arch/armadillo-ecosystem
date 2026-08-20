#!/usr/bin/env python3
"""
SOL Auto-Distributor
====================
Detects incoming SOL on the sniper wallet and distributes it across the
ecosystem for maximum return:

  1. Keep 40% as SOL → pump.fun sniper (best performer, 66% win rate)
  2. Swap 35% SOL → USDC on Solana (via Jupiter)
     → Bridge USDC Solana → Base (via deBridge)
     → Arrives as USDC on ArmaBase wallet
     → cron_watchdog then distributes to sub-agents
  3. Swap 25% SOL → USDC on Solana (via Jupiter)
     → Deposit to Hyperliquid for Saint perps + spot trading

Runs every 10 minutes via cron. Only triggers when SOL balance exceeds
the gas reserve threshold (0.02 SOL).

Wallet: CT5Z79b1ie7AaeRzEsn1uQjMz3p33xTJST7Na49UDoSL
Keypair: ~/.config/solana/armabase-sol.json
"""

import json, time, os, subprocess, base64, requests, urllib.request
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from datetime import datetime, timezone

# ============ CONFIG ============
SOL_KEYPAIR_PATH = os.path.expanduser("~/.config/solana/armabase-sol.json")
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ArmaBase wallet on Base (receives bridged USDC)
ARMABASE_EVM = "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"
# Saint wallet on Base (for HL deposits)
SAINT_EVM = "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"

# Distribution split
SNIPER_KEEP_PCT = 40   # Keep as SOL for pump.fun sniper
BRIDGE_PCT = 35        # SOL→USDC→bridge to Base (ArmaBase distributes)
HL_DEPOSIT_PCT = 25    # SOL→USDC→deposit to Hyperliquid (Saint)

# Thresholds
GAS_RESERVE_SOL = 0.02         # Keep at least this much SOL for gas
MIN_DISTRIBUTE_SOL = 0.1       # Only distribute if > 0.1 SOL available
MAX_SLIPPAGE_BPS = 500         # 5% slippage

DISTRIBUTION_LOG = "/workspace/sol_distribution_log.json"

# ============ UTILITIES ============
def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except Exception as e:
        return "", str(e), -1

def log(action, details):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details
    }
    log_data = []
    if os.path.exists(DISTRIBUTION_LOG):
        try:
            with open(DISTRIBUTION_LOG) as f:
                log_data = json.load(f)
        except:
            pass
    log_data.append(entry)
    log_data = log_data[-200:]
    with open(DISTRIBUTION_LOG, "w") as f:
        json.dump(log_data, f, indent=2)
    print(f"[{entry['timestamp'][:19]}] {action}: {json.dumps(details)[:200]}")

def load_keypair():
    with open(SOL_KEYPAIR_PATH) as f:
        kp = Keypair.from_json(f.read())
    return kp

def get_sol_balance(pubkey_str):
    """Get SOL balance via JSON-RPC"""
    try:
        data = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [pubkey_str]
        }).encode()
        req = urllib.request.Request(SOL_RPC, data=data, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return resp["result"]["value"] / 1e9
    except Exception as e:
        print(f"   ❌ Balance check failed: {e}")
        return 0.0

def get_sol_price():
    """Get SOL price in USD from DexScreener"""
    try:
        r = requests.get(f"{DEXSCREENER}{SOL_MINT}", timeout=10)
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            for p in pairs:
                if p.get("chainId") == "solana" and p.get("quoteToken", {}).get("symbol") == "USDC":
                    return float(p.get("priceUsd", 0))
    except:
        pass
    return 85.0  # fallback

def get_usdc_balance_solana(pubkey_str):
    """Get USDC balance on Solana via token accounts"""
    try:
        data = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                pubkey_str,
                {"mint": USDC_MINT},
                {"encoding": "jsonParsed"}
            ]
        }).encode()
        req = urllib.request.Request(SOL_RPC, data=data, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        accts = resp["result"]["value"]
        total = 0.0
        for a in accts:
            info = a["account"]["data"]["parsed"]["info"]
            total += float(info["tokenAmount"]["uiAmountString"])
        return total
    except:
        return 0.0

# ============ JUPITER SWAP ============
def jupiter_swap(sol_amount, input_mint, output_mint, kp, slippage_bps=MAX_SLIPPAGE_BPS):
    """Execute a swap via Jupiter aggregator. Returns (success, out_amount, tx_sig)"""
    # Get decimals for input token
    decimals = 9 if input_mint == SOL_MINT else 6
    
    # Step 1: Get quote
    amount_in_lamports = int(sol_amount * 10**decimals)
    try:
        r = requests.get(JUP_QUOTE, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_in_lamports),
            "slippageBps": slippage_bps,
        }, timeout=15)
        if r.status_code != 200:
            return False, 0, f"Quote failed: {r.status_code} {r.text[:100]}"
        quote = r.json()
        out_amount = int(quote.get("outAmount", 0))
        if out_amount == 0:
            return False, 0, "Quote returned 0 output"
    except Exception as e:
        return False, 0, f"Quote error: {e}"

    # Step 2: Get swap transaction
    try:
        r = requests.post(JUP_SWAP, json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapUnwrapSOL": True,
            "computeUnitPriceMicroLamports": 500000,  # priority fee
        }, timeout=15)
        if r.status_code != 200:
            return False, 0, f"Swap tx failed: {r.status_code} {r.text[:100]}"
        swap_data = r.json()
        swap_tx_b64 = swap_data.get("swapTransaction")
        if not swap_tx_b64:
            return False, 0, "No swapTransaction in response"
    except Exception as e:
        return False, 0, f"Swap tx error: {e}"

    # Step 3: Sign and send
    try:
        raw_tx = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [kp])
        tx_b64 = base64.b64encode(bytes(signed_tx)).decode()

        # Send via RPC
        data = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}]
        }).encode()
        req = urllib.request.Request(SOL_RPC, data=data, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())

        if "error" in resp:
            return False, 0, f"RPC error: {resp['error']}"

        sig = resp.get("result", "")
        out_amount_human = out_amount / 1e6 if output_mint == USDC_MINT else out_amount / 1e9
        return True, out_amount_human, sig
    except Exception as e:
        return False, 0, f"Send error: {e}"

# ============ DEBRIDGE BRIDGE ============
def bridge_usdc_to_base(usdc_amount, kp):
    """Bridge USDC from Solana to Base via deBridge.
    This creates a deBridge order that sends USDC from Solana to ArmaBase wallet on Base.
    Returns (success, details)."""
    # deBridge DLN API: create order
    try:
        amount_raw = int(usdc_amount * 1e6)  # USDC has 6 decimals
        r = requests.get("https://api.dln.trade/v1.0/dln/order/quote", params={
            "srcChainId": 7565164,  # Solana
            "srcChainTokenIn": USDC_MINT,
            "dstChainId": 8453,     # Base
            "dstChainTokenOut": "0x833589fCD6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
            "srcChainTokenInAmount": str(amount_raw),
            "dstChainTokenOutAmount": "auto",
            "prependOperatingMode": "src",
        }, timeout=15)

        if r.status_code != 200:
            return False, f"Quote failed: {r.status_code}"

        quote = r.json()
        est_out = quote.get("estimation", {}).get("dstChainTokenOut", {}).get("amount", "0")
        est_out_human = int(est_out) / 1e6

        # Create the order
        r2 = requests.post("https://api.dln.trade/v1.0/dln/order/create", json={
            "order": {
                "give": {
                    "chainId": 7565164,
                    "tokenAddress": USDC_MINT,
                    "amount": str(amount_raw),
                },
                "take": {
                    "chainId": 8453,
                    "tokenAddress": "0x833589fCD6edb6e08f4c7c32d4f71b54bda02913",
                    "amount": est_out,
                },
                "receiverAddr": ARMABASE_EVM,
            },
            "referrer": "armabase",
        }, timeout=20)

        if r2.status_code != 200:
            return False, f"Order creation failed: {r2.status_code} {r2.text[:100]}"

        order_data = r2.json()
        # The response should contain a Solana transaction to sign
        tx_data = order_data.get("txData") or order_data.get("transaction")
        if not tx_data:
            # Try different format
            return False, f"No tx data in response: {json.dumps(order_data)[:200]}"

        # Sign and send the transaction
        if isinstance(tx_data, str):
            raw_tx = base64.b64decode(tx_data)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [kp])
            tx_b64 = base64.b64encode(bytes(signed_tx)).decode()

            data = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [tx_b64, {"encoding": "base64", "skipPreflight": False}]
            }).encode()
            req = urllib.request.Request(SOL_RPC, data=data, headers={"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())

            if "error" in resp:
                return False, f"RPC error: {resp['error']}"
            sig = resp.get("result", "")
            return True, f"Bridged ~{est_out_human:.2f} USDC → Base (ArmaBase). TX: {sig[:20]}..."
        else:
            return False, f"Unexpected tx_data format: {type(tx_data)}"
    except Exception as e:
        return False, f"Bridge error: {e}"

# ============ HL DEPOSIT ============
def deposit_to_hl(usdc_amount):
    """Deposit USDC from Solana to Hyperliquid via ACP.
    ACP trade can bridge Solana USDC → HL deposit."""
    # Use ACP trade: USDC on Solana → USDC on HL
    # ACP handles cross-chain automatically
    cmd = f"acp trade --token-in usdc --chain-in 501 --amount-in {usdc_amount} --token-out usdc --chain-out 1337 --json 2>&1"
    out, err, rc = run(cmd, timeout=120)
    try:
        raw = out.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in out else out
        data = json.loads(raw)
        if data.get("success") or data.get("finalReceived"):
            return True, f"Deposited {usdc_amount:.2f} USDC to HL"
        return False, f"ACP response: {json.dumps(data)[:200]}"
    except:
        return False, f"ACP trade failed: {out[:200]}"

# ============ MAIN ============
def distribute_sol():
    print(f"\n💸 SOL Auto-Distributor — {now_utc()}")
    kp = load_keypair()
    pubkey = str(kp.pubkey())
    print(f"   Wallet: {pubkey}")

    sol_balance = get_sol_balance(pubkey)
    sol_price = get_sol_price()
    usdc_balance = get_usdc_balance_solana(pubkey)

    print(f"   SOL: {sol_balance:.6f} (~${sol_balance * sol_price:.2f})")
    print(f"   USDC: {usdc_balance:.6f}")
    print(f"   SOL price: ${sol_price:.2f}")

    # Check if there's SOL to distribute (beyond gas reserve)
    distributable_sol = sol_balance - GAS_RESERVE_SOL
    if distributable_sol < MIN_DISTRIBUTE_SOL:
        print(f"   ⏭️ Nothing to distribute (distributable: {distributable_sol:.6f} SOL, need >{MIN_DISTRIBUTE_SOL})")
        log("skip", {
            "sol_balance": sol_balance,
            "distributable": distributable_sol,
            "reason": "below_threshold"
        })
        return

    # Also distribute any existing USDC on the wallet
    total_usdc_to_route = usdc_balance

    # Calculate splits
    sniper_sol = distributable_sol * (SNIPER_KEEP_PCT / 100)
    bridge_sol = distributable_sol * (BRIDGE_PCT / 100)
    hl_sol = distributable_sol * (HL_DEPOSIT_PCT / 100)

    print(f"\n   📊 Distribution plan:")
    print(f"   • Sniper (keep SOL): {sniper_sol:.6f} SOL (~${sniper_sol * sol_price:.2f})")
    print(f"   • Bridge to Base:     {bridge_sol:.6f} SOL (~${bridge_sol * sol_price:.2f})")
    print(f"   • HL deposit:         {hl_sol:.6f} SOL (~${hl_sol * sol_price:.2f})")
    if total_usdc_to_route > 0.01:
        print(f"   • Existing USDC to route: ${total_usdc_to_route:.6f}")

    actions = []

    # Step 1: Keep sniper SOL (it's already on the wallet, just don't touch it)
    actions.append(f"Sniper kept: {sniper_sol:.6f} SOL")

    # Step 2: Swap bridge_sol → USDC and bridge to Base
    if bridge_sol >= 0.01:
        print(f"\n   🔄 Swapping {bridge_sol:.6f} SOL → USDC for Base bridge...")
        ok, out_amount, detail = jupiter_swap(bridge_sol, SOL_MINT, USDC_MINT, kp)
        if ok:
            print(f"   ✅ Got {out_amount:.6f} USDC")
            actions.append(f"Swapped {bridge_sol:.6f} SOL → {out_amount:.6f} USDC")

            # Bridge to Base
            if out_amount >= 1.0:  # deBridge minimum
                print(f"   🌉 Bridging {out_amount:.6f} USDC → Base (ArmaBase)...")
                bridge_ok, bridge_detail = bridge_usdc_to_base(out_amount, kp)
                if bridge_ok:
                    print(f"   ✅ {bridge_detail}")
                    actions.append(bridge_detail)
                else:
                    print(f"   ❌ Bridge failed: {bridge_detail}")
                    actions.append(f"Bridge failed: {bridge_detail}")
            else:
                print(f"   ⏭️ Bridge amount too small (${out_amount:.2f}, need $1+)")
                actions.append(f"Bridge skipped: ${out_amount:.2f} below minimum")
        else:
            print(f"   ❌ Swap failed: {detail}")
            actions.append(f"Swap failed: {detail}")

    # Step 3: Swap hl_sol → USDC and deposit to HL
    if hl_sol >= 0.01:
        print(f"\n   🔄 Swapping {hl_sol:.6f} SOL → USDC for HL deposit...")
        ok, out_amount, detail = jupiter_swap(hl_sol, SOL_MINT, USDC_MINT, kp)
        if ok:
            print(f"   ✅ Got {out_amount:.6f} USDC")
            actions.append(f"Swapped {hl_sol:.6f} SOL → {out_amount:.6f} USDC for HL")

            # Deposit to HL via ACP (Solana USDC → HL)
            if out_amount >= 1.0:
                print(f"   📥 Depositing {out_amount:.6f} USDC to Hyperliquid...")
                hl_ok, hl_detail = deposit_to_hl(out_amount)
                if hl_ok:
                    print(f"   ✅ {hl_detail}")
                    actions.append(hl_detail)
                else:
                    print(f"   ❌ HL deposit failed: {hl_detail}")
                    actions.append(f"HL deposit failed: {hl_detail}")
            else:
                print(f"   ⏭️ HL deposit too small (${out_amount:.2f})")
                actions.append(f"HL deposit skipped: ${out_amount:.2f} below minimum")
        else:
            print(f"   ❌ Swap failed: {detail}")
            actions.append(f"Swap failed: {detail}")

    # Step 4: Route any pre-existing USDC
    if total_usdc_to_route >= 1.0:
        # Split existing USDC: 60% bridge, 40% HL
        bridge_usdc = total_usdc_to_route * 0.60
        hl_usdc = total_usdc_to_route * 0.40

        if bridge_usdc >= 1.0:
            print(f"\n   🌉 Bridging existing ${bridge_usdc:.6f} USDC → Base...")
            bridge_ok, bridge_detail = bridge_usdc_to_base(bridge_usdc, kp)
            actions.append(f"Existing USDC bridge: {bridge_detail}")
            if bridge_ok:
                print(f"   ✅ {bridge_detail}")
            else:
                print(f"   ❌ {bridge_detail}")

        if hl_usdc >= 1.0:
            print(f"\n   📥 Depositing existing ${hl_usdc:.6f} USDC to HL...")
            hl_ok, hl_detail = deposit_to_hl(hl_usdc)
            actions.append(f"Existing USDC HL: {hl_detail}")
            if hl_ok:
                print(f"   ✅ {hl_detail}")
            else:
                print(f"   ❌ {hl_detail}")

    # Final balance check
    final_sol = get_sol_balance(pubkey)
    final_usdc = get_usdc_balance_solana(pubkey)
    print(f"\n   📊 After distribution:")
    print(f"   SOL: {final_sol:.6f} (~${final_sol * sol_price:.2f})")
    print(f"   USDC: {final_usdc:.6f}")

    log("distribution_complete", {
        "initial_sol": sol_balance,
        "initial_usdc": usdc_balance,
        "distributable_sol": distributable_sol,
        "sniper_sol": sniper_sol,
        "bridge_sol": bridge_sol,
        "hl_sol": hl_sol,
        "final_sol": final_sol,
        "final_usdc": final_usdc,
        "sol_price": sol_price,
        "actions": actions,
    })

if __name__ == "__main__":
    distribute_sol()
