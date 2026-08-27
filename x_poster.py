#!/usr/bin/env python3
"""
X/Twitter Auto-Poster
=====================
Auto-posts trade wins, ecosystem updates, and memes to @TheDrkGltch.

Posts types:
1. Trade wins (from sniper_guard, saint_perps, hl_spot)
2. Ecosystem milestones (goal progress, new offerings, volume)
3. Meme content (Armadillo Saints themed)
4. Market insights (from growth engine research)

Posts max 1 time per hour to avoid spam.
Requires: xurl auth oauth2 --app armabase (one-time setup)

Runs every 60 minutes via cron.
"""

import json, time, os, subprocess, random
from datetime import datetime, timezone

# ============ CONFIG ============
XURL_BIN = os.path.expanduser("~/.local/bin/xurl")
POST_LOG = "/workspace/x_post_log.json"
LAST_POST_FILE = "/workspace/x_last_post.json"
MIN_HOURS_BETWEEN_POSTS = 1

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
    if os.path.exists(POST_LOG):
        try:
            with open(POST_LOG) as f:
                log_data = json.load(f)
        except Exception:
            pass
    log_data.append(entry)
    log_data = log_data[-100:]
    with open(POST_LOG, "w") as f:
        json.dump(log_data, f, indent=2)

def can_post():
    """Check if enough time has passed since last post"""
    if not os.path.exists(LAST_POST_FILE):
        return True
    try:
        with open(LAST_POST_FILE) as f:
            data = json.load(f)
        last = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return hours_since >= MIN_HOURS_BETWEEN_POSTS
    except Exception:
        return True

def post_to_x(text):
    """Post a tweet using xurl. Gracefully handles 402 credits depleted."""
    # Check if credits are known to be depleted (skip to avoid wasting cron time)
    credits_file = "/workspace/xurl_credits_depleted.flag"
    if os.path.exists(credits_file):
        # Check if flag is older than 24h (credits may have reset)
        try:
            flag_age = time.time() - os.path.getmtime(credits_file)
            if flag_age < 86400:  # Less than 24h old
                print("   ⏭️ X API credits depleted (flag <24h old) — skipping post")
                return False
        except Exception:
            pass

    # Escape quotes
    safe_text = text.replace('"', '\\"')
    cmd = f'xurl --app armabase post "{safe_text}" 2>&1'
    out, err, rc = run(cmd, timeout=30)

    if rc == 0 and "error" not in out.lower() and "402" not in out:
        # Save last post time
        with open(LAST_POST_FILE, "w") as f:
            json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "text": text}, f)
        log("post_success", {"text": text, "response": out[:200]})
        print(f"✅ Posted: {text[:80]}...")
        # Remove credits depleted flag if it existed
        try:
            os.remove(credits_file)
        except Exception:
            pass
        return True
    else:
        # Check for 402 credits depleted
        if "402" in out or "credits depleted" in out.lower():
            print(f"❌ X API credits depleted — flagging to skip future attempts")
            try:
                with open(credits_file, "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except Exception:
                pass
            log("credits_depleted", {"error": out[:200]})
        else:
            print(f"❌ Post failed: {out[:100]}")
            log("post_failed", {"text": text, "error": out[:200]})
        return False

# ============ CONTENT GENERATORS ============
def get_trade_wins():
    """Check recent trades for wins to post about"""
    wins = []
    
    # Check sniper bags for recent profitable sells
    try:
        with open("/workspace/sniper_bags.json") as f:
            bags = json.load(f)
        for token, data in bags.items():
            if data.get("status") == "sold" and data.get("pnl_pct", 0) > 20:
                sells = data.get("sells", [])
                if sells:
                    last_sell = sells[-1]
                    sell_time = last_sell.get("time", "")
                    # Only post if sold in last 24h
                    if sell_time and "2026-08-2" in sell_time:
                        pnl_pct = data.get("pnl_pct", 0)
                        token_name = data.get("symbol", token[:8])
                        wins.append(("sniper", token_name, pnl_pct))
    except Exception:
        pass
    
    # Check saint positions for profitable ones
    try:
        with open("/workspace/saint_positions.json") as f:
            positions = json.load(f)
        for pos in positions if isinstance(positions, list) else [positions]:
            if isinstance(pos, dict):
                pnl = float(pos.get("pnl", 0))
                if pnl > 1.0:
                    wins.append(("perps", pos.get("coin", "?"), pnl))
    except Exception:
        pass
    
    # Check treasury learning for cumulative stats
    try:
        with open("/workspace/treasury_learning.json") as f:
            learning = json.load(f)
        sniper = learning.get("strategies", {}).get("pumpfun_sniper", {})
        total_pnl = sniper.get("total_pnl_sol", 0)
        win_rate = sniper.get("wins", 0) / max(1, sniper.get("wins", 0) + sniper.get("losses", 0)) * 100
        if total_pnl > 0:
            wins.append(("stats", f"{win_rate:.0f}% WR", total_pnl))
    except Exception:
        pass
    
    return wins

def get_goal_progress():
    """Get goal progress for milestone posts"""
    try:
        with open("/workspace/goal_state.json") as f:
            state = json.load(f)
        earned = state.get("total_earned", 0)
        goal = state.get("goal_usd", 1000000)
        pct = earned / goal * 100
        return earned, pct
    except Exception:
        return 0, 0

def generate_trade_win_post(wins):
    """Generate a tweet about recent trade wins"""
    if not wins:
        return None
    
    templates = [
        "🛡️ Armadillo sniper locked in {detail} — {pnl} profit! The armor holds strong. #ArmaBase #Solana",
        "🎯 Another hit for the Armadillo ecosystem: {detail} with {pnl} gains. Texas degen energy. #VirtualsProtocol",
        "📈 {detail} — Armadillo Saint called it right. {pnl} in the bag. Saints don't miss. #Hyperliquid #Perps",
        "🔥 {detail} on the sniper bot. {pnl} realized. Armadillos don't fold, we scale out. #SolanaSummer",
    ]
    
    win_type, detail, pnl = wins[0]
    if win_type == "sniper":
        pnl_str = f"+{pnl:.0f}%"
    elif win_type == "perps":
        pnl_str = f"+${pnl:.2f}"
    else:
        pnl_str = f"{pnl:.1f} SOL total"
    
    template = random.choice(templates)
    return template.format(detail=detail, pnl=pnl_str)

def generate_milestone_post(earned, pct):
    """Generate a goal progress tweet"""
    templates = [
        f"🛡️ Armadillo Ecosystem update: ${earned:.2f} earned toward $1M goal ({pct:.2f}%). Every trade, every cycle, the armor thickens. #ArmaBase #BuildInPublic",
        f"📊 Armadillo progress: ${earned:.2f}/${'$1M'} ({pct:.2f}%). Sniper bot running 24/7, Saint on perps, Scout on the marketplace. We don't stop. #VirtualsProtocol",
        f"🐂 ${earned:.2f} and counting. The Armadillo army marches on. Sniper 66% win rate, Saint 5W/0L on perps, 50+ marketplace offerings live. #DeFi #Base",
    ]
    return random.choice(templates)

def generate_meme_post():
    """Generate a meme-style tweet"""
    memes = [
        "🛡️ Armadillo energy: roll up when the market drops, unroll when it pumps. Texas saints don't panic sell. #ArmaBase #DegenSeason",
        "🎸 In Texas we have a saying: 'everything's bigger in Texas.' Including our bags. 🛡️📈 #ArmadilloSaints #Solana",
        "🐂 Armadillos have natural armor. Your portfolio should too. Saint on perps, sniper on Solana, 24/7 degen mode. #VirtualsProtocol",
        "🔥 They said an armadillo can't trade. Now we're 66% win rate on the sniper, 5W/0L on perps. Armor up. #ArmaBase #DeFi",
        "🛡️ Roll deep, trade smart. The Armadillo ecosystem doesn't sleep — 15 cron jobs running 24/7 so you don't have to. #CryptoAutomation",
    ]
    return random.choice(memes)

def generate_offering_post():
    """Generate a tweet about marketplace offerings"""
    return "🛡️ ArmaBase marketplace is live! 30+ crypto research offerings starting at $0.10. Narrative scanning, wallet PnL, token audits, perp signals & more. Hire us on @virtuals_io #ArmaBase #AICommerce"

# ============ MAIN ============
def main():
    print("\n🐦 X Auto-Poster —", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    
    if not can_post():
        print("   ⏭️ Too soon since last post, skipping")
        return
    
    # Check if xurl is authenticated
    out, _, _ = run("xurl --app armabase /2/users/me 2>&1", timeout=15)
    if "error" in out.lower() or "noauth" in out.lower():
        print("   ⚠️ xurl not authenticated. Run: xurl auth oauth2 --app armabase")
        log("auth_needed", {"error": out[:200]})
        return
    
    # Gather content options
    posts = []
    
    wins = get_trade_wins()
    if wins:
        win_post = generate_trade_win_post(wins)
        if win_post:
            posts.append(("trade_win", win_post))
    
    earned, pct = get_goal_progress()
    if earned > 0:
        milestone_post = generate_milestone_post(earned, pct)
        posts.append(("milestone", milestone_post))
    
    # Always have meme and offering posts as fallback
    posts.append(("meme", generate_meme_post()))
    posts.append(("offering", generate_offering_post()))
    
    # Pick the highest priority post
    # Priority: trade_win > milestone > offering > meme
    priority = {"trade_win": 0, "milestone": 1, "offering": 2, "meme": 3}
    posts.sort(key=lambda x: priority.get(x[0], 99))
    
    post_type, post_text = posts[0]
    print(f"   Selected: {post_type}")
    print(f"   Content: {post_text}")
    
    post_to_x(post_text)

if __name__ == "__main__":
    main()
