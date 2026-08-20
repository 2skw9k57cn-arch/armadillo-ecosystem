#!/usr/bin/env python3
"""Ecosystem funding audit"""
import urllib.request, json, requests, subprocess

rpc = "https://api.mainnet-beta.solana.com"
def rpc_call(method, params):
    data = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(rpc, data=data, headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

# Get SOL price
r = requests.get("https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112", timeout=10)
sol_price = 85.0
if r.status_code == 200:
    for p in r.json().get("pairs", []):
        if p.get("chainId") == "solana":
            sol_price = float(p.get("priceUsd", 85))
            break

print("=" * 60)
print("ECOSYSTEM FUNDING AUDIT")
print("=" * 60)

# 1. Sniper wallet (Solana)
sniper = "CT5Z79b1ie7AaeRzEsn1uQjMz3p33xTJST7Na49UDoSL"
r1 = rpc_call("getBalance", [sniper])
sol = r1["result"]["value"] / 1e9
print(f"\n1. PUMP.FUN SNIPER (Solana)")
print(f"   Wallet: {sniper}")
print(f"   SOL: {sol:.6f} (~${sol*sol_price:.2f})")
status = "FUNDED" if sol > 0.1 else "LOW"
print(f"   Status: {status}")

# 2. ArmaBase
print(f"\n2. ARMABASE (Base)")
subprocess.run("acp agent use --agent-id 019fbb50-31de-7e2f-be3b-2225023960b3 2>&1", shell=True, capture_output=True, text=True, timeout=10)
r2 = subprocess.run("acp wallet balance --chain-id 8453 --json 2>&1", shell=True, capture_output=True, text=True, timeout=15)
raw2 = r2.stdout.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in r2.stdout else r2.stdout
try:
    d2 = json.loads(raw2)
    tokens = d2.get("tokens", [])
    for t in tokens:
        sym = t.get("tokenMetadata", {}).get("symbol", "?")
        bal_raw = t.get("tokenBalance", "0")
        try:
            val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw)
            dec = t.get("tokenMetadata", {}).get("decimals", 18)
            amt = val / (10**dec)
        except:
            amt = 0
        usd = t.get("usdValue", 0)
        if isinstance(usd, str):
            usd = float(usd)
        if amt > 0 or (isinstance(usd, (int,float)) and usd > 0.01):
            print(f"   {sym}: {amt:.6f} (${usd:.2f})")
except Exception as e:
    print(f"   Parse error: {e}")

# 3. Saint (Base + HL)
print(f"\n3. SAINT (Base + Hyperliquid)")
subprocess.run("acp agent use --agent-id 019f9f75-493a-7011-b547-aa9c2df1a1ac 2>&1", shell=True, capture_output=True, text=True, timeout=10)
r3 = subprocess.run("acp wallet balance --json 2>&1", shell=True, capture_output=True, text=True, timeout=15)
raw3 = r3.stdout.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in r3.stdout else r3.stdout
try:
    d3 = json.loads(raw3)
    tokens = d3.get("tokens", [])
    for t in tokens:
        sym = t.get("tokenMetadata", {}).get("symbol", "?")
        bal_raw = t.get("tokenBalance", "0")
        try:
            val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw)
            dec = t.get("tokenMetadata", {}).get("decimals", 18)
            amt = val / (10**dec)
        except:
            amt = 0
        usd = t.get("usdValue", 0)
        if isinstance(usd, str):
            usd = float(usd)
        if amt > 0 or (isinstance(usd, (int,float)) and usd > 0.01):
            print(f"   {sym}: {amt:.6f} (${usd:.2f})")
    hl = d3.get("hyperliquid", {})
    hl_bal = hl.get("balanceUsd", "0")
    hl_spot = hl.get("spotUsd", "0")
    print(f"   HL total: ${hl_bal}")
    print(f"   HL spot USDC: ${hl_spot}")
    positions = hl.get("positions", [])
    if positions:
        print(f"   HL open positions: {len(positions)}")
        for p in positions:
            token = p.get("token", "?")
            size = p.get("size", "0")
            pnl = p.get("unrealizedPnl", "0")
            print(f"     {token}: size={size} uPnL={pnl}")
except Exception as e:
    print(f"   Parse error: {e}")

# 4. Scout
print(f"\n4. SCOUT (Base)")
subprocess.run("acp agent use --agent-id 019fa674-7be2-72ce-956c-3a7f831e9102 2>&1", shell=True, capture_output=True, text=True, timeout=10)
r4 = subprocess.run("acp wallet balance --chain-id 8453 --json 2>&1", shell=True, capture_output=True, text=True, timeout=15)
raw4 = r4.stdout.split("[acp-wrapper]")[0].strip() if "[acp-wrapper]" in r4.stdout else r4.stdout
try:
    d4 = json.loads(raw4)
    tokens = d4.get("tokens", [])
    for t in tokens:
        sym = t.get("tokenMetadata", {}).get("symbol", "?")
        bal_raw = t.get("tokenBalance", "0")
        try:
            val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith("0x") else int(bal_raw)
            dec = t.get("tokenMetadata", {}).get("decimals", 18)
            amt = val / (10**dec)
        except:
            amt = 0
        usd = t.get("usdValue", 0)
        if isinstance(usd, str):
            usd = float(usd)
        if amt > 0 or (isinstance(usd, (int,float)) and usd > 0.01):
            print(f"   {sym}: {amt:.6f} (${usd:.2f})")
except Exception as e:
    print(f"   Parse error: {e}")

print(f"\n{'=' * 60}")
print(f"SOL price: ${sol_price:.2f}")
print(f"{'=' * 60}")
