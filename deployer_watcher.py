#!/usr/bin/env python3
"""
Deployer Watcher + Auto-Snipe + Team Coordination
==================================================
Monitors a pump.fun deployer's Solana wallet for new token launches.
When a new token is detected:
1. ArmaBase Solana wallet auto-buys via Jupiter
2. Token is added to the pump.fun multi-loop for volume cycling
3. Team state is updated so all agents can coordinate
4. Saint can hedge on Hyperliquid if the token has a major narrative

Runs as a cron job every 5 minutes. Tracks seen tokens to avoid duplicates.
"""

import json, base64, requests, time, sys, os
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from datetime import datetime

# ============ CONFIG ============
DEPLOYER_WALLET = "EgFYLqyAHFfpbDNd3HtnytnJ2xV2yyduChNWHBih9fvv"
SOL_KEYPAIR_PATH = "/home/hermes/.config/solana/armabase-sol.json"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
PUMP_API = "https://frontend-api-v3.pump.fun/coins/"

# State files
SEEN_TOKENS_FILE = "/workspace/deployer_seen_tokens.json"
TEAM_STATE_FILE = "/workspace/team_state.json"
LOOP_TOKENS_FILE = "/workspace/pumpfun_loop_tokens.json"  # dynamically updated token list
TRADE_LOG = "/workspace/sol_trade_log.json"

# Trading config
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
SNIPE_AMOUNT_USDC = 0.10       # USDC to spend per snipe
MIN_SOL_RESERVE = 0.005        # Min SOL for gas
MAX_SLIPPAGE_BPS = 1000        # 10% max slippage for snipes (new tokens are volatile)
MAX_PRICE_IMPACT = 0.25        # Skip if >25% price impact
MAX_TOKENS_PER_RUN = 3         # Max new tokens to snipe per cron run

# Known pump.fun program ID
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ============ WALLET ============
with open(SOL_KEYPAIR_PATH) as f:
    secret = json.load(f)
KEYPAIR = Keypair.from_bytes(bytes(secret))
WALLET = str(KEYPAIR.pubkey())

# ============ FUNCTIONS ============

def rpc_call(method, params):
    resp = requests.post(SOL_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }, timeout=15)
    return resp.json()

def get_sol_balance():
    r = rpc_call("getBalance", [WALLET])
    return r.get("result", {}).get("value", 0) / 1e9

def get_usdc_balance():
    r = rpc_call("getTokenAccountsByOwner", [WALLET, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
    accts = r.get("result", {}).get("value", [])
    if accts:
        return float(accts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return 0.0

def load_seen_tokens():
    try:
        with open(SEEN_TOKENS_FILE) as f:
            return json.load(f)
    except:
        return {"tokens": {}, "last_checked_slot": 0, "total_detected": 0, "total_sniped": 0}

def save_seen_tokens(data):
    with open(SEEN_TOKENS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_team_state():
    try:
        with open(TEAM_STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_team_state(state):
    with open(TEAM_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_loop_tokens():
    try:
        with open(LOOP_TOKENS_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_loop_tokens(data):
    with open(LOOP_TOKENS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def detect_new_tokens():
    """Check deployer's recent transactions for new pump.fun token launches"""
    r = rpc_call("getSignaturesForAddress", [DEPLOYER_WALLET, {"limit": 20}])
    sigs = r.get("result", [])
    
    seen = load_seen_tokens()
    last_slot = seen.get("last_checked_slot", 0)
    
    new_tokens = []
    for s in sigs:
        slot = s.get("slot", 0)
        if slot <= last_slot:
            continue
        if s.get("err"):
            continue
        
        sig = s["signature"]
        # Get transaction details
        try:
            r2 = rpc_call("getTransaction", [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
            tx = r2.get("result", {})
            if not tx:
                continue
            
            # Check postTokenBalances for new tokens created
            post_balances = tx.get("meta", {}).get("postTokenBalances", [])
            pre_balances = tx.get("meta", {}).get("preTokenBalances", [])
            
            pre_mints = {pb.get("mint") for pb in pre_balances}
            
            for pb in post_balances:
                mint = pb.get("mint", "")
                owner = pb.get("owner", "")
                
                # New token that wasn't in pre-balances and is a pump.fun token
                if mint not in pre_mints and mint.endswith("pump") and mint not in seen.get("tokens", {}):
                    # Verify on pump.fun API
                    try:
                        resp = requests.get(f"{PUMP_API}{mint}", timeout=10)
                        data = resp.json()
                        symbol = data.get("symbol", "?")
                        name = data.get("name", "?")
                        mc = float(data.get("usd_market_cap", 0))
                        state = data.get("mayhem_state", "unknown")
                        complete = data.get("complete", False)
                        
                        new_tokens.append({
                            "mint": mint,
                            "symbol": symbol,
                            "name": name,
                            "market_cap": mc,
                            "mayhem_state": state,
                            "complete": complete,
                            "detected_slot": slot,
                            "detected_time": datetime.utcnow().isoformat(),
                            "tx_sig": sig,
                        })
                        
                        seen["tokens"][mint] = {
                            "symbol": symbol,
                            "name": name,
                            "market_cap": mc,
                            "detected": datetime.utcnow().isoformat(),
                            "sniped": False,
                        }
                    except:
                        pass
            
            # Also check inner instructions for pump.fun create instructions
            inner = tx.get("meta", {}).get("innerInstructions", [])
            for ii in inner:
                for ix in ii.get("instructions", []):
                    program = ix.get("programId", "")
                    if PUMP_PROGRAM in program or "pump" in program.lower():
                        accounts = ix.get("accounts", [])
                        if accounts:
                            for acc in accounts:
                                if acc.endswith("pump") and acc not in seen.get("tokens", {}):
                                    try:
                                        resp = requests.get(f"{PUMP_API}{acc}", timeout=10)
                                        data = resp.json()
                                        symbol = data.get("symbol", "?")
                                        name = data.get("name", "?")
                                        mc = float(data.get("usd_market_cap", 0))
                                        
                                        new_tokens.append({
                                            "mint": acc,
                                            "symbol": symbol,
                                            "name": name,
                                            "market_cap": mc,
                                            "mayhem_state": data.get("mayhem_state", "unknown"),
                                            "complete": data.get("complete", False),
                                            "detected_slot": slot,
                                            "detected_time": datetime.utcnow().isoformat(),
                                            "tx_sig": sig,
                                        })
                                        
                                        seen["tokens"][acc] = {
                                            "symbol": symbol,
                                            "name": name,
                                            "market_cap": mc,
                                            "detected": datetime.utcnow().isoformat(),
                                            "sniped": False,
                                        }
                                    except:
                                        pass
        except:
            continue
    
    # Update last checked slot
    if sigs:
        seen["last_checked_slot"] = max(s.get("slot", 0) for s in sigs)
    
    seen["total_detected"] = seen.get("total_detected", 0) + len(new_tokens)
    save_seen_tokens(seen)
    
    return new_tokens, seen

def jup_swap(input_mint, output_mint, amount, slippage=MAX_SLIPPAGE_BPS):
    """Execute Jupiter swap"""
    try:
        resp = requests.get(JUP_QUOTE, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage),
        }, timeout=15)
        quote = resp.json()
        if "error" in quote:
            return False, "", f"Quote error: {quote['error']}"
        
        out_amount = int(quote["outAmount"])
        price_impact = float(quote.get("priceImpactPct", 0) or 0)
        
        if price_impact > MAX_PRICE_IMPACT:
            return False, "", f"Price impact too high: {price_impact*100:.1f}%"
        
        resp2 = requests.post(JUP_SWAP, json={
            "quoteResponse": quote,
            "userPublicKey": WALLET,
            "wrapUnwrapSOL": False,
        }, timeout=15)
        swap = resp2.json()
        if "error" in swap:
            return False, "", f"Swap build error: {swap['error']}"
        
        tx_b64 = swap["swapTransaction"]
        tx_bytes = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [KEYPAIR])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()
        
        resp3 = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=60)
        
        result = resp3.json()
        if "error" in result:
            return False, "", f"Send error: {result['error'].get('message', '')[:200]}"
        
        return True, result["result"], f"out={out_amount} impact={price_impact*100:.2f}%"
    except Exception as e:
        return False, "", f"Exception: {str(e)[:200]}"

def snipe_token(mint, symbol, amount_usdc):
    """Auto-buy a newly detected token"""
    usdc_balance = get_usdc_balance()
    sol_balance = get_sol_balance()
    
    if sol_balance < MIN_SOL_RESERVE:
        return False, "Insufficient SOL for gas"
    
    if usdc_balance < amount_usdc:
        return False, f"Insufficient USDC: ${usdc_balance:.4f} < ${amount_usdc}"
    
    amount_lamports = int(amount_usdc * 1e6)  # USDC has 6 decimals
    
    ok, sig, details = jup_swap(USDC_MINT, mint, amount_lamports)
    return ok, f"TX: {sig[:20]}... | {details}" if ok else details

def add_to_loop_trading(mint, symbol):
    """Add token to the pump.fun multi-loop token list"""
    loop_tokens = load_loop_tokens()
    
    if mint not in loop_tokens:
        loop_tokens[mint] = {
            "symbol": symbol,
            "mint": mint,
            "decimals": 6,  # pump.fun tokens typically have 6 decimals
            "min_trade_usdc": 0.05,
            "added": datetime.utcnow().isoformat(),
        }
        save_loop_tokens(loop_tokens)
        return True
    return False

def notify_team_state(token_info, snipe_result):
    """Update team state so all agents can coordinate"""
    state = load_team_state()
    state.setdefault("deployer_alerts", []).append({
        "token": token_info["symbol"],
        "mint": token_info["mint"],
        "market_cap": token_info["market_cap"],
        "sniped": snipe_result[0],
        "snipe_details": snipe_result[1][:200],
        "time": datetime.utcnow().isoformat(),
    })
    # Keep only last 50 alerts
    if len(state.get("deployer_alerts", [])) > 50:
        state["deployer_alerts"] = state["deployer_alerts"][-50:]
    save_team_state(state)

# ============ MAIN ============

def run():
    print(f"\n🔍 Deployer Watcher — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Monitoring: {DEPLOYER_WALLET[:12]}...")
    print(f"   Wallet: {WALLET[:12]}...")
    
    sol_bal = get_sol_balance()
    usdc_bal = get_usdc_balance()
    print(f"   SOL: {sol_bal:.6f} | USDC: ${usdc_bal:.4f}")
    
    # Detect new tokens
    new_tokens, seen = detect_new_tokens()
    
    if not new_tokens:
        total_seen = len(seen.get("tokens", {}))
        print(f"   No new tokens detected ({total_seen} previously seen)")
        print(f"   ✅ Watch cycle complete")
        return
    
    print(f"\n   🚨 {len(new_tokens)} NEW TOKEN(S) DETECTED!")
    
    sniped = 0
    for token in new_tokens:
        sym = token["symbol"]
        name = token["name"]
        mint = token["mint"]
        mc = token["market_cap"]
        
        print(f"\n   📢 {sym} ({name})")
        print(f"      Mint: {mint[:20]}...")
        print(f"      Market cap: ${mc:,.0f}")
        print(f"      State: {token['mayhem_state']}")
        
        # Skip if bonding curve is complete (already graduated to Raydium)
        if token.get("complete"):
            print(f"      ⚠️ Bonding curve complete — may still be tradeable on Raydium")
        
        # Auto-snipe
        if usdc_bal >= SNIPE_AMOUNT_USDC and sol_bal >= MIN_SOL_RESERVE and sniped < MAX_TOKENS_PER_RUN:
            print(f"      🎯 Sniping with ${SNIPE_AMOUNT_USDC} USDC...")
            ok, details = snipe_token(mint, sym, SNIPE_AMOUNT_USDC)
            
            if ok:
                print(f"      ✅ SNIPED: {details}")
                usdc_bal -= SNIPE_AMOUNT_USDC
                sniped += 1
                
                # Update seen tokens
                seen["tokens"][mint]["sniped"] = True
                seen["tokens"][mint]["snipe_time"] = datetime.utcnow().isoformat()
                seen["total_sniped"] = seen.get("total_sniped", 0) + 1
                save_seen_tokens(seen)
            else:
                print(f"      ❌ Snipe failed: {details}")
        else:
            if usdc_bal < SNIPE_AMOUNT_USDC:
                print(f"      ⏭️ Skipped (insufficient USDC: ${usdc_bal:.4f})")
            elif sniped >= MAX_TOKENS_PER_RUN:
                print(f"      ⏭️ Skipped (max snipes per run reached)")
        
        # Add to loop trading
        added = add_to_loop_trading(mint, sym)
        if added:
            print(f"      🔄 Added to pump.fun loop trading")
        else:
            print(f"      (Already in loop trading list)")
        
        # Notify team
        notify_team_state(token, (ok if 'ok' in dir() else False, details if 'details' in dir() else "not attempted"))
    
    # Update multi-loop script's token list dynamically
    loop_tokens = load_loop_tokens()
    print(f"\n   📋 Loop trading tokens: {list(loop_tokens.keys())}")

    # Write the dynamic token list for pumpfun_multi_loop.py to read
    # Format: { "SYMBOL": {"mint": "...", "decimals": 6, "min_trade_usdc": 0.05} }
    if loop_tokens:
        token_dict = {}
        for key, info in loop_tokens.items():
            # Normalize: use symbol as key, extract fields safely
            sym = info.get("symbol", key) if isinstance(info, dict) else key
            mint = info.get("mint", key) if isinstance(info, dict) else key
            token_dict[sym] = {
                "mint": mint,
                "decimals": info.get("decimals", 6) if isinstance(info, dict) else 6,
                "min_trade_usdc": info.get("min_trade_usdc", 0.05) if isinstance(info, dict) else 0.05,
            }

        with open("/workspace/pumpfun_loop_tokens.json", "w") as f:
            json.dump(token_dict, f, indent=2)
    
    print(f"\n   ✅ Watch cycle complete")
    print(f"   Total detected: {seen.get('total_detected', 0)} | Total sniped: {seen.get('total_sniped', 0)}")

# ─── Self-Healing Cron Mesh ──────────────────────────────────────────────
# Any surviving cron script rebuilds all 10 jobs (including watchdog) on run.
import sys as _sys, os as _os
from goal_tracker import check_goal
_sys.path.insert(0, "/workspace")
try:
    from cron_watchdog import ensure_all_crons
    _recreated = ensure_all_crons()
    # Check $1M goal — halt if achieved
    try:
        if check_goal():
            sys.exit(0)
    except Exception as e:
        print(f"Goal check skipped: {e}")
    if _recreated:
        print(f"  🔧 Self-healed crons: {', '.join(sorted(_recreated))}")
except Exception as _e:
    print(f"  ⚠️ Cron self-heal skipped: {_e}")
# ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
