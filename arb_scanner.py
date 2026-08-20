#!/usr/bin/env python3
"""
Cross-Chain Arbitrage Scanner
==============================
Scans for price discrepancies between Base, Ethereum, Arbitrum, and Hyperliquid.
When a spread exceeds fees + slippage, executes the arbitrage via ACP trade.

Strategy:
1. Get token prices on each chain via DexScreener + ACP trade dry-run
2. Compare prices across chains for tradable tokens
3. If spread > estimated costs (bridge fee + gas + slippage), execute:
   - Buy on cheaper chain
   - Sell on more expensive chain (cross-chain auto-bridged by ACP)
4. Log all opportunities and executions

Tokens scanned: VIRTUAL, USDC variants, WETH, and ecosystem tokens
Runs every 20 minutes via cron.
"""

import json, time, os, subprocess, requests
from datetime import datetime, timezone

# ============ CONFIG ============
BASE_CHAIN = 8453
ETH_CHAIN = 1
ARB_CHAIN = 42161
HL_CHAIN = 1337

# Tokens to scan for arb opportunities
SCAN_TOKENS = {
    "VIRTUAL": {
        "base": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
        "eth": None,  # Check if available on ETH
        "arb": None,
        "hl": None,
    },
    "WETH": {
        "base": "0x4200000000000000000000000000000000000006",
        "eth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "arb": "0x912CE59144191C1204E64559FE8253a0e49E6548",
        "hl": None,
    },
}

# Minimum spread to attempt arb (after fees)
MIN_SPREAD_PCT = 2.0  # 2% minimum after est. costs
ESTIMATED_COSTS_PCT = 1.5  # Bridge + gas + slippage estimate
MAX_TRADE_USDC = 5.0  # Max per arb trade (capital limited)

ARB_LOG = "/workspace/arb_log.json"
ARMA_BASE_ID = "019fbb50-31de-7e2f-be3b-2225023960b3"

DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/"

# ============ UTILITIES ============
def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1

def log(action, details):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details
    }
    log_data = []
    if os.path.exists(ARB_LOG):
        try:
            with open(ARB_LOG) as f:
                log_data = json.load(f)
        except:
            pass
    log_data.append(entry)
    log_data = log_data[-200:]  # Keep last 200
    with open(ARB_LOG, "w") as f:
        json.dump(log_data, f, indent=2)
    print(f"[{entry['timestamp'][:19]}] {action}: {json.dumps(details)[:200]}")

def get_dexscreener_price(token_address, chain="base"):
    """Get token price from DexScreener"""
    try:
        url = f"{DEXSCREENER_API}{token_address}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            pairs = data.get("pairs", [])
            if pairs:
                # Filter for the right chain and find best price
                chain_pairs = [p for p in pairs if p.get("chainId") == chain]
                if not chain_pairs:
                    chain_pairs = pairs
                # Sort by liquidity (highest first)
                chain_pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
                best = chain_pairs[0]
                price = float(best.get("priceUsd", 0))
                liquidity = float(best.get("liquidity", {}).get("usd", 0))
                return price, liquidity
    except Exception as e:
        pass
    return 0, 0

def get_acp_swap_quote(token_in, chain_in, token_out, chain_out, amount_in):
    """Get a dry-run quote from ACP trade"""
    cmd = f"acp trade --token-in {token_in} --chain-in {chain_in} --amount-in {amount_in} --token-out {token_out} --chain-out {chain_out} --dry-run --json 2>&1"
    out, err, rc = run(cmd, timeout=30)
    try:
        # Parse JSON from output
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        data = json.loads(raw)
        return data
    except:
        return None

def use_armabase():
    run(f"acp agent use --agent-id {ARMA_BASE_ID}")

def get_usdc_balance():
    """Get ArmaBase USDC balance on Base"""
    out, _, _ = run("acp wallet balance --chain-id 8453 --json 2>&1", timeout=20)
    try:
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        d = json.loads(raw)
        for t in d.get("tokens", []):
            sym = t.get("tokenMetadata", {}).get("symbol") or "NATIVE"
            if sym == "USDC":
                bal_raw = t.get("tokenBalance", "0")
                dec = t.get("tokenMetadata", {}).get("decimals", 18)
                val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw)
                return val / (10 ** dec)
    except:
        pass
    return 0.0

# ============ MAIN ============
def scan_arbitrage():
    print("\n🔄 Cross-Chain Arbitrage Scanner —", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    use_armabase()
    usdc = get_usdc_balance()
    print(f"   USDC available: ${usdc:.2f}")

    opportunities = []

    # Scan VIRTUAL across chains
    # Get VIRTUAL price on Base via DexScreener
    virtual_base_addr = SCAN_TOKENS["VIRTUAL"]["base"]
    price_base, liq_base = get_dexscreener_price(virtual_base_addr, "base")
    print(f"   VIRTUAL on Base: ${price_base:.6f} (liq: ${liq_base:,.0f})")

    # Get VIRTUAL price via ACP dry-run swap (Base → other chains)
    if usdc >= 1.0 and price_base > 0:
        # Check buying VIRTUAL on Base vs other chains
        # Dry-run: USDC(base) → VIRTUAL(base) — baseline
        quote_base = get_acp_swap_quote("usdc", BASE_CHAIN, "virtual", BASE_CHAIN, min(usdc, MAX_TRADE_USDC))
        
        # Dry-run: USDC(base) → VIRTUAL via cross-chain to see if routing differs
        # ACP auto-bridges, so we can check USDC(eth) → VIRTUAL(base) etc.
        
        if quote_base:
            try:
                raw = json.dumps(quote_base)
                print(f"   Quote USDC→VIRTUAL on Base: {raw[:200]}")
            except:
                pass

    # Scan ecosystem tokens for cross-chain spreads
    ecosystem_tokens = {
        "ARBA": {"base": "0x557642685ce68F3975458375B51553871807e1b5"},
        "ARMAD": {"base": "0x69e71ce955373d7117394b0c7aaee6ef42cf6d51"},
        "OGSAINT": {"base": "0xfde1f1255683772d48b12b082fd3140713d6e40d"},
    }

    for sym, info in ecosystem_tokens.items():
        price, liq = get_dexscreener_price(info["base"], "base")
        if price > 0:
            print(f"   {sym} on Base: ${price:.8f} (liq: ${liq:,.0f})")
            
            # Check if token is also on other chains via DexScreener search
            try:
                search_url = f"https://api.dexscreener.com/latest/dex/search?q={sym}"
                r = requests.get(search_url, timeout=10)
                if r.status_code == 200:
                    pairs = r.json().get("pairs", [])
                    chain_prices = {}
                    for p in pairs[:20]:
                        chain_id = p.get("chainId", "")
                        p_price = float(p.get("priceUsd", 0))
                        p_liq = float(p.get("liquidity", {}).get("usd", 0))
                        if p_price > 0 and p_liq > 100:  # Min liquidity
                            if chain_id not in chain_prices or p_liq > chain_prices[chain_id][1]:
                                chain_prices[chain_id] = (p_price, p_liq)
                    
                    if len(chain_prices) > 1:
                        prices_sorted = sorted(chain_prices.items(), key=lambda x: x[1][0])
                        cheapest_chain, (cheap_price, cheap_liq) = prices_sorted[0]
                        expensive_chain, (exp_price, exp_liq) = prices_sorted[-1]
                        spread_pct = ((exp_price - cheap_price) / cheap_price) * 100
                        
                        if spread_pct > MIN_SPREAD_PCT + ESTIMATED_COSTS_PCT:
                            opp = {
                                "token": sym,
                                "buy_chain": cheapest_chain,
                                "buy_price": cheap_price,
                                "sell_chain": expensive_chain,
                                "sell_price": exp_price,
                                "spread_pct": round(spread_pct, 2),
                                "buy_liquidity": cheap_liq,
                                "sell_liquidity": exp_liq,
                                "est_profit_pct": round(spread_pct - ESTIMATED_COSTS_PCT, 2),
                            }
                            opportunities.append(opp)
                            print(f"   🎯 {sym}: {spread_pct:.1f}% spread — buy on {cheapest_chain} (${cheap_price:.8f}), sell on {expensive_chain} (${exp_price:.8f})")
            except Exception as e:
                pass

    # Scan HL spot vs on-chain for BTC/ETH
    try:
        # Get HL BTC spot price
        r = requests.post("https://api.hyperliquid.xyz/info", json={
            "type": "l2Book",
            "coin": "BTC"
        }, timeout=10)
        if r.status_code == 200:
            book = r.json()
            # Get mid price from orderbook
            levels = book.get("levels", [])
            if levels:
                best_bid = float(levels[0].get("px", 0))
                best_ask = float(levels[0].get("px", 0))
                hl_btc_price = (best_bid + best_ask) / 2
                
                # Get on-chain BTC price via DexScreener (WBTC on Base)
                wbtc_base = "0x296471F6be0523C57d6592B6a7DaC59f9a93d5d3"
                onchain_price, _ = get_dexscreener_price(wbtc_base, "base")
                
                if hl_btc_price > 0 and onchain_price > 0:
                    spread = abs(hl_btc_price - onchain_price) / min(hl_btc_price, onchain_price) * 100
                    print(f"   BTC: HL=${hl_btc_price:.2f} vs Base WBTC=${onchain_price:.2f} — spread {spread:.2f}%")
                    if spread > MIN_SPREAD_PCT + ESTIMATED_COSTS_PCT:
                        opportunities.append({
                            "token": "BTC",
                            "buy_venue": "HL" if hl_btc_price < onchain_price else "Base",
                            "sell_venue": "Base" if hl_btc_price < onchain_price else "HL",
                            "spread_pct": round(spread, 2),
                            "est_profit_pct": round(spread - ESTIMATED_COSTS_PCT, 2),
                        })
                        print(f"   🎯 BTC arb: {spread:.1f}% spread")
    except Exception as e:
        print(f"   BTC HL scan error: {e}")

    # Log results
    log("scan_complete", {
        "usdc_available": usdc,
        "opportunities_found": len(opportunities),
        "opportunities": opportunities,
    })

    # Execute best opportunity if we have capital
    if opportunities and usdc >= 1.0:
        best = max(opportunities, key=lambda x: x.get("est_profit_pct", 0))
        print(f"\n   ⚡ Best opportunity: {best}")
        # TODO: Execute when capital is sufficient
        # For now, just log — execution requires careful slippage management
        log("opportunity_found", best)
    elif opportunities:
        print(f"\n   Found {len(opportunities)} opportunities but insufficient USDC (${usdc:.2f})")
    else:
        print("\n   No profitable arbitrage opportunities found this cycle")

    return opportunities

if __name__ == "__main__":
    scan_arbitrage()
