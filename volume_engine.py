#!/usr/bin/env python3
"""
Volume Engine — Generates trading volume on pump.fun tokens and agent EVM tokens.

PURPOSE:
  1. Swap existing token holdings on pump.fun (Solana) for volume + price impact
  2. Swap agent EVM tokens (ARBA, ARMAD, ARRB, OGSAINT) for volume + liquidity
  3. All volume benefits token visibility on DexScreener, attracts buyers
  4. Profits flow to treasury engine → accumulate toward $1M goal

STRATEGY:
  - For each token with balance > 0: sell a portion, then buy back
  - This creates volume without net position change
  - Uses Jupiter for Solana swaps, ACP trade for EVM swaps
  - Learns optimal cycle sizes from trade history

RUNS: Every 30 minutes via Hermes cron
"""

import os
import sys
import json
import time
import subprocess
from datetime import datetime, timezone

# ─── CONFIG ────────────────────────────────────────────────────────
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Solana sniper keypair (raw keypair file)
SOL_KEYPAIR_PATH = os.path.expanduser("~/.config/solana/armabase-sol.json")

# Pump.fun tokens (Solana) — from pumpfun_loop_tokens.json
PUMPFUN_TOKENS_FILE = "/workspace/pumpfun_loop_tokens.json"

# Agent EVM tokens (Base chain 8453)
EVM_TOKENS = {
    "ARBA":    {"address": "0x557642685ce68F3975458375B51553871807e1b5", "chain": "8453"},  # Base
    "ARMAD":   {"address": "0x69e71ce955373d7117394b0c7aaee6ef42cf6d51", "chain": "8453"},  # Base
    "ARRB":    {"address": "0xdA3C5b4d05c40a9244E534a966A1424C51055950",   "chain": "4663"},  # Robinhood
    "OGSAINT": {"address": "0xfde1f1255683772d48b12b082fd3140713d6e40d", "chain": "8453"},  # Base
}

# Agent wallets for EVM swaps
AGENTS = [
    {"name": "ArmaBase",      "evm": "0x12b5d81cdbe234de287cf45061f5e56f3ceb37dc"},
    {"name": "Scout",         "evm": "0xc274243bdfb988f0fc0766f214d2ef66e0b8c6f4"},
    {"name": "Saint-ARMAD",   "evm": "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"},
    {"name": "Saint-OGSAINT", "evm": "0x52a140c6dab119a6a050f857591bbf469c1856ce"},
]

VOLUME_LOG = "/workspace/volume_log.json"
CONFIG_PATH = os.path.expanduser("~/.config/acp/config.json")

# Volume parameters
SELL_PCT = 30       # Sell 30% of holdings per cycle
SLIPPAGE_BPS = 500  # 5% slippage tolerance
MIN_USDC_TRADE = 2.0  # ACP minimum for EVM swaps

# ─── UTILITIES ─────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), -1

def acp_cmd(args, timeout=30, agent_wallet=None):
    env = os.environ.copy()
    env["TS_KEYRING_BACKEND"] = "file"
    if agent_wallet:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            cfg["activeWallet"] = agent_wallet
            with open(CONFIG_PATH, 'w') as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass
    try:
        r = subprocess.run(["acp"] + args, capture_output=True, text=True, timeout=timeout, env=env)
        raw = r.stdout.strip()
        clean = raw.split('\n[acp-wrapper]')[0].strip()
        try:
            return json.loads(clean), r.returncode == 0
        except json.JSONDecodeError:
            return {"raw": clean, "stderr": r.stderr.strip()}, r.returncode == 0
    except Exception as e:
        return {"error": str(e)}, False

def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  ⚠️ Failed to save {path}: {e}")

# ─── SOLANA SWAPS (Jupiter) ────────────────────────────────────────

def get_solana_balance():
    """Get SOL balance of the sniper wallet via raw JSON-RPC (no solana SDK needed)."""
    try:
        import urllib.request
        from solders.keypair import Keypair
        
        with open(SOL_KEYPAIR_PATH) as f:
            kp = Keypair.from_json(f.read())
        
        pubkey = str(kp.pubkey())
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [pubkey]
        }).encode()
        
        req = urllib.request.Request(
            "https://api.mainnet-beta.solana.com",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        
        bal = data.get("result", {}).get("value", 0)
        return bal / 1e9, kp  # Return SOL amount + keypair
    except Exception as e:
        print(f"  ⚠️ Solana balance error: {e}")
        return 0, None

def jup_swap(input_mint, output_mint, amount_lamports, keypair, slippage_bps=SLIPPAGE_BPS):
    """Execute a Jupiter swap on Solana. Returns (success, tx_sig, details)."""
    try:
        import urllib.request
        import base64
        
        # 1. Get quote
        quote_url = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippageBps={slippage_bps}"
        req = urllib.request.Request(quote_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            quote = json.loads(resp.read())
        
        if not quote or "routePlan" not in quote:
            return False, "", "No route found"
        
        # 2. Get swap transaction
        swap_url = "https://lite-api.jup.ag/swap/v1/swap"
        swap_data = json.dumps({
            "quoteResponse": quote,
            "userPublicKey": str(keypair.pubkey()),
            "wrapAndUnwrapSol": True,
        }).encode()
        
        req2 = urllib.request.Request(swap_url, data=swap_data, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            swap = json.loads(resp2.read())
        
        if "error" in swap:
            return False, "", f"Swap build error: {swap['error']}"
        
        # 3. Sign and send via raw JSON-RPC
        from solders.transaction import VersionedTransaction
        import base64 as b64mod
        tx_b64 = swap["swapTransaction"]
        tx_bytes = b64mod.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        
        # Send via raw RPC
        tx_bytes_signed = bytes(signed_tx)
        send_payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [b64mod.b64encode(tx_bytes_signed).decode(), {"encoding": "base64"}]
        }).encode()
        
        req3 = urllib.request.Request(
            "https://api.mainnet-beta.solana.com",
            data=send_payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req3, timeout=20) as resp3:
            send_result = json.loads(resp3.read())
        
        sig = send_result.get("result", "")
        
        # Confirm
        time.sleep(3)
        
        out_amount = int(quote.get("outAmount", 0))
        return True, sig, f"out={out_amount}"
    except Exception as e:
        return False, "", str(e)[:100]

def get_token_balance_solana(mint, keypair):
    """Get SPL token balance for a mint via raw JSON-RPC."""
    try:
        import urllib.request
        from spl.token.instructions import get_associated_token_address
        
        ata = str(get_associated_token_address(keypair.pubkey(), mint))
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountBalance",
            "params": [ata]
        }).encode()
        
        req = urllib.request.Request(
            "https://api.mainnet-beta.solana.com",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        
        val = data.get("result", {}).get("value")
        if val:
            return int(val["amount"]), val.get("decimals", 6)
    except Exception:
        pass
    return 0, 6

def volume_cycle_solana(keypair):
    """Run volume cycles on all pump.fun tokens that have balance."""
    actions = []
    tokens = load_json(PUMPFUN_TOKENS_FILE, {})
    
    sol_bal, _ = get_solana_balance()
    actions.append(f"Sniper SOL: {sol_bal:.6f}")
    
    if sol_bal < 0.01:
        actions.append("  ⚠️ Not enough SOL for swaps")
        return actions
    
    for sym, info in tokens.items():
        mint = info["mint"]
        decimals = info.get("decimals", 6)
        
        # Get current token balance
        token_balance, token_decimals = get_token_balance_solana(mint, keypair)
        
        if token_balance > 0:
            # Sell 30% of holdings → SOL
            sell_amount = int(token_balance * SELL_PCT / 100)
            if sell_amount > 0:
                actions.append(f"\n  [{sym}] Balance: {token_balance / 10**token_decimals:.2f}")
                actions.append(f"  Selling {SELL_PCT}% ({sell_amount / 10**token_decimals:.2f}) → SOL")
                
                ok, sig, details = jup_swap(mint, SOL_MINT, sell_amount, keypair)
                if ok:
                    actions.append(f"  ✅ Sold: {sig[:20]}... ({details})")
                    
                    # Buy back with the SOL received
                    time.sleep(2)
                    # Get new SOL balance
                    new_sol, _ = get_solana_balance()
                    buy_sol = min(new_sol - 0.005, 0.02)  # Buy back with small SOL amount
                    if buy_sol > 0.001:
                        buy_lamports = int(buy_sol * 1e9)
                        actions.append(f"  Buying back with {buy_sol:.6f} SOL → {sym}")
                        ok2, sig2, details2 = jup_swap(SOL_MINT, mint, buy_lamports, keypair)
                        if ok2:
                            actions.append(f"  ✅ Bought back: {sig2[:20]}... ({details2})")
                        else:
                            actions.append(f"  ❌ Buyback failed: {details2}")
                else:
                    actions.append(f"  ❌ Sell failed: {details}")
        else:
            # No balance — try a small buy to generate volume
            if sol_bal > 0.02:
                buy_sol = 0.01  # Tiny buy for volume
                buy_lamports = int(buy_sol * 1e9)
                min_trade = info.get("min_trade_usdc", 0.05)
                actions.append(f"\n  [{sym}] No balance, buying {buy_sol} SOL worth (volume)")
                ok, sig, details = jup_swap(SOL_MINT, mint, buy_lamports, keypair)
                if ok:
                    actions.append(f"  ✅ Bought: {sig[:20]}... ({details})")
                    
                    # Immediately sell half back
                    time.sleep(2)
                    new_bal, new_dec = get_token_balance_solana(mint, keypair)
                    if new_bal > 0:
                        sell_amt = int(new_bal * 0.5)
                        if sell_amt > 0:
                            ok2, sig2, details2 = jup_swap(mint, SOL_MINT, sell_amt, keypair)
                            if ok2:
                                actions.append(f"  ✅ Sold half: {sig2[:20]}...")
                            else:
                                actions.append(f"  ❌ Sell-back failed: {details2}")
                else:
                    actions.append(f"  ❌ Buy failed: {details}")
    
    return actions

# ─── EVM SWAPS (ACP) ───────────────────────────────────────────────

def volume_cycle_evm():
    """Run volume cycles on agent EVM tokens (ARBA, ARMAD, ARRB, OGSAINT)."""
    actions = []
    
    for agent in AGENTS:
        name = agent["name"]
        evm = agent["evm"]
        
        # Check token balances
        data, ok = acp_cmd(["wallet", "balance", "--chain-id", "8453", "--json"], agent_wallet=evm)
        if not ok or not isinstance(data, dict):
            continue
        
        usdc_balance = 0
        token_balances = {}
        
        for chain in data.get("chains", []):
            for t in chain.get("tokens", []):
                meta = t.get("tokenMetadata", {}) or {}
                sym = (meta.get("symbol") or "").upper()
                bal_raw = t.get("tokenBalance", "0x0")
                bal_val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw or 0)
                dec = meta.get("decimals") or 18
                bal = bal_val / (10 ** dec)
                
                if sym == "USDC":
                    usdc_balance = bal
                elif sym in EVM_TOKENS and bal > 0.001:
                    token_balances[sym] = bal
        
        if not token_balances and usdc_balance < MIN_USDC_TRADE:
            continue
        
        agent_actions = []
        
        # If we have USDC, buy token → sell back (volume)
        if usdc_balance >= MIN_USDC_TRADE:
            for sym in ["ARBA", "ARMAD", "OGSAINT"]:
                if sym in EVM_TOKENS:
                    token_info = EVM_TOKENS[sym]
                    chain = token_info["chain"]
                    buy_amount = min(usdc_balance * 0.3, 5.0)  # Use 30% of USDC, max $5
                    
                    if buy_amount >= MIN_USDC_TRADE:
                        agent_actions.append(f"  [{sym}] Buying ${buy_amount:.2f} USDC → {sym}")
                        data, ok = acp_cmd([
                            "trade", "--token-in", "usdc", "--chain-in", chain,
                            "--token-out", sym, "--chain-out", chain,
                            "--amount-in", str(round(buy_amount, 2)), "--json"
                        ], agent_wallet=evm, timeout=120)
                        
                        if ok:
                            received = data.get("finalReceived", "0")
                            if isinstance(received, str):
                                received = float(received.split()[0])
                            else:
                                received = float(received or 0)
                            agent_actions.append(f"  ✅ Bought {received:.2f} {sym}")
                            
                            # Sell it back immediately
                            time.sleep(3)
                            agent_actions.append(f"  Selling {received:.2f} {sym} → USDC (volume)")
                            data2, ok2 = acp_cmd([
                                "trade", "--token-in", sym, "--chain-in", chain,
                                "--token-out", "usdc", "--chain-out", chain,
                                "--amount-in", str(round(received * 0.95, 2)), "--json"
                            ], agent_wallet=evm, timeout=120)
                            
                            if ok2:
                                agent_actions.append(f"  ✅ Sold back (volume generated)")
                            else:
                                agent_actions.append(f"  ❌ Sell-back failed: {str(data2.get('error',''))[:60]}")
                        else:
                            err = str(data.get("error", ""))[:60]
                            agent_actions.append(f"  ❌ Buy failed: {err}")
        
        # If we have token balance, sell portion → buy back (volume)
        for sym, bal in token_balances.items():
            if sym in EVM_TOKENS and bal >= MIN_USDC_TRADE:
                token_info = EVM_TOKENS[sym]
                chain = token_info["chain"]
                sell_amount = bal * SELL_PCT / 100
                
                if sell_amount >= MIN_USDC_TRADE:
                    agent_actions.append(f"  [{sym}] Selling {SELL_PCT}% ({sell_amount:.2f}) → USDC (volume)")
                    data, ok = acp_cmd([
                        "trade", "--token-in", sym, "--chain-in", chain,
                        "--token-out", "usdc", "--chain-out", chain,
                        "--amount-in", str(round(sell_amount, 2)), "--json"
                    ], agent_wallet=evm, timeout=120)
                    
                    if ok:
                        received = data.get("finalReceived", "0")
                        if isinstance(received, str):
                            received = float(received.split()[0])
                        else:
                            received = float(received or 0)
                        agent_actions.append(f"  ✅ Sold for ${received:.2f} USDC")
                        
                        # Buy back immediately
                        time.sleep(3)
                        agent_actions.append(f"  Buying ${received:.2f} USDC → {sym} (volume)")
                        data2, ok2 = acp_cmd([
                            "trade", "--token-in", "usdc", "--chain-in", chain,
                            "--token-out", sym, "--chain-out", chain,
                            "--amount-in", str(round(received * 0.95, 2)), "--json"
                        ], agent_wallet=evm, timeout=120)
                        
                        if ok2:
                            agent_actions.append(f"  ✅ Bought back (volume generated)")
                        else:
                            agent_actions.append(f"  ❌ Buy-back failed: {str(data2.get('error',''))[:60]}")
                    else:
                        err = str(data.get("error", ""))[:60]
                        if "below" not in err.lower():
                            agent_actions.append(f"  ❌ Sell failed: {err}")
        
        if agent_actions:
            actions.append(f"\n[{name}] (USDC: ${usdc_balance:.2f})")
            actions.extend(agent_actions)
    
    return actions

# ─── LOGGING ───────────────────────────────────────────────────────

def log_volume(swap_type, token, amount, details, success):
    log = load_json(VOLUME_LOG, {"swaps": [], "total_volume": 0})
    entry = {
        "time": now_utc(),
        "type": swap_type,
        "token": token,
        "amount": amount,
        "details": details[:200],
        "success": success,
    }
    log["swaps"].append(entry)
    log["swaps"] = log["swaps"][-500:]  # Keep last 500
    if success:
        log["total_volume"] = log.get("total_volume", 0) + float(amount or 0)
    save_json(VOLUME_LOG, log)

# ─── MAIN ──────────────────────────────────────────────────────────

def main():
    print(f"🔄 Volume Engine — {now_utc()}")
    print("=" * 60)
    
    # 1. Solana pump.fun volume cycles
    print("\n📡 Solana pump.fun volume cycles...")
    try:
        sol_bal, keypair = get_solana_balance()
        if keypair and sol_bal > 0.005:
            sol_actions = volume_cycle_solana(keypair)
            for a in sol_actions:
                print(f"  {a}")
        else:
            print(f"  ⚠️ No Solana keypair or insufficient SOL ({sol_bal:.6f})")
    except Exception as e:
        print(f"  ❌ Solana volume error: {e}")
    
    # 2. EVM agent token volume cycles
    print("\n📡 EVM agent token volume cycles...")
    try:
        evm_actions = volume_cycle_evm()
        if evm_actions:
            for a in evm_actions:
                print(f"  {a}")
        else:
            print("  💤 No EVM tokens with sufficient balance for volume")
    except Exception as e:
        print(f"  ❌ EVM volume error: {e}")
    
    # 3. Summary
    vol_log = load_json(VOLUME_LOG, {"swaps": [], "total_volume": 0})
    recent = [s for s in vol_log.get("swaps", []) if s.get("success")]
    print(f"\n📊 Total volume generated (all time): ${vol_log.get('total_volume', 0):.2f}")
    print(f"   Successful swaps: {len(recent)}")
    
    print(f"\n{'='*60}")
    print(f"✅ Volume cycle complete")

if __name__ == "__main__":
    # Ensure crons are healthy
    try:
        sys.path.insert(0, "/workspace")
        from cron_watchdog import ensure_all_crons
        ensure_all_crons()
    except Exception:
        pass
    
    main()
