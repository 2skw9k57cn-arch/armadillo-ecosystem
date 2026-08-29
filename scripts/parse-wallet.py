import sys, json

data = json.load(sys.stdin)
spendable = []
holdings = []
for t in data.get('tokens', []):
    meta = t.get('tokenMetadata', {})
    sym = meta.get('symbol', '')
    decimals = meta.get('decimals', 18) or 18
    raw = int(t.get('tokenBalance', '0x0'), 16)
    human = raw / (10**decimals)
    if human == 0:
        continue
    price = 0
    if t.get('tokenPrices'):
        try:
            price = float(t['tokenPrices'][0]['value'])
        except:
            pass
    usd = human * price
    if sym in ('USDC', 'VIRTUAL'):
        spendable.append(f'{sym}={human:.2f} (${usd:.2f})')
    elif usd > 0.01 and sym not in ('www.badrp.co ✅','USAS','SOSO','TRC','GITLAWB', None):
        holdings.append(f'{sym}={human:,.0f} (${usd:.2f})')
    elif sym in ('OGSAINT','ARMAD','ARBA','ARRB') and human > 0:
        holdings.append(f'{sym}={human:,.0f}')

sep = ' | '
print(f'Spendable: {sep.join(spendable)}')
print(f'Holdings: {sep.join(holdings[:10])}')

stocks = data.get('stocks', [])
if stocks:
    stock_strs = []
    for s in stocks:
        t = s.get('ticker', '?')
        sh = s.get('shares', 0)
        stock_strs.append(f'{t}={sh}')
    print(f'Stocks: {sep.join(stock_strs)}')

hl = data.get('hyperliquid', {})
if hl:
    bal = hl.get('balanceUsd', 0)
    positions = hl.get('positions', [])
    print(f'HL balance: ${bal}, positions: {len(positions)}')
