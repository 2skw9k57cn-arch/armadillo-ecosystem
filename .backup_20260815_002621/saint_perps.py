#!/usr/bin/env python3
"""
Saint Perps Trading Engine — Self-Learning v2 (Technical Analysis)
====================================================================
Armadillo Saint — Hyperliquid perpetuals trader with full TA + adaptive learning.

Technical Indicators Used:
  - RSI (14): Oversold <30 = buy signal, Overbought >70 = sell signal
  - EMA Crossover (9/21): Golden cross = bullish, Death cross = bearish
  - Bollinger Bands (20, 2σ): Price at lower = oversold, upper = overbought
  - MACD (12/26/9): Histogram positive = bullish momentum, negative = bearish
  - Volume Spike: >1.5x average volume confirms breakouts/breakdowns
  - Funding Rate: Negative funding = shorts paying longs (bullish for longs)
  - 24h Momentum: Combined with above for trend confirmation

Self-Learning Features:
  1. Tracks every trade outcome per-asset, per-signal-type, per-leverage
  2. Adjusts position size (0.3x–1.5x), leverage (1x–5x), SL/TP by performance
  3. Learns which indicators/combos produce best results per asset
  4. Raises confidence bar after losing streaks, lowers after winning streaks
  5. Records market conditions at entry for post-trade analysis
  6. Generates actionable lessons that feed back into next cycle

Markets: Scans top liquid HL perps (BTC, ETH, SOL, DOGE, XRP, + high-movers)
Runs every 30 minutes via cron.
"""

import json, time, os, subprocess, requests, math
from datetime import datetime
from collections import defaultdict

# ============ CONFIG ============
SAINT_ID = "019f9f75-493a-7011-b547-aa9c2df1a1ac"
SAINT_WALLET = "0x73d1486635fe66b3fff1db69a289a3e3fa625f8d"
BASE_CHAIN = 8453
HL_CHAIN = 1337
HL_API = "https://api.hyperliquid.xyz/info"

# Trading config — BASE values, adjusted by learning engine
MAX_POSITION_PCT = 0.10      # Base: max 10% of HL balance per position
MAX_POSITIONS = 3             # Max concurrent positions (raised from 2 for diversification)
DEFAULT_LEVERAGE = 3
MIN_HL_BALANCE = 3.0
MIN_DEPOSIT = 5.0
STOP_LOSS_PCT = -0.05         # Base: -5%
TAKE_PROFIT_PCT = 0.10        # Base: +10%
TRAILING_STOP_ACTIVATE = 0.04 # Activate trailing stop at +4% profit
TRAILING_STOP_DISTANCE = 0.02 # Trail by 2% from peak
POSITION_LOG = "/workspace/saint_positions.json"
HL_DEPOSIT_LOG = "/workspace/saint_hl_log.json"
LEARNING_DB = "/workspace/saint_learning.json"

# Core markets (always scanned) — high liquidity, proven movers
CORE_MARKETS = ["BTC", "ETH", "SOL"]
# Extended markets (scanned for opportunities) — high OI altcoins
EXTENDED_MARKETS = ["DOGE", "XRP", "HBAR", "AVAX", "ARB", "WLD", "ENA",
                    "FARTCOIN", "PENGU", "SAGA", "PYTH", "KAS", "ADA",
                    "KAITO", "HMSTR", "PUMP", "MEME", "PEOPLE", "TRX"]
# Min OI to consider an extended market tradeable
MIN_OI_EXTENDED = 30_000_000


# ============ TECHNICAL INDICATORS ============

def get_candles(symbol, interval="1h", hours=48):
    """Fetch candle data from HL"""
    end = int(time.time() * 1000)
    if interval == "15m":
        start = end - hours * 900000
    elif interval == "4h":
        start = end - hours * 14400000
    else:  # 1h default
        start = end - hours * 3600000
    try:
        r = requests.post(HL_API, json={
            "type": "candleSnapshot",
            "req": {"coin": symbol, "interval": interval, "startTime": start, "endTime": end}
        }, timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return []

def calc_rsi(closes, period=14):
    """RSI: Relative Strength Index (0-100)"""
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(values, period):
    """EMA: Exponential Moving Average"""
    if len(values) < period:
        return sum(values) / len(values) if values else 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_bollinger(closes, period=20, std_dev=2):
    """Bollinger Bands: returns (middle, upper, lower)"""
    if len(closes) < period:
        avg = sum(closes) / len(closes) if closes else 0
        return avg, avg, avg
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = variance ** 0.5
    return sma, sma + std_dev * std, sma - std_dev * std

def calc_macd(closes, fast=12, slow=26, signal=9):
    """MACD: returns (macd_line, signal_line, histogram)"""
    if len(closes) < slow + signal:
        return 0, 0, 0
    ema_fast_vals, ema_slow_vals = [], []
    for i in range(slow, len(closes) + 1):
        chunk = closes[:i]
        ema_fast_vals.append(calc_ema(chunk, fast))
        ema_slow_vals.append(calc_ema(chunk, slow))
    macd_line = [ef - es for ef, es in zip(ema_fast_vals, ema_slow_vals)]
    signal_line = calc_ema(macd_line, signal) if len(macd_line) >= signal else sum(macd_line) / len(macd_line)
    histogram = macd_line[-1] - signal_line if macd_line else 0
    return macd_line[-1] if macd_line else 0, signal_line, histogram

def calc_atr(candles, period=14):
    """ATR: Average True Range (volatility measure)"""
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]['h'])
        l = float(candles[i]['l'])
        pc = float(candles[i-1]['c'])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

# ============ MARKET ANALYSIS ============

def get_all_markets():
    """Get all HL markets with funding/OI data"""
    try:
        r = requests.post(HL_API, json={"type": "metaAndAssetCtxs"}, timeout=15)
        data = r.json()
        universe = data[0].get('universe', [])
        ctxs = data[1]
        markets = {}
        for i, m in enumerate(universe):
            ctx = ctxs[i] if i < len(ctxs) else {}
            name = m.get('name', '')
            mark = float(ctx.get('markPx', 0))
            oi = float(ctx.get('openInterest', 0))
            funding = float(ctx.get('funding', 0))
            prev = float(ctx.get('prevDayPx', 0))
            day_change = ((mark - prev) / prev * 100) if prev > 0 and mark > 0 else 0
            markets[name] = {
                'price': mark, 'oi': oi, 'funding': funding,
                'day_change': day_change,
                'max_lev': m.get('maxLeverage', 1),
                'sz_decimals': m.get('szDecimals', 4),
                'only_isolated': m.get('onlyIsolated', False),
            }
        return markets
    except:
        return {}

def analyze_market(symbol, learning_db, market_info=None):
    """Full technical analysis of a market — returns signal dict or None.
    Uses multi-timeframe confirmation (15m + 1h alignment)."""
    print(f"\n  Analyzing {symbol}...")

    # Fetch 1h candle data
    candles_1h = get_candles(symbol, "1h", 48)
    if not candles_1h or len(candles_1h) < 20:
        print(f"    Not enough 1h candle data ({len(candles_1h) if candles_1h else 0} candles)")
        return None

    # Fetch 15m candle data for multi-timeframe confirmation
    candles_15m = get_candles(symbol, "15m", 24)
    if not candles_15m or len(candles_15m) < 10:
        print(f"    Not enough 15m candle data, using 1h only")
        candles_15m = None

    closes = [float(c['c']) for c in candles_1h]
    volumes = [float(c['v']) for c in candles_1h]
    current = closes[-1]

    # Get market info if not provided
    if not market_info:
        all_markets = get_all_markets()
        market_info = all_markets.get(symbol, {})

    funding = market_info.get('funding', 0)
    day_change = market_info.get('day_change', 0)
    max_lev = market_info.get('max_lev', 3)
    oi = market_info.get('oi', 0)

    # Calculate all indicators on 1h
    rsi = calc_rsi(closes, 14)
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    bb_mid, bb_upper, bb_lower = calc_bollinger(closes, 20, 2)
    macd_line, signal_line, macd_hist = calc_macd(closes)
    atr = calc_atr(candles_1h, 14)
    avg_vol = sum(volumes) / len(volumes) if volumes else 1
    recent_vol = sum(volumes[-3:]) / 3 if len(volumes) >= 3 else 0
    vol_spike = recent_vol / avg_vol if avg_vol > 0 else 1.0

    # ATR as % of price (volatility)
    atr_pct = (atr / current * 100) if current > 0 and atr > 0 else 0

    # ── Multi-timeframe confirmation on 15m ──────────────────────────
    mtf_confirmed = True
    mtf_direction = None
    if candles_15m:
        closes_15m = [float(c['c']) for c in candles_15m]
        rsi_15m = calc_rsi(closes_15m, 14)
        ema9_15m = calc_ema(closes_15m, 9)
        ema21_15m = calc_ema(closes_15m, 21)
        if ema9_15m > ema21_15m:
            mtf_direction = "long"
        elif ema9_15m < ema21_15m:
            mtf_direction = "short"
        print(f"    15m: RSI={rsi_15m:.1f} EMA9={'>' if ema9_15m > ema21_15m else '<'}EMA21 → {mtf_direction or 'neutral'}")

    print(f"    Price: ${current:,.4f} | 24h: {day_change:+.2f}% | OI: {oi:,.0f} | Funding: {funding:+.6f}")
    print(f"    RSI: {rsi:.1f} | EMA9: ${ema9:.4f} vs EMA21: ${ema21:.4f} | ATR: {atr_pct:.2f}%")
    print(f"    BB: [{bb_lower:.4f}, {bb_mid:.4f}, {bb_upper:.4f}] | MACD hist: {macd_hist:+.6f}")
    print(f"    Vol: avg={avg_vol:.0f} recent={recent_vol:.0f} spike={vol_spike:.2f}x")

    # --- Build composite signal ---
    bull_score = 0
    bear_score = 0
    signal_type_parts = []
    reasons_bull = []
    reasons_bear = []

    # 1. RSI
    if rsi < 30:
        bull_score += 3; signal_type_parts.append("rsi_oversold"); reasons_bull.append(f"RSI {rsi:.0f} oversold")
    elif rsi < 40:
        bull_score += 1; reasons_bull.append(f"RSI {rsi:.0f} approaching oversold")
    elif rsi > 70:
        bear_score += 3; signal_type_parts.append("rsi_overbought"); reasons_bear.append(f"RSI {rsi:.0f} overbought")
    elif rsi > 60:
        bear_score += 1; reasons_bear.append(f"RSI {rsi:.0f} approaching overbought")

    # 2. EMA Crossover
    if ema9 > ema21:
        bull_score += 2; signal_type_parts.append("ema_bull"); reasons_bull.append("EMA9>EMA21 bullish cross")
    else:
        bear_score += 2; signal_type_parts.append("ema_bear"); reasons_bear.append("EMA9<EMA21 bearish cross")

    # 3. Bollinger Bands
    if current <= bb_lower * 1.002:
        bull_score += 2; signal_type_parts.append("bb_lower"); reasons_bull.append("Price at lower BB (oversold)")
    elif current >= bb_upper * 0.998:
        bear_score += 2; signal_type_parts.append("bb_upper"); reasons_bear.append("Price at upper BB (overbought)")

    # 4. MACD
    if macd_hist > 0:
        bull_score += 1; reasons_bull.append("MACD histogram positive")
    else:
        bear_score += 1; reasons_bear.append("MACD histogram negative")

    # 5. Volume spike (confirms direction)
    if vol_spike > 1.5:
        if current > closes[-2]:
            bull_score += 1; signal_type_parts.append("vol_bull"); reasons_bull.append(f"Volume spike {vol_spike:.1f}x + price up")
        else:
            bear_score += 1; signal_type_parts.append("vol_bear"); reasons_bear.append(f"Volume spike {vol_spike:.1f}x + price down")

    # 6. Funding rate
    if funding < -0.0001:
        bull_score += 1; reasons_bull.append("Negative funding (shorts paying longs)")
    elif funding > 0.0001:
        bear_score += 1; reasons_bear.append("Positive funding (longs paying shorts)")

    # 7. 24h momentum
    if day_change > 5:
        bull_score += 1; reasons_bull.append(f"24h +{day_change:.1f}% strong momentum")
    elif day_change < -5:
        bear_score += 1; reasons_bear.append(f"24h {day_change:.1f}% strong decline")

    signal_type = "_".join(signal_type_parts) if signal_type_parts else "neutral"

    # Check learning: should we trade this asset?
    asset_rec, asset_mult = get_asset_recommendation(learning_db, symbol)
    if asset_rec == "skip":
        print(f"    🚫 Learning says SKIP {symbol} (poor historical performance)")
        return None

    # Calculate net signal
    net = bull_score - bear_score
    confidence = min(95, abs(net) * 12 + 20)  # Base 20 + 12 per net point

    # Get adaptive params
    adaptive = get_adaptive_params(learning_db)
    min_confidence = adaptive["confidence_threshold"]

    if net > 0 and confidence >= min_confidence:
        direction = "long"
        # Multi-timeframe check: if 15m disagrees, reduce confidence
        if mtf_direction and mtf_direction != direction:
            confidence = int(confidence * 0.7)
            print(f"    ⚠️ 15m timeframe disagrees — confidence reduced to {confidence}%")
            if confidence < min_confidence:
                print(f"    😐 Below threshold after MTF check")
                return None
        print(f"    📈 LONG signal — confidence: {confidence:.0f}% (bull: {bull_score}, bear: {bear_score})")
        print(f"    Reasons: {', '.join(reasons_bull)}")
    elif net < 0 and confidence >= min_confidence:
        direction = "short"
        # Multi-timeframe check
        if mtf_direction and mtf_direction != direction:
            confidence = int(confidence * 0.7)
            print(f"    ⚠️ 15m timeframe disagrees — confidence reduced to {confidence}%")
            if confidence < min_confidence:
                print(f"    😐 Below threshold after MTF check")
                return None
        print(f"    📉 SHORT signal — confidence: {confidence:.0f}% (bull: {bull_score}, bear: {bear_score})")
        print(f"    Reasons: {', '.join(reasons_bear)}")
    else:
        print(f"    😐 No signal (net: {net:+d}, confidence: {confidence:.0f}% < threshold {min_confidence}%)")
        return None

    # Check if this signal type historically performs well
    sig_history = learning_db.get("per_signal_type", {}).get(signal_type, {})
    sig_trades = sig_history.get("trades", 0)
    sig_wr = (sig_history.get("wins", 0) / sig_trades * 100) if sig_trades > 0 else 50
    if sig_trades >= 3 and sig_wr < 25:
        confidence = int(confidence * 0.6)
        print(f"    ⚠️ Signal type '{signal_type}' has {sig_wr:.0f}% WR — confidence reduced to {confidence:.0f}%")
        if confidence < min_confidence:
            return None

    # Calculate adaptive SL/TP and leverage
    sl_pct = STOP_LOSS_PCT + adaptive["sl_tightening"]
    tp_pct = TAKE_PROFIT_PCT + adaptive["tp_extension"]
    
    # ATR-based SL adjustment: use max(base SL, 1.5x ATR)
    if atr_pct > 0:
        atr_sl = atr_pct * 1.5 / 100
        sl_pct = max(sl_pct, -atr_sl)  # Don't go tighter than 1.5x ATR
    
    leverage = DEFAULT_LEVERAGE + adaptive["leverage_adjustment"]
    leverage = max(1, min(min(5, max_lev), leverage))  # Clamp to 1x-5x and market max

    effective_mult = asset_mult * adaptive["position_size_mult"] * get_kelly_size(learning_db, symbol)

    return {
        'symbol': symbol,
        'direction': direction,
        'confidence': confidence,
        'entry': current,
        'funding': funding,
        'day_change': day_change,
        'signal_type': signal_type,
        'rsi': rsi,
        'ema9': ema9,
        'ema21': ema21,
        'bb_lower': bb_lower,
        'bb_upper': bb_upper,
        'macd_hist': macd_hist,
        'atr_pct': atr_pct,
        'vol_spike': vol_spike,
        'stop_loss': current * (1 + sl_pct) if direction == "long" else current * (1 - sl_pct),
        'take_profit': current * (1 + tp_pct) if direction == "long" else current * (1 - tp_pct),
        'leverage': leverage,
        'size_multiplier': effective_mult,
        'sl_pct': sl_pct,
        'tp_pct': tp_pct,
        'max_lev': max_lev,
        'reasons': reasons_bull if direction == "long" else reasons_bear,
    }


# ============ LEARNING ENGINE ============

def load_learning_db():
    """Load the learning database, initialize if missing"""
    defaults = {
        "created_at": datetime.utcnow().isoformat(),
        "total_trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0,
        "current_win_streak": 0, "current_loss_streak": 0,
        "best_win_streak": 0, "worst_loss_streak": 0,
        "per_asset": {}, "per_signal_type": {}, "per_leverage": {},
        "adaptive_params": {
            "position_size_mult": 1.0,
            "leverage_adjustment": 0,
            "sl_tightening": 0.0,
            "tp_extension": 0.0,
            "confidence_threshold": 50,
            "min_hold_cycles": 0,
        },
        "recent_trades": [], "lessons": [],
    }
    if os.path.exists(LEARNING_DB):
        try:
            with open(LEARNING_DB) as f:
                data = json.load(f)
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
            return data
        except:
            pass
    return defaults

def save_learning_db(db):
    with open(LEARNING_DB, 'w') as f:
        json.dump(db, f, indent=2)

def get_adaptive_params(db):
    params = db.get("adaptive_params", {}).copy()
    total = db.get("total_trades", 0)
    wins = db.get("wins", 0)
    losses = db.get("losses", 0)
    win_rate = (wins / total * 100) if total > 0 else 0
    loss_streak = db.get("current_loss_streak", 0)
    win_streak = db.get("current_win_streak", 0)

    # ── Kelly Criterion position sizing ──────────────────────────────
    # Kelly fraction = W - (1-W)/R where W=win_rate, R=avg_win/avg_loss
    if total >= 5:
        win_p = wins / total
        avg_win = db.get("total_pnl", 0) / max(1, wins) if wins > 0 else 0
        avg_loss = abs(db.get("total_pnl", 0)) / max(1, losses) if losses > 0 else 1
        # Use realized PnL to estimate payoff ratio
        recent = db.get("recent_trades", [])
        win_pnls = [t.get("pnl", 0) for t in recent if t.get("win")]
        loss_pnls = [abs(t.get("pnl", 0)) for t in recent if not t.get("win")]
        if win_pnls and loss_pnls:
            avg_win = sum(win_pnls) / len(win_pnls)
            avg_loss = sum(loss_pnls) / len(loss_pnls)
        R = avg_win / max(0.01, avg_loss) if avg_loss > 0 else 1.0
        kelly = win_p - (1 - win_p) / R
        # Use half-Kelly for safety, clamp 0.1x–1.5x
        kelly_size = max(0.1, min(1.5, kelly * 0.5))
        params["position_size_mult"] = kelly_size
    else:
        params["position_size_mult"] = 1.0

    # Loss streak override — always protect
    if loss_streak >= 3:
        params["position_size_mult"] = min(params["position_size_mult"], 0.3)

    # ── Winning cycle detection ──────────────────────────────────────
    # On consecutive wins, gradually increase size and extend TP
    if win_streak >= 3:
        win_bonus = min(0.3, (win_streak - 2) * 0.1)
        params["position_size_mult"] = min(1.5, params["position_size_mult"] + win_bonus)
    elif total >= 5 and win_rate > 60:
        params["position_size_mult"] = min(1.5, params["position_size_mult"] + 0.1)

    # Leverage — increase on win streaks, decrease on losses
    base_lev = DEFAULT_LEVERAGE
    if win_streak >= 2:
        base_lev += 1
    if loss_streak >= 2:
        base_lev -= 1
    params["leverage_adjustment"] = base_lev - DEFAULT_LEVERAGE

    # SL/TP — tighten on losses, extend on wins
    if loss_streak >= 2:
        params["sl_tightening"] = -0.01 * min(loss_streak, 3)
    elif win_streak >= 3:
        params["sl_tightening"] = 0.01
    else:
        params["sl_tightening"] = 0.0

    if win_streak >= 3:
        params["tp_extension"] = 0.02 * min(win_streak - 2, 3)
    else:
        params["tp_extension"] = 0.0

    # Confidence threshold — raise after losses, lower after wins
    base_threshold = 50
    if loss_streak >= 2:
        params["confidence_threshold"] = min(75, base_threshold + loss_streak * 5)
    elif win_streak >= 3 and win_rate > 50:
        params["confidence_threshold"] = max(35, base_threshold - 5)
    else:
        params["confidence_threshold"] = base_threshold

    return params


def get_kelly_size(db, symbol):
    """Kelly criterion size multiplier for a specific asset based on its history."""
    asset = db.get("per_asset", {}).get(symbol, {})
    trades = asset.get("trades", 0)
    if trades < 3:
        return 1.0  # Not enough data, use default
    wins = asset.get("wins", 0)
    losses = asset.get("losses", 0)
    if losses == 0:
        return 1.3  # All wins, increase
    win_p = wins / trades
    pnl = asset.get("pnl", 0)
    # Estimate payoff from PnL
    avg_pnl = pnl / trades
    if avg_pnl > 0:
        R = abs(avg_pnl) / max(0.01, abs(pnl) / max(1, losses))
        kelly = win_p - (1 - win_p) / max(0.1, R)
        return max(0.2, min(1.5, kelly * 0.5 + 0.5))
    return 0.5  # Losing asset, reduce

def record_trade(db, symbol, side, size, entry, leverage, sl, tp,
                 signal_type, confidence, funding, day_change, pnl, hold_cycles,
                 rsi=0, atr_pct=0, vol_spike=1, reasons=None):
    """Record a completed trade and update all learning metrics"""
    is_win = pnl > 0

    db["total_trades"] += 1
    if is_win:
        db["wins"] += 1
        db["current_win_streak"] += 1
        db["current_loss_streak"] = 0
        db["best_win_streak"] = max(db.get("best_win_streak", 0), db["current_win_streak"])
    else:
        db["losses"] += 1
        db["current_loss_streak"] += 1
        db["current_win_streak"] = 0
        db["worst_loss_streak"] = max(db.get("worst_loss_streak", 0), db["current_loss_streak"])

    db["total_pnl"] = db.get("total_pnl", 0) + pnl

    # Per-asset
    if symbol not in db["per_asset"]:
        db["per_asset"][symbol] = {
            "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
            "best_pnl": 0, "worst_pnl": 0, "total_hold_cycles": 0, "avg_hold_cycles": 0,
        }
    a = db["per_asset"][symbol]
    a["trades"] += 1
    if is_win: a["wins"] += 1
    else: a["losses"] += 1
    a["pnl"] += pnl
    a["best_pnl"] = max(a["best_pnl"], pnl)
    a["worst_pnl"] = min(a["worst_pnl"], pnl)
    a["total_hold_cycles"] += hold_cycles
    a["avg_hold_cycles"] = a["total_hold_cycles"] / a["trades"]

    # Per-signal-type
    sig_key = signal_type or "unknown"
    if sig_key not in db["per_signal_type"]:
        db["per_signal_type"][sig_key] = {
            "trades": 0, "wins": 0, "pnl": 0.0,
            "total_confidence": 0, "avg_confidence": 0,
        }
    s = db["per_signal_type"][sig_key]
    s["trades"] += 1
    if is_win: s["wins"] += 1
    s["pnl"] += pnl
    s["total_confidence"] += confidence
    s["avg_confidence"] = s["total_confidence"] / s["trades"]

    # Per-leverage
    lev_key = f"{leverage}x"
    if lev_key not in db["per_leverage"]:
        db["per_leverage"][lev_key] = {"trades": 0, "wins": 0, "pnl": 0.0}
    l = db["per_leverage"][lev_key]
    l["trades"] += 1
    if is_win: l["wins"] += 1
    l["pnl"] += pnl

    # Recent trades
    db["recent_trades"].append({
        "symbol": symbol, "side": side, "size": size, "entry": entry,
        "leverage": leverage, "pnl": pnl, "win": is_win,
        "signal_type": signal_type, "confidence": confidence,
        "funding": funding, "day_change": day_change,
        "hold_cycles": hold_cycles, "rsi": rsi, "atr_pct": atr_pct,
        "vol_spike": vol_spike, "reasons": reasons or [],
        "timestamp": datetime.utcnow().isoformat(),
    })
    if len(db["recent_trades"]) > 30:
        db["recent_trades"] = db["recent_trades"][-30:]

    db["adaptive_params"] = get_adaptive_params(db)
    generate_lessons(db)
    save_learning_db(db)

def generate_lessons(db):
    lessons = []
    total = db.get("total_trades", 0)
    wins = db.get("wins", 0)
    win_rate = (wins / total * 100) if total > 0 else 0
    pnl = db.get("total_pnl", 0)

    if total < 3:
        lessons.append(f"Still learning — {total} trades, need more data")
        db["lessons"] = lessons
        return

    if pnl > 0:
        lessons.append(f"Profitable: ${pnl:.2f} over {total} trades ({win_rate:.0f}% WR)")
    else:
        lessons.append(f"Losing: ${pnl:.2f} over {total} trades ({win_rate:.0f}% WR)")

    best_asset = worst_asset = None
    for sym, info in db.get("per_asset", {}).items():
        if info["trades"] >= 2:
            if best_asset is None or info["pnl"] > best_asset[1]:
                best_asset = (sym, info["pnl"])
            if worst_asset is None or info["pnl"] < worst_asset[1]:
                worst_asset = (sym, info["pnl"])

    if best_asset:
        lessons.append(f"Best asset: {best_asset[0]} (${best_asset[1]:+.2f})")
    if worst_asset and worst_asset[0] != best_asset[0]:
        lessons.append(f"Worst asset: {worst_asset[0]} (${worst_asset[1]:+.2f})")

    best_signal = None
    for sig, info in db.get("per_signal_type", {}).items():
        if info["trades"] >= 2:
            wr = info["wins"] / info["trades"] * 100
            if best_signal is None or info["pnl"] > best_signal[1]:
                best_signal = (sig, info["pnl"], wr)
    if best_signal:
        lessons.append(f"Best signal: {best_signal[0]} (${best_signal[1]:+.2f}, {best_signal[2]:.0f}% WR)")

    loss_streak = db.get("current_loss_streak", 0)
    win_streak = db.get("current_win_streak", 0)
    if loss_streak >= 3:
        lessons.append(f"⚠️ {loss_streak} losses in a row — position size cut to 30%")
    if win_streak >= 3:
        lessons.append(f"🔥 {win_streak} wins in a row — TP targets extended")

    for lev, info in db.get("per_leverage", {}).items():
        if info["trades"] >= 3:
            wr = info["wins"] / info["trades"] * 100
            if wr < 30:
                lessons.append(f"{lev} performing poorly ({wr:.0f}% WR) — reduce leverage")
            elif wr > 60:
                lessons.append(f"{lev} performing well ({wr:.0f}% WR)")

    db["lessons"] = lessons

def get_asset_recommendation(db, symbol):
    asset = db.get("per_asset", {}).get(symbol, {})
    trades = asset.get("trades", 0)
    if trades < 2:
        return "neutral", 1.0
    wr = asset.get("wins", 0) / trades * 100
    pnl = asset.get("pnl", 0)
    if wr > 60 and pnl > 0:
        return "increase", 1.3
    elif wr < 20:
        return "skip", 0.0
    elif wr < 30 and pnl < 0:
        return "reduce", 0.5
    return "neutral", 1.0


# ============ HL FUNCTIONS ============

def run(cmd, timeout=300):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1

def use_saint():
    run(f"acp agent use --agent-id {SAINT_ID} --json")
    return True

def get_balance(symbol, chain=BASE_CHAIN):
    out, _, _ = run(f"acp wallet balance --chain-id {chain} --json")
    try:
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        d = json.loads(raw)
        for t in d.get('tokens', []):
            sym = t.get('tokenMetadata', {}).get('symbol') or 'NATIVE'
            if sym.upper() == symbol.upper():
                bal_raw = t.get('tokenBalance', '0')
                dec = t.get('tokenMetadata', {}).get('decimals', 18)
                try:
                    val = int(bal_raw, 16) if isinstance(bal_raw, str) and bal_raw.startswith('0x') else int(bal_raw)
                    return val / (10**dec) if val > 0 else 0.0
                except:
                    return 0.0
        return 0.0
    except:
        return 0.0

def get_hl_status():
    out, _, _ = run("acp trade hl-status --json")
    try:
        raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
        d = json.loads(raw)
        return {
            'account_value': float(d.get('accountValue', 0)),
            'withdrawable': float(d.get('withdrawable', 0)),
            'positions': d.get('positions', []),
            'spot_balances': d.get('spotBalances', [])
        }
    except:
        return {'account_value': 0, 'withdrawable': 0, 'positions': [], 'spot_balances': []}

def deposit_to_hl(amount):
    print(f"  Depositing ${amount:.2f} USDC to Hyperliquid...")
    out, err, rc = run(
        f"acp trade --token-in usdc --chain-in {BASE_CHAIN} --amount-in {amount} "
        f"--token-out usdc --chain-out {HL_CHAIN} --json"
    )
    if rc == 0:
        print(f"  ✅ HL deposit successful")
        log_hl_action('deposit', amount)
        return True
    else:
        print(f"  ❌ HL deposit failed: {err[:150]}")
        return False

def withdraw_from_hl(amount):
    print(f"  Withdrawing ${amount:.2f} from Hyperliquid...")
    out, err, rc = run(f"acp trade withdraw-from-hl --amount {amount:.2f} --json")
    if rc == 0:
        print(f"  ✅ HL withdrawal successful")
        log_hl_action('withdrawal', amount)
        return True
    else:
        print(f"  ❌ HL withdrawal failed: {err[:150]}")
        return False

def open_position(signal, learning_db):
    symbol = signal['symbol']
    side = signal['direction']
    leverage = signal['leverage']

    hl = get_hl_status()
    account_value = hl['account_value']

    if account_value < MIN_HL_BALANCE:
        print(f"  ⚠️ HL balance too low (${account_value:.2f})")
        return False

    base_usd = account_value * MAX_POSITION_PCT
    conf_factor = min(1.0, signal['confidence'] / 80)
    adjusted_usd = base_usd * signal['size_multiplier'] * conf_factor
    
    # Enforce HL $10 minimum notional (use buffer to avoid rounding rejection)
    MIN_NOTIONAL = 10.50
    if adjusted_usd < MIN_NOTIONAL:
        if account_value * 0.50 >= MIN_NOTIONAL:
            adjusted_usd = MIN_NOTIONAL
            print(f"    ⚠️ Adjusted up to ${MIN_NOTIONAL} min notional")
        else:
            print(f"  ⚠️ Notional ${adjusted_usd:.2f} below HL $10 minimum and can't afford it")
            return False
    
    price = signal['entry']
    size = adjusted_usd / price

    # Round based on HL market sz_decimals
    sz_dec = signal.get('max_lev', 3)  # fallback
    # Use market info for rounding
    all_markets = get_all_markets()
    m_info = all_markets.get(symbol, {})
    decimals = m_info.get('sz_decimals', 4)

    if decimals == 0:
        size = round(size)
    elif decimals == 1:
        size = round(size, 1)
    elif decimals == 2:
        size = round(size, 2)
    elif decimals == 3:
        size = round(size, 3)
    else:
        size = round(size, 4)

    if size <= 0:
        print(f"  ⚠️ Size too small: {size}")
        return False

    print(f"  Opening {side.upper()} {symbol}: size={size} @ ${price:,.4f} ({leverage}x)")
    print(f"    SL: ${signal['stop_loss']:,.4f} ({signal['sl_pct']*100:+.1f}%) | TP: ${signal['take_profit']:,.4f} ({signal['tp_pct']*100:+.1f}%)")
    print(f"    Size: ${adjusted_usd:.2f} (base ${base_usd:.2f} × mult {signal['size_multiplier']:.2f} × conf {conf_factor:.2f})")

    # Use isolated margin if market requires it
    isolated_flag = "--isolated" if m_info.get('only_isolated', False) else ""

    out, err, rc = run(
        f"acp trade --side {side} --token {symbol} --size {size} "
        f"--leverage {leverage} {isolated_flag} --json"
    )

    if rc == 0:
        try:
            raw = out.split('[acp-wrapper]')[0].strip() if '[acp-wrapper]' in out else out
            d = json.loads(raw)
            if d.get('status') == 'success' or d.get('filledSize'):
                print(f"  ✅ Position opened: {side} {size} {symbol}")
                log_position(symbol, side, size, price, leverage,
                           signal['stop_loss'], signal['take_profit'],
                           signal.get('signal_type'), signal['confidence'],
                           signal.get('rsi', 0), signal.get('atr_pct', 0),
                           signal.get('vol_spike', 1), signal.get('reasons', []))
                return True
        except:
            pass
        print(f"  ✅ Position likely opened (check HL)")
        log_position(symbol, side, size, price, leverage,
                   signal['stop_loss'], signal['take_profit'],
                   signal.get('signal_type'), signal['confidence'],
                   signal.get('rsi', 0), signal.get('atr_pct', 0),
                   signal.get('vol_spike', 1), signal.get('reasons', []))
        return True
    else:
        print(f"  ❌ Position failed: {err[:150]}")
        return False

def close_position(position):
    coin = position.get('coin', position.get('token', ''))
    size = float(position.get('szi', position.get('size', 0)))
    close_side = "short" if size > 0 else "long"

    print(f"  Closing {coin} position (size: {size})...")
    out, err, rc = run(
        f"acp trade --side {close_side} --token {coin} --size {abs(size)} "
        f"--reduce-only --json"
    )
    if rc == 0:
        print(f"  ✅ Position closed")
        log_position_close(coin, size)
        return True
    else:
        print(f"  ❌ Close failed: {err[:150]}")
        return False

def check_positions(learning_db):
    """Check open positions, close on SL/TP/trailing stop, record outcomes for learning"""
    hl = get_hl_status()
    positions = hl['positions']

    if not positions:
        print("  No open positions")
        return 0

    print(f"  Open positions: {len(positions)}")
    pos_log = load_position_log()
    adaptive = get_adaptive_params(learning_db)

    for pos in positions:
        coin = pos.get('coin', pos.get('token', '?'))
        size = float(pos.get('szi', pos.get('size', 0)))
        entry = float(pos.get('entryPx', 0))
        pnl = float(pos.get('unrealizedPnl', 0))
        lev_raw = pos.get('leverage', 1)
        leverage = lev_raw.get('value', 1) if isinstance(lev_raw, dict) else lev_raw
        side = "long" if size > 0 else "short"

        pnl_pct = (pnl / (abs(size) * entry)) * 100 if entry > 0 else 0

        print(f"    {coin}: {side} {size} @ ${entry:,.4f} | PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%) | {leverage}x")

        sl_threshold = (STOP_LOSS_PCT + adaptive["sl_tightening"]) * 100 * leverage
        tp_threshold = (TAKE_PROFIT_PCT + adaptive["tp_extension"]) * 100 * leverage

        # ── Trailing stop: if position is profitable, trail the stop ──
        # Get peak PnL from position log
        pos_info = find_position_log_entry(pos_log, coin)
        peak_pnl_pct = pos_info.get('peak_pnl_pct', 0) if pos_info else 0
        current_pnl_pct = pnl_pct
        if current_pnl_pct > peak_pnl_pct:
            peak_pnl_pct = current_pnl_pct
            # Update peak in log
            update_peak_pnl(coin, peak_pnl_pct)

        trailing_sl_trigger = TRAILING_STOP_ACTIVATE * 100 * leverage
        trailing_sl_distance = TRAILING_STOP_DISTANCE * 100 * leverage

        should_close = False
        close_reason = ""

        if pnl_pct <= sl_threshold:
            should_close = True
            close_reason = f"STOP LOSS ({pnl_pct:.2f}% ≤ {sl_threshold:.2f}%)"
        elif pnl_pct >= tp_threshold:
            should_close = True
            close_reason = f"TAKE PROFIT ({pnl_pct:.2f}% ≥ {tp_threshold:.2f}%)"
        elif peak_pnl_pct >= trailing_sl_trigger and pnl_pct <= peak_pnl_pct - trailing_sl_distance:
            should_close = True
            close_reason = f"TRAILING STOP (peak {peak_pnl_pct:.2f}%, now {pnl_pct:.2f}%, dropped {trailing_sl_distance:.1f}%)"

        if should_close:
            print(f"    🛑 {close_reason} — closing")
            if close_position(pos):
                hold_cycles = count_hold_cycles(pos_info)
                record_trade(learning_db, coin, side, abs(size), entry, leverage,
                           0, 0, pos_info.get('signal_type', 'unknown'),
                           pos_info.get('confidence', 50), 0, 0, pnl, hold_cycles,
                           pos_info.get('rsi', 0), pos_info.get('atr_pct', 0),
                           pos_info.get('vol_spike', 1), pos_info.get('reasons', []))
                print(f"    📝 Learned: PnL ${pnl:+.2f} on {coin} {side} ({close_reason.split('(')[0].strip()})")

    return len(positions)


# ============ LOGGING ============

def load_position_log():
    if os.path.exists(POSITION_LOG):
        try:
            with open(POSITION_LOG) as f:
                return json.load(f)
        except:
            pass
    return []

def find_position_log_entry(log, coin):
    for p in reversed(log):
        if p.get('symbol') == coin and p.get('status') == 'open':
            return p
    return {}

def update_peak_pnl(coin, peak_pnl_pct):
    """Update the peak PnL% for an open position in the log (for trailing stop)."""
    log = load_position_log()
    for p in reversed(log):
        if p.get('symbol') == coin and p.get('status') == 'open':
            p['peak_pnl_pct'] = peak_pnl_pct
            break
    try:
        with open(POSITION_LOG, 'w') as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass

def count_hold_cycles(pos_info):
    if not pos_info.get('opened'):
        return 1
    try:
        opened = datetime.fromisoformat(pos_info['opened'].replace('Z', ''))
        delta = datetime.utcnow() - opened
        return max(1, int(delta.total_seconds() / 1800))
    except:
        return 1

def log_hl_action(action, amount):
    log = []
    if os.path.exists(HL_DEPOSIT_LOG):
        with open(HL_DEPOSIT_LOG) as f:
            log = json.load(f)
    log.append({'action': action, 'amount': amount, 'timestamp': datetime.utcnow().isoformat()})
    with open(HL_DEPOSIT_LOG, 'w') as f:
        json.dump(log, f, indent=2)

def log_position(symbol, side, size, entry, leverage, sl, tp,
                 signal_type=None, confidence=0, rsi=0, atr_pct=0, vol_spike=1, reasons=None):
    log = load_position_log()
    log.append({
        'symbol': symbol, 'side': side, 'size': size, 'entry': entry,
        'leverage': leverage, 'sl': sl, 'tp': tp,
        'signal_type': signal_type, 'confidence': confidence,
        'rsi': rsi, 'atr_pct': atr_pct, 'vol_spike': vol_spike,
        'reasons': reasons or [],
        'opened': datetime.utcnow().isoformat(), 'status': 'open'
    })
    with open(POSITION_LOG, 'w') as f:
        json.dump(log, f, indent=2)

def log_position_close(symbol, size):
    log = load_position_log()
    for p in reversed(log):
        if p['symbol'] == symbol and p['status'] == 'open':
            p['status'] = 'closed'
            p['closed'] = datetime.utcnow().isoformat()
            break
    with open(POSITION_LOG, 'w') as f:
        json.dump(log, f, indent=2)


# ============ DISPLAY ============

def print_learning_summary(db):
    total = db.get("total_trades", 0)
    wins = db.get("wins", 0)
    losses = db.get("losses", 0)
    pnl = db.get("total_pnl", 0)
    win_rate = (wins / total * 100) if total > 0 else 0

    print(f"\n  📊 LEARNING SUMMARY")
    print(f"    Trades: {total} | W: {wins} | L: {losses} | WR: {win_rate:.0f}% | PnL: ${pnl:+.2f}")
    print(f"    Win streak: {db.get('current_win_streak', 0)} | Loss streak: {db.get('current_loss_streak', 0)}")

    adaptive = get_adaptive_params(db)
    print(f"\n  ⚙️ ADAPTIVE PARAMS")
    print(f"    Size mult: {adaptive['position_size_mult']:.2f}x | Lev adj: {adaptive['leverage_adjustment']:+d}")
    print(f"    SL tighten: {adaptive['sl_tightening']:+.3f} | TP extend: {adaptive['tp_extension']:+.3f}")
    print(f"    Conf threshold: {adaptive['confidence_threshold']}%")

    if db.get("per_asset"):
        print(f"\n    Per-asset:")
        for sym, info in sorted(db["per_asset"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = (info["wins"] / info["trades"] * 100) if info["trades"] > 0 else 0
            print(f"      {sym}: {info['trades']}t, WR {wr:.0f}%, ${info['pnl']:+.2f}")

    if db.get("per_signal_type"):
        print(f"\n    Per-signal-type:")
        for sig, info in sorted(db["per_signal_type"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = (info["wins"] / info["trades"] * 100) if info["trades"] > 0 else 0
            print(f"      {sig}: {info['trades']}t, WR {wr:.0f}%, ${info['pnl']:+.2f}")

    if db.get("lessons"):
        print(f"\n  💡 LESSONS:")
        for l in db["lessons"]:
            print(f"    • {l}")


# ============ MAIN ============

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
    print(f"\n⚔️ Saint Perps v2 (TA + Self-Learning) — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    learning_db = load_learning_db()
    print_learning_summary(learning_db)

    use_saint()

    # 1. Check Base USDC — deposit to HL if available
    usdc = get_balance('USDC')
    print(f"\n  Saint USDC on Base: ${usdc:.2f}")

    if usdc >= MIN_DEPOSIT:
        deposit_amount = min(usdc - 2, usdc * 0.7)
        if deposit_amount >= MIN_DEPOSIT:
            deposit_to_hl(round(deposit_amount, 2))
            time.sleep(5)

    # 2. Check HL status
    hl = get_hl_status()
    print(f"\n  HL Account: ${hl['account_value']:.2f} (withdrawable: ${hl['withdrawable']:.2f})")

    # 3. Manage existing positions (with learning)
    open_count = check_positions(learning_db)

    # 4. Scan markets for new opportunities — dynamic + hardcoded
    adaptive = get_adaptive_params(learning_db)
    
    if open_count < MAX_POSITIONS and hl['account_value'] >= MIN_HL_BALANCE:
        print(f"\n  🔍 Scanning markets (min confidence: {adaptive['confidence_threshold']}%)...")
        
        # Get all market data once
        all_markets = get_all_markets()
        
        # Build scan list: core markets first, then dynamic top movers
        scan_list = list(CORE_MARKETS)
        
        # Add extended markets that meet OI threshold
        extended_available = []
        for sym in EXTENDED_MARKETS:
            if sym in all_markets:
                m = all_markets[sym]
                if m['oi'] >= MIN_OI_EXTENDED and m['price'] > 0:
                    extended_available.append((sym, m['oi']))
        
        # Sort by OI and add to scan list
        extended_available.sort(key=lambda x: x[1], reverse=True)
        scan_list.extend([s for s, _ in extended_available])
        
        # ── Dynamic: scan top 8 markets by |day_change| (big movers = opportunity) ──
        movers = []
        for sym, m in all_markets.items():
            if sym not in scan_list and m['oi'] >= MIN_OI_EXTENDED and m['price'] > 0:
                if abs(m['day_change']) > 3:  # Lowered from 5 to catch more
                    movers.append((sym, abs(m['day_change'])))
        
        movers.sort(key=lambda x: x[1], reverse=True)
        dynamic_movers = [s for s, _ in movers[:8]]
        scan_list.extend(dynamic_movers)
        
        # ── Dynamic: scan top 5 by volume spike (high OI + high volume = breakout) ──
        # We'll check volume via candle data during analysis
        
        if dynamic_movers:
            print(f"  📊 Dynamic movers detected: {', '.join(dynamic_movers[:5])}")
        
        print(f"  Scanning {len(scan_list)} markets: {', '.join(scan_list[:10])}{'...' if len(scan_list) > 10 else ''}")
        best_signal = None
        best_confidence = 0
        
        for symbol in scan_list:
            if open_count >= MAX_POSITIONS:
                break
            
            market_info = all_markets.get(symbol, {})
            signal = analyze_market(symbol, learning_db, market_info)
            
            if signal:
                # Track the highest confidence signal if we can only open one
                if open_count + 1 < MAX_POSITIONS:
                    # We have room for this one
                    opened = open_position(signal, learning_db)
                    if opened:
                        open_count += 1
                        time.sleep(3)
                elif signal['confidence'] > best_confidence:
                    best_signal = signal
                    best_confidence = signal['confidence']
        
        # If we had room for only one more and found a best signal, open it
        if best_signal and open_count < MAX_POSITIONS:
            open_position(best_signal, learning_db)
    else:
        if open_count >= MAX_POSITIONS:
            print(f"\n  Max positions reached ({open_count}/{MAX_POSITIONS})")
        if hl['account_value'] < MIN_HL_BALANCE:
            print(f"\n  HL balance too low (${hl['account_value']:.2f})")

    # 5. Withdraw profits if no positions and withdrawable > $10
    hl = get_hl_status()
    if not hl['positions'] and hl['withdrawable'] > 10:
        print(f"\n  💰 Withdrawing ${hl['withdrawable']:.2f} profits from HL...")
        withdraw_from_hl(round(hl['withdrawable'] - 1, 2))

    # 6. Save learning DB
    save_learning_db(learning_db)

    print(f"\n  ✅ Saint trading cycle complete")
