"""
╔══════════════════════════════════════════════════════════╗
║       NEARCERTAIN — "Almost Sure? Think Again."          ║
║                                                          ║
║  Strategy: Prediction markets systematically overprice  ║
║  near-certain events. When YES is bid to 75-95¢, NO     ║
║  still wins ~70% of the time. Buy NO at 5-25¢.          ║
║                                                          ║
║  Backtest: 1,555 trades | 70.1% WR | +52% edge          ║
║  Data: 1.1B on-chain records (SII-WANGZJ dataset)        ║
║                                                          ║
║  Entry window: YES priced at 75-95¢ (NO at 5-25¢)       ║
║  Hold: up to 7 days (backtest WR improves further out)   ║
║  Blocks: sports, crypto (known weak spots)              ║
║  Sizing: higher NO price = bigger stake (YES most        ║
║  aggressively overbid = strongest edge)                  ║
║                                                          ║
║  SETUP:  pip install anthropic requests feedparser       ║
║  RUN:    python nearcertain.py --single-scan             ║
╚══════════════════════════════════════════════════════════╝
"""

import time
import json
import os
import sys
import re
import requests
from datetime import datetime, timezone, timedelta
import anthropic

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False

# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID  = os.environ.get("TELEGRAM_CHANNEL_ID", "")
TELEGRAM_PERSONAL_ID = os.environ.get("TELEGRAM_PERSONAL_ID", "")

SCREENER_MODEL     = "claude-haiku-4-5-20251001"
PAPER_TRADING      = True
STARTING_BANKROLL  = 1000.00
LOG_FILE           = "nearcertain_log.json"
SCAN_INTERVAL_MINS = 60

# ── Entry window ─────────────────────────────────────────
# YES must be priced at 75-95¢ → NO at 5-25¢
# The market thinks it's nearly certain → backtest says NO wins 70%
YES_MIN = 75    # YES at least 75¢ (NO ≤ 25¢)
YES_MAX = 95    # YES at most 95¢ (NO ≥ 5¢) — above 95 is genuine certainty

# ── Hold window ──────────────────────────────────────────
# Backtest shows WR IMPROVES with longer hold on NearCertain:
# T-7d: 74.9% WR (+56.9% edge) vs T-1d: 73.8% WR (+55.8% edge)
# Structural mispricing is baked in from day one — time doesn't decay it
MAX_HOLD_DAYS  = 7.0   # up to 7 days
MIN_HOLD_HOURS = 2     # don't enter if closing in under 2h

# ── Stake sizing ─────────────────────────────────────────
# Higher NO price = YES was most aggressively overbid = strongest mispricing
# 20-25¢ NO = YES was bid to 75-80¢ = weakest near-certain signal → smaller stake
# 5-10¢ NO  = YES was bid to 90-95¢ = strongest overbid → larger stake
STAKE_TIERS = [
    {"max_no": 10, "pct": 1.50},   # NO 5-10¢  (YES 90-95¢): 1.5% — extreme overbid
    {"max_no": 15, "pct": 1.00},   # NO 10-15¢ (YES 85-90¢): 1.0%
    {"max_no": 20, "pct": 0.75},   # NO 15-20¢ (YES 80-85¢): 0.75%
    {"max_no": 25, "pct": 0.50},   # NO 20-25¢ (YES 75-80¢): 0.5% — marginal signal
]

MIN_VOLUME        = 2000    # thin books = adverse selection
DAILY_LOSS_LIMIT  = 150.00
NEWS_LOOKBACK_HOURS = 72
MAX_OPEN_POSITIONS  = 60    # 7-day window = more concurrent positions

# ── Blocked categories ────────────────────────────────────
# Crypto: backtest shows 57% WR in NearCertain — barely above 50%, not worth the risk
# Sports: live score markets, YES legitimately near-certain mid-game
BLOCKED_CATEGORIES = {
    "sports",
    "crypto",
    "conflict",
}

# ── Keyword blocks ────────────────────────────────────────
BLOCK_KEYWORDS = [
    "up or down", "odd or even", "odd/even", "total kills",
    "invasion", "strike on", "attack on", "declare war",
    "natural disaster", "hurricane", "earthquake",
    "tsunami", "explosion", "shooting", "assassination",
]

NEWS_FEEDS = [
    ("Reuters",    "https://feeds.reuters.com/reuters/worldNews"),
    ("BBC",        "https://feeds.bbci.co.uk/news/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Sky News",   "https://feeds.skynews.com/feeds/rss/world.xml"),
]

# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────

def telegram_send(msg, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    targets = []
    if chat_id:
        targets = [chat_id]
    else:
        if TELEGRAM_CHANNEL_ID:
            targets.append(TELEGRAM_CHANNEL_ID)
        if TELEGRAM_PERSONAL_ID:
            targets.append(TELEGRAM_PERSONAL_ID)
    for cid in targets:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=8
            )
        except Exception:
            pass

def telegram_new_trade(trade, state):
    roi = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    msg = (
        f"🎯 <b>NEARCERTAIN — New Trade</b>\n\n"
        f"NO @ {trade['entry_no_price']}¢\n"
        f"<b>{trade['market'][:70]}</b>\n\n"
        f"Category: {trade.get('category','?')}\n"
        f"Stake: ${trade['stake']:.2f}\n"
        f"Closes in: {trade['closes_in_days']:.1f}d\n\n"
        f"Bankroll: ${state['bankroll']:.2f} ({roi:+.1f}%)"
    )
    telegram_send(msg)

def telegram_trade_resolved(trade, state):
    roi = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    won = trade.get("won", False)
    emoji = "✅" if won else "❌"
    pnl = trade.get("realized_pnl", 0)
    msg = (
        f"{emoji} <b>NEARCERTAIN — {'WON' if won else 'LOST'}</b>\n\n"
        f"<b>{trade['market'][:70]}</b>\n\n"
        f"P&L: ${pnl:+.2f}\n"
        f"Entry: NO @ {trade['entry_no_price']}¢\n\n"
        f"Bankroll: ${state['bankroll']:.2f} ({roi:+.1f}%)"
    )
    telegram_send(msg)

# ─────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            s = json.load(f)
        log(f"📂 Loaded — {len(s.get('trades',[]))} trades | bankroll ${s.get('bankroll',STARTING_BANKROLL):.2f} | {len([t for t in s.get('trades',[]) if t['status']=='open'])} open")
        return s
    return {
        "bankroll":    STARTING_BANKROLL,
        "trades":      [],
        "daily_loss":  0.0,
        "daily_reset": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scan_count":  0,
        "started":     datetime.now(timezone.utc).isoformat(),
    }

def save_state(state):
    with open(LOG_FILE, "w") as f:
        json.dump(state, f, indent=2)
    closed = [t for t in state["trades"] if t["status"] == "closed"]
    open_t = [t for t in state["trades"] if t["status"] == "open"]
    log(f"💾 Saved — bankroll ${state['bankroll']:.2f} | {len(open_t)} open positions")

def reset_daily_loss_if_needed(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 Daily loss reset")

# ─────────────────────────────────────────────────────────
#  SETTLE + RESOLVE
# ─────────────────────────────────────────────────────────

def _settle(trade, won, state):
    trade["status"]      = "closed"
    trade["won"]         = won
    trade["resolved_at"] = datetime.now(timezone.utc).isoformat()

    if won:
        payout               = round(trade["stake"] * 100 / trade["entry_no_price"], 2)
        trade["realized_pnl"] = round(payout - trade["stake"], 2)
        state["bankroll"]    = round(state["bankroll"] + payout, 2)
        log(f"  ✅ WON   +${trade['realized_pnl']:.2f}  {trade['market'][:55]}")
    else:
        trade["realized_pnl"] = round(-trade["stake"], 2)
        state["daily_loss"]  = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST  -${trade['stake']:.2f}  {trade['market'][:55]}")

    log(f"     Bankroll now: ${state['bankroll']:.2f}")
    telegram_trade_resolved(trade, state)


def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state
    log(f"🔍 Checking {len(open_trades)} open position(s)...")
    for trade in open_trades:
        market_id = trade.get("market_id", "")
        if not market_id or market_id.startswith("d0"):
            continue
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=10
            )
            if r.status_code != 200:
                continue
            mkt = r.json()
            if mkt.get("active", True) and not mkt.get("closed", False):
                continue
            prices   = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
            no_price = round(float(prices[1]) * 100)
            won = no_price >= 99
            _settle(trade, won, state)
        except Exception as e:
            log(f"  ⚠️  Could not check {market_id}: {e}")
    return state

# ─────────────────────────────────────────────────────────
#  CATEGORY DETECTION
# ─────────────────────────────────────────────────────────

def get_category(question):
    q = question.lower()
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "crypto",
                              "solana", "bnb", "xrp", "defi", "sol", "doge"]):
        return "crypto"
    if any(k in q for k in ["temperature", "weather", "rain", "snow", "°c", "°f",
                              "celsius", "fahrenheit", "humidity", "forecast"]):
        return "weather"
    if any(k in q for k in ["election", "vote", "president", "congress", "senate",
                              "prime minister", "parliament", "referendum", "trump",
                              "biden", "harris", "democrat", "republican"]):
        return "politics"
    if any(k in q for k in ["gdp", "inflation", "cpi", "fed", "fomc", "rate",
                              "earnings", "revenue", "stock", "nasdaq", "s&p",
                              "tesla", "apple", "nvidia", "amazon"]):
        return "economics"
    if any(k in q for k in ["goal", "win", "score", "match", "game", "nba", "nfl",
                              "fifa", "premier league", "championship", "tournament",
                              "player", "team", "sport", "cup", "league"]):
        return "sports"
    if any(k in q for k in ["outbreak", "cases", "death", "disease", "virus",
                              "pandemic", "measles", "covid", "flu", "mpox"]):
        return "health"
    if any(k in q for k in ["war", "military", "attack", "troops", "ceasefire",
                              "invasion", "strike", "missile", "conflict"]):
        return "conflict"
    return "other"

# ─────────────────────────────────────────────────────────
#  NEWS MONITOR
# ─────────────────────────────────────────────────────────

def scan_news_feeds():
    if not FEEDPARSER_AVAILABLE:
        return []
    headlines = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    for name, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                headlines.append({
                    "source": name,
                    "title":  entry.get("title", ""),
                    "summary": entry.get("summary", "")[:200],
                })
        except Exception:
            pass
    return headlines

def haiku_news_screen(markets, headlines):
    """
    Use Haiku to flag any markets that should be SKIPPED due to breaking news
    that might make the near-certain outcome actually resolve YES.
    This is the critical filter for NearCertain — we're betting NO on things
    that seem certain, so news confirming the YES outcome kills the trade.
    """
    if not headlines or not markets:
        return set()   # skip none by default

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    market_list = "\n".join(
        f'ID:{m["id"]} [{m["category"]}] YES={m["yes"]}¢ "{m["question"][:80]}"'
        for m in markets
    )
    headline_txt = "\n".join(
        f'[{h["source"]}] {h["title"]}'
        for h in headlines[:50]
    )

    prompt = (
        f"You are screening prediction markets for a NO-bias trading bot.\n"
        f"The bot buys NO on markets where YES is priced 75-95¢ (near-certain).\n"
        f"Strategy: these near-certain events usually DON'T happen.\n\n"
        f"BREAKING NEWS:\n{headline_txt}\n\n"
        f"MARKETS (all have YES priced 75-95¢):\n{market_list}\n\n"
        f"Flag ONLY markets where breaking news CONFIRMS the YES outcome is "
        f"actually happening right now — making NO a clear loser.\n"
        f"Be conservative — only flag obvious YES confirmations.\n"
        f"Do NOT flag markets just because news is related.\n\n"
        f"Return JSON array of IDs to SKIP: [\"id1\", \"id2\"] or []"
    )

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if m:
            skipped = json.loads(m.group(0))
            if skipped:
                log(f"  📰 News screen blocked {len(skipped)} market(s)")
            return set(skipped)
    except Exception as e:
        log(f"  ⚠️  News screen error: {e}")
    return set()

# ─────────────────────────────────────────────────────────
#  MARKET FETCHING
# ─────────────────────────────────────────────────────────

def fetch_markets():
    """
    Fetch markets where YES is priced at 75-95¢ (NO at 5-25¢)
    closing within 2h to 7 days.
    """
    now = datetime.now(timezone.utc)
    markets = []
    skipped = 0

    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "active":     "true",
                "closed":     "false",
                "limit":      10000,
                "order":      "endDate",
                "ascending":  "true",
            },
            timeout=15
        )
        if r.status_code != 200:
            log(f"⚠️  Gamma API error: {r.status_code}")
            return []

        raw_markets = r.json()
        log(f"   📄 Fetched {len(raw_markets)} total markets")

        for m in raw_markets:
            # Skip non-binary or missing data
            if not m.get("question") or not m.get("outcomePrices"):
                continue

            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            if len(prices) < 2:
                continue

            yes = round(float(prices[0]) * 100)
            no  = round(float(prices[1]) * 100)

            # Entry filter: YES must be 75-95¢
            if not (YES_MIN <= yes <= YES_MAX):
                continue

            # Volume filter
            if float(m.get("volume", 0)) < MIN_VOLUME:
                skipped += 1
                continue

            # Time filter: must close within 2h to 7 days
            end_str = m.get("endDate") or m.get("endDateIso") or ""
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                continue

            cid = (end_dt - now).total_seconds() / 86400
            if cid < (MIN_HOLD_HOURS / 24) or cid > MAX_HOLD_DAYS:
                continue

            cat = get_category(m["question"])

            # Category block
            if cat in BLOCKED_CATEGORIES:
                skipped += 1
                continue

            # Keyword block
            q_lower = m["question"].lower()
            if any(k in q_lower for k in BLOCK_KEYWORDS):
                skipped += 1
                continue

            # enableOrderBook check
            if not m.get("enableOrderBook", True):
                skipped += 1
                continue

            markets.append({
                "id":             str(m.get("id", "")),
                "question":       m["question"],
                "yes":            yes,
                "no":             no,
                "volume":         float(m.get("volume", 0)),
                "category":       cat,
                "closes":         end_dt.isoformat(),
                "closes_in_days": round(cid, 3),
            })

    except Exception as e:
        log(f"⚠️  Market fetch failed: {e}")
        return []

    markets.sort(key=lambda x: x["closes_in_days"])
    log(f"✅ {len(markets)} NearCertain candidates (YES 75-95¢, closing 2h-7d)")
    return markets

# ─────────────────────────────────────────────────────────
#  STAKE SIZING
# ─────────────────────────────────────────────────────────

def calc_stake(no_price, bankroll):
    """
    Higher NO price = YES was most aggressively overbid = strongest edge.
    NO at 5-10¢ (YES was 90-95¢) gets the largest stake.
    NO at 20-25¢ (YES was 75-80¢) gets the smallest.
    """
    for tier in STAKE_TIERS:
        if no_price <= tier["max_no"]:
            return round(bankroll * tier["pct"] / 100, 2)
    return 0.0

# ─────────────────────────────────────────────────────────
#  PLACE TRADE
# ─────────────────────────────────────────────────────────

def place_trade(market, state):
    no_price = market["no"]
    stake    = calc_stake(no_price, state["bankroll"])

    if stake <= 0:
        return
    if stake > state["bankroll"] * 0.20:   # hard cap 20% of bankroll
        stake = round(state["bankroll"] * 0.20, 2)

    # Dedup — already have this market open?
    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    if market["id"] in open_ids:
        return

    # Daily loss limit
    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  ⛔ Daily loss limit hit — skipping")
        return

    # Max open positions
    open_count = len([t for t in state["trades"] if t["status"] == "open"])
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"  ⛔ Max open positions ({MAX_OPEN_POSITIONS}) reached")
        return

    # Deployment cap — don't deploy more than 60% of bankroll at once
    deployed = sum(t["stake"] for t in state["trades"] if t["status"] == "open")
    if deployed >= state["bankroll"] * 0.60:
        log(f"  ⛔ Deployment cap (60%) reached — ${deployed:.2f} already out")
        return

    trade = {
        "id":             f"NC{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "market_id":      market["id"],
        "market":         market["question"],
        "category":       market["category"],
        "position":       "NO",
        "entry_no_price": no_price,
        "entry_yes_price": market["yes"],
        "stake":          stake,
        "closes":         market["closes"],
        "closes_in_days": market["closes_in_days"],
        "status":         "open",
        "placed_at":      datetime.now(timezone.utc).isoformat(),
        "paper":          True,
        "model":          "nearcertain",
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  🔵 NO @ {no_price}¢ (YES was {market['yes']}¢) | ${stake:.2f} | "
        f"{market['category']} | {market['question'][:55]}")

    telegram_new_trade(trade, state)

# ─────────────────────────────────────────────────────────
#  PORTFOLIO DISPLAY
# ─────────────────────────────────────────────────────────

def print_portfolio(state):
    trades  = state["trades"]
    closed  = [t for t in trades if t["status"] == "closed"]
    open_t  = [t for t in trades if t["status"] == "open"]
    won     = [t for t in closed if t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed)
    deployed = sum(t["stake"] for t in open_t)
    wr = len(won) / len(closed) * 100 if closed else 0
    roi = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100

    print("\n" + "═"*65)
    print("  NEARCERTAIN  ·  'Almost Sure? Think Again.'")
    print("═"*65)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Deployed       ${deployed:.2f} across {len(open_t)} open positions")
    print(f"  Closed         {len(closed)}  ({len(won)}W / {len(closed)-len(won)}L"
          f"  —  {wr:.0f}% win rate)" if closed else f"  Closed         0")
    print(f"  Total Scans    {state.get('scan_count', 0)}")
    print(f"  Telegram       {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    print("═"*65)

    # Category breakdown
    if closed:
        cats = {}
        for t in closed:
            c = t.get("category", "other")
            if c not in cats:
                cats[c] = {"w": 0, "l": 0}
            if t.get("won"):
                cats[c]["w"] += 1
            else:
                cats[c]["l"] += 1
        print(f"\n  Win rate by category:")
        for cat, v in sorted(cats.items(), key=lambda x: -(x[1]["w"]+x[1]["l"])):
            total = v["w"] + v["l"]
            wr_c  = v["w"] / total * 100
            print(f"    {cat:<15} {wr_c:.0f}% ({v['w']}W/{v['l']}L)")

    if open_t:
        print(f"\n  OPEN POSITIONS ({len(open_t)}):")
        for t in sorted(open_t, key=lambda x: x["closes_in_days"])[:10]:
            print(f"  🔵 NO@{t['entry_no_price']}¢ | ${t['stake']:.2f} | "
                  f"{t['closes'][:10]} ({t['closes_in_days']:.1f}d) | "
                  f"{t['market'][:45]}")
        if len(open_t) > 10:
            print(f"  ... and {len(open_t)-10} more")
    print()

# ─────────────────────────────────────────────────────────
#  MAIN SCAN
# ─────────────────────────────────────────────────────────

def single_scan():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'╔'+'═'*58+'╗'}")
    print(f"║  NEARCERTAIN  ·  Almost Sure? Think Again.              ║")
    print(f"║  {now_str:<56}║")
    print(f"{'╚'+'═'*58+'╝'}\n")

    state = load_state()
    reset_daily_loss_if_needed(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    # ── Step 1: Resolve open trades ───────────────────────
    log("── Step 1: Resolve open trades ─────────────────────────")
    resolve_open_trades(state)

    # ── Step 2: Fetch NearCertain markets ────────────────
    log("── Step 2: Fetch NearCertain markets (YES 75-95¢) ──────")
    markets = fetch_markets()

    if not markets:
        log("  No NearCertain candidates this scan")
        save_state(state)
        print_portfolio(state)
        return

    # ── Step 3: News screen ──────────────────────────────
    log("── Step 3: News screen (block YES-confirming news) ─────")
    headlines  = scan_news_feeds()
    skip_ids   = haiku_news_screen(markets, headlines)
    candidates = [m for m in markets if m["id"] not in skip_ids]
    log(f"   {len(candidates)} markets pass news screen ({len(skip_ids)} blocked)")

    # ── Step 4: Place trades ──────────────────────────────
    log(f"── Step 4: Place trades ({len(candidates)} candidates) ──────────")
    placed = 0
    for market in candidates:
        place_trade(market, state)
        placed += 1

    if placed:
        log(f"   {placed} new trade(s) placed")
    else:
        log("   No new trades this scan")

    # ── Step 5: Save ──────────────────────────────────────
    log("── Step 5: Save ─────────────────────────────────────────")
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  NEARCERTAIN  ·  Almost Sure? Think Again.               ║")
    print(f"║  Interval: {SCAN_INTERVAL_MINS}min                                          ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        return

    while True:
        try:
            single_scan()
            log(f"💤 Sleeping {SCAN_INTERVAL_MINS} min...\n")
            time.sleep(SCAN_INTERVAL_MINS * 60)
        except KeyboardInterrupt:
            log("🛑 Stopped")
            break
        except Exception as e:
            log(f"❌ Unexpected error: {e} — retrying in 60s")
            time.sleep(60)


if __name__ == "__main__":
    if "--single-scan" in sys.argv:
        if not ANTHROPIC_API_KEY:
            print("❌  ANTHROPIC_API_KEY not set")
            sys.exit(1)
        single_scan()
    else:
        run_loop()
