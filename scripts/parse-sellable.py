import sys, json

data = json.load(sys.stdin)

ECO_TOKENS = {
    '0xfde1f1255683772d48b12b082fd3140713d6e40d': ('OGSAINT', '8453'),
    '0x69e71ce955373d7117394b0c7aaee6ef42cf6d51': ('ARMAD', '8453'),
    '0x557642685ce68f3975458375b51553871807e1b5': ('ARBA', '8453'),
    '0xda3c5b4d05c40a9244e534a966a1424c51055950': ('ARRB', '4663'),
}

results = []
for t in data.get('tokens', []):
    addr = (t.get('tokenAddress') or '').lower()
    if addr in ECO_TOKENS:
        name, chain = ECO_TOKENS[addr]
        decimals = t.get('tokenMetadata', {}).get('decimals', 18) or 18
        raw = int(t.get('tokenBalance', '0x0'), 16)
        human = raw / (10**decimals)
        if human > 1:  # exclude dust amounts
            results.append(f'{name}|{addr}|{chain}|{human:.0f}')

for r in results:
    print(r)
