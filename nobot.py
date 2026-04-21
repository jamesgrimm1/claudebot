"""
╔══════════════════════════════════════════════════════════╗
║         NOBOT — "Nothing Ever Happens"                   ║
║                                                          ║
║  Strategy: 73.3% of Polymarket markets resolve NO.       ║
║  Buy NO on markets closing within 2-12 hours ONLY.      ║
║  Data: 89%+ WR on <12h holds vs 48% WR on >12h holds.  ║
║  Haiku news screen filters obvious disasters.            ║
║  Hold to resolution — full 100¢ payout on every win.    ║
║  No early exit. No Opus. Volume is the edge.             ║
║                                                          ║
║  Sizing (compounds with bankroll):                       ║
║  $0.45–$0.52 → 0.50% of bankroll                        ║
║  $0.52–$0.58 → 0.375% of bankroll                       ║
║  $0.58–$0.62 → 0.25% of bankroll                        ║
║  Above $0.62  → skip                                     ║
║                                                          ║
║  Entry window: 2h–12h before market close               ║
║  Hold: resolve at market close — full payout            ║
║                                                          ║
║  SETUP:  pip install anthropic requests feedparser       ║
║  RUN:    python nobot.py --single-scan                   ║
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
LOG_FILE           = "nobot_log.json"
SCAN_INTERVAL_MINS = 60

# Entry price window (NO price in cents)
NO_ENTRY_MIN = 45   # 45¢ NO = 55¢ YES
NO_ENTRY_MAX = 62   # 62¢ NO = 38¢ YES

# Exit target — sell NO when it reaches this price
# No early exit — hold all trades to market resolution for full payout

# Stake sizing by NO entry price
STAKE_TIERS = [
    {"max_no": 52, "pct": 0.50},    # 45-52¢: 0.5% of bankroll
    {"max_no": 58, "pct": 0.375},   # 52-58¢: 0.375% of bankroll
    {"max_no": 62, "pct": 0.25},    # 58-62¢: 0.25% of bankroll
]

# Min market volume — avoid thin books
MIN_VOLUME = 1000

# Max hold — only enter markets closing within 12 hours
# Data shows 89%+ WR on <12h holds vs 48% WR on >12h holds
MAX_HOLD_DAYS = 0.5   # 12 hours (was 14 days)

# Min hold hours — don't buy something closing in under 2 hours
MIN_HOLD_HOURS = 2

# Daily loss limit
DAILY_LOSS_LIMIT = 200.00

# News lookback for Haiku screen
NEWS_LOOKBACK_HOURS = 72

# Categories blocked entirely
BLOCKED_CATEGORIES = {
    "sports",
    "conflict",    # geopolitical military strikes, invasions
}

# Keyword blocks — markets to always skip regardless of price
BLOCK_KEYWORDS = [
    "up or down", "odd or even", "odd/even", "total kills",
    "will there be a", "invasion", "strike on", "attack on",
    "declare war", "natural disaster", "hurricane", "earthquake",
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─────────────────────────────────────────────────────────
#  CATEGORY HELPER
# ─────────────────────────────────────────────────────────

def get_category(question):
    q = question.lower()
    if any(k in q for k in ["nba", "nfl", "mlb", "nhl", "soccer", "football",
                              "tennis", "golf", "match", "game", "fc ", " united",
                              "spread", "o/u", "rebounds", "assists", "esport",
                              "valorant", "counter-strike", "dota", "bucks", "nets",
                              "knicks", "bulls", "heat", "hawks", "sixers", "suns",
                              "nuggets", "warriors", "lakers", "celtics", "rockets",
                              "f1", "formula 1", "grand prix", "podium", "bottas",
                              "verstappen", "hamilton", "leclerc", "norris",
                              "pickleball", "ppa", "jansen", "sock", "bjerg",
                              "ufc", "mma", "boxing", "fight", "knockout",
                              "blue jays", "brewers", "yankees", "red sox", "cubs",
                              "dodgers", "giants", "braves", "mets", "astros",
                              "vs.", " vs ", "at the ", "open:", "cup:", "league:",
                              "pitcher", "batter", "touchdown", "goal scorer",
                              "win the", "beat the", "defeat"]):
        return "sports"
    if any(k in q for k in ["war", "military", "attack", "strike", "invasion",
                              "ceasefire", "conflict", "troops", "missile",
                              "hezbollah", "hamas", "houthi", "airstrike"]):
        return "conflict"
    if any(k in q for k in ["temperature", "weather", "rain", "snow", "°c", "°f"]):
        return "weather"
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto"]):
        return "crypto"
    if any(k in q for k in ["president", "election", "senate", "congress", "vote",
                              "trump", "biden", "policy", "tariff"]):
        return "politics"
    if any(k in q for k in ["fed", "rate", "inflation", "gdp", "jobs", "economy",
                              "ecb", "nasdaq", "s&p", "stock", "earnings"]):
        return "economics"
    return "other"


# ─────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────

def send_telegram(msg, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    target = chat_id or TELEGRAM_CHANNEL_ID
    if not target:
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": target, "text": msg, "parse_mode": "HTML"}
        r    = requests.post(url, json=data, timeout=10)
        if r.status_code == 200:
            log(f"📨 Telegram → {target}")
    except Exception as e:
        log(f"⚠️  Telegram failed: {e}")


def telegram_new_trade(trade, state):
    # NoBot trades are high volume — no per-trade messages
    # Summary is sent every 12 hours instead
    pass


def telegram_trade_resolved(trade, state):
    # NoBot trades are high volume — no per-trade messages
    # Summary is sent every 12 hours instead
    pass




def should_send_summary(state):
    """Send summary every 12 hours."""
    now       = datetime.now(timezone.utc)
    last_sent = state.get("last_summary_sent", "")
    if not last_sent:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sent)
        return (now - last_dt).total_seconds() >= 12 * 3600
    except Exception:
        return True


def telegram_nobot_summary(state):
    """12-hour summary to private channel only."""
    if not TELEGRAM_PERSONAL_ID:
        return

    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    won_pnl  = sum(t.get("realized_pnl", 0) for t in won_t)
    lost_pnl = sum(t.get("realized_pnl", 0) for t in lost_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0
    roi      = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    deployed = sum(t["stake"] for t in open_t)

    msg = (
        f"📊 <b>NOBOT — 12H SUMMARY</b>\n"
        f"{'─' * 28}\n"
        f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
        f"💰 Realized P&L: <b>${realized:+.2f}</b>\n"
        f"{'─' * 28}\n"
        f"📋 Open: <b>{len(open_t)}</b> positions (${deployed:.2f} deployed)\n"
        f"{'─' * 28}\n"
        f"📈 Win rate: <b>{win_rate:.0f}%</b> "
        f"({len(won_t)}W / {len(lost_t)}L)\n"
        f"✅ Won: <b>${won_pnl:+.2f}</b>\n"
        f"❌ Lost: <b>${lost_pnl:.2f}</b>\n"
        f"{'─' * 28}\n"
        f"🔄 Total scans: <b>{state.get('scan_count', 0)}</b>"
    )
    send_telegram(msg, TELEGRAM_PERSONAL_ID)
    log("📨 NoBot 12h summary sent")

# ─────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            s = json.load(f)
        open_ct = len([t for t in s.get("trades", []) if t["status"] == "open"])
        log(f"📂 Loaded — {len(s.get('trades', []))} trades | bankroll ${s.get('bankroll', STARTING_BANKROLL):.2f} | {open_ct} open")
        return s
    log("📂 No log — starting fresh")
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
    open_ct = len([t for t in state["trades"] if t["status"] == "open"])
    log(f"💾 Saved — bankroll ${state['bankroll']:.2f} | {open_ct} open positions")


def reset_daily_loss(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 Daily loss reset")
    return state


# ─────────────────────────────────────────────────────────
#  DATE HELPERS
# ─────────────────────────────────────────────────────────

def parse_utc(date_str):
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def days_until(dt):
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


# ─────────────────────────────────────────────────────────
#  MARKET RESOLUTION
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

            prices   = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
            yes_price = round(float(prices[0]) * 100)
            no_price  = round(float(prices[1]) * 100)

            # Check if market resolved
            if mkt.get("active", True) and not mkt.get("closed", False):
                continue

            won = no_price >= 99
            _settle(trade, won, state)

        except Exception as e:
            log(f"  ⚠️  Could not check {market_id}: {e}")
    return state


# ─────────────────────────────────────────────────────────
#  NEWS SCREEN — lightweight Haiku check
# ─────────────────────────────────────────────────────────

def get_recent_headlines():
    """Fetch recent headlines from RSS feeds."""
    if not FEEDPARSER_AVAILABLE:
        return []
    headlines = []
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    for name, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                published = entry.get("published_parsed")
                if published:
                    try:
                        pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                        if pub_dt < cutoff:
                            continue
                    except Exception:
                        pass
                headlines.append(f"[{name}] {entry.get('title', '')}")
        except Exception:
            pass
    return headlines


def haiku_news_screen(markets, headlines):
    """
    Single Haiku call that screens ALL markets against recent news.
    Returns set of market IDs to SKIP due to triggering news.
    Much cheaper than screening each market individually.
    """
    if not headlines or not markets:
        return set()

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    mkt_list = "\n".join(
        f'ID:{m["id"]} | "{m["question"]}"'
        for m in markets[:100]  # batch up to 100 at once
    )
    headline_txt = "\n".join(headlines[:80])

    prompt = (
        f"You are screening prediction markets for a NO-trading bot.\n"
        f"Today: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n\n"
        f"NEWS HEADLINES (last {NEWS_LOOKBACK_HOURS}h):\n{headline_txt}\n\n"
        f"MARKETS:\n{mkt_list}\n\n"
        f"Return IDs of markets to SKIP because recent news makes a YES resolution\n"
        f"significantly more likely than the base rate suggests. Only flag markets\n"
        f"where there is a DIRECT and SPECIFIC news link — a headline that clearly\n"
        f"increases the probability the specific event in the market will happen.\n\n"
        f"Be conservative — only flag clear direct matches. Most markets should NOT be flagged.\n\n"
        f"Return ONLY a JSON array of IDs to skip (empty array if none):\n"
        f'["id1", "id2"]'
    )

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw   = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[[\s\S]*\]', raw)
        skip  = json.loads(match.group(0) if match else "[]")
        if skip:
            log(f"📰 News screen flagged {len(skip)} market(s) to skip")
        return set(str(s) for s in skip)
    except Exception as e:
        log(f"⚠️  News screen error: {e}")
        return set()


# ─────────────────────────────────────────────────────────
#  MARKET FETCHING
# ─────────────────────────────────────────────────────────

def fetch_markets():
    # Paginate through active markets — cap at 10,000 to keep scan fast
    MAX_FETCH = 10000
    raw       = []
    offset    = 0
    limit     = 500
    while len(raw) < MAX_FETCH:
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets"
                f"?active=true&closed=false&limit={limit}&offset={offset}"
                f"&order=volume&ascending=false",
                timeout=12
            )
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            raw += page
            if len(page) < limit:
                break  # last page
            offset += limit
        except Exception as e:
            log(f"⚠️  Polymarket fetch error at offset {offset}: {e}")
            break
    log(f"   📄 Fetched {len(raw)} total markets")

    now     = datetime.now(timezone.utc)
    markets = []
    skipped = 0

    for m in raw:
        if not m.get("question") or not m.get("outcomePrices"):
            continue
        end_dt = parse_utc(m.get("endDate") or m.get("end_date") or "")
        if end_dt is None:
            skipped += 1
            continue
        cid = (end_dt - now).total_seconds() / 86400
        if cid < (MIN_HOLD_HOURS / 24) or cid > MAX_HOLD_DAYS:
            skipped += 1
            continue
        try:
            prices   = json.loads(m["outcomePrices"])
            yes_price = round(float(prices[0]) * 100)
            no_price  = round(float(prices[1]) * 100)
        except Exception:
            continue

        # Only markets where NO is in our entry window
        if no_price < NO_ENTRY_MIN or no_price > NO_ENTRY_MAX:
            skipped += 1
            continue

        # Volume filter
        if float(m.get("volume", 0)) < MIN_VOLUME:
            skipped += 1
            continue

        # Use Polymarket's own tags to block sports — much more reliable than keywords
        market_tags = m.get("tags") or []
        tag_labels  = [t.get("label", "").lower() for t in market_tags if isinstance(t, dict)]
        tag_slugs   = [t.get("slug", "").lower() for t in market_tags if isinstance(t, dict)]
        is_sports   = any(
            "sport" in label or "sport" in slug or
            label in ["nfl", "nba", "mlb", "nhl", "soccer", "tennis", "golf",
                      "mma", "ufc", "boxing", "f1", "nascar", "cricket",
                      "rugby", "pickleball", "esports"]
            for label, slug in zip(tag_labels, tag_slugs)
        )
        if is_sports:
            skipped += 1
            continue

        # Also run keyword check as secondary filter
        cat = get_category(m["question"])
        if cat in BLOCKED_CATEGORIES:
            skipped += 1
            continue

        # Keyword block
        q_lower = m["question"].lower()
        if any(k in q_lower for k in BLOCK_KEYWORDS):
            skipped += 1
            continue

        # Block match result markets — "win on YYYY-MM-DD" is the universal pattern
        # for ALL soccer, basketball, esports match markets on Polymarket
        if re.search(r'win on \d{4}-\d{2}-\d{2}', q_lower):
            skipped += 1
            continue
        # Block "who will win" head-to-head markets
        if q_lower.startswith("who will") or " vs " in q_lower or " vs. " in q_lower:
            skipped += 1
            continue

        markets.append({
            "id":             str(m.get("id", "")),
            "slug":           m.get("slug", ""),
            "question":       m["question"],
            "yes_price":      yes_price,
            "no_price":       no_price,
            "volume":         float(m.get("volume", 0)),
            "category":       cat,
            "closes":         end_dt.isoformat(),
            "closes_in_days": round(cid, 2),
        })

    markets.sort(key=lambda x: x["volume"], reverse=True)
    log(f"✅ {len(markets)} candidate markets (NO in {NO_ENTRY_MIN}-{NO_ENTRY_MAX}¢ window)")
    return markets


# ─────────────────────────────────────────────────────────
#  STAKE SIZING
# ─────────────────────────────────────────────────────────

def get_stake(no_price, bankroll):
    """Returns stake based on NO entry price. Compounds with bankroll."""
    for tier in STAKE_TIERS:
        if no_price <= tier["max_no"]:
            stake = round(bankroll * tier["pct"] / 100, 2)
            return max(stake, 0.50)  # minimum 50¢ stake
    return 0.0  # above ceiling — skip


# ─────────────────────────────────────────────────────────
#  TRADE EXECUTION
# ─────────────────────────────────────────────────────────

def place_trade(market, state):
    """Place a NO trade on a market that passed all filters."""

    # Already open in this market?
    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    if market["id"] in open_ids:
        return state

    # Daily loss limit
    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit hit — no more trades today")
        return state

    no_price = market["no_price"]
    stake    = get_stake(no_price, state["bankroll"])

    if stake <= 0:
        return state

    # Payout if NO resolves (hold to resolution)
    payout = round(stake * 100 / no_price, 2)
    profit = round(payout - stake, 2)

    trade = {
        "id":               f"NB{int(time.time())}",
        "market_id":        market["id"],
        "market_slug":      market.get("slug", ""),
        "market":           market["question"],
        "position":         "NO",
        "entry_no_price":   no_price,
        "entry_yes_price":  market["yes_price"],
        "stake":            stake,
        "payout_if_wins":   payout,
        "profit_if_wins":   profit,
        "category":         market["category"],
        "closes_in_days":   market["closes_in_days"],
        "closes":           market["closes"],
        "volume":           market["volume"],
        "status":           "open",
        "placed_at":        datetime.now(timezone.utc).isoformat(),
        "paper":            True,
        "model":            "nobot-mechanical",
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  🔴 NO @ {no_price}¢ | ${stake:.2f} stake | "
        f"{market['category']} | {market['question'][:50]}")

    telegram_new_trade(trade, state)
    return state


# ─────────────────────────────────────────────────────────
#  PORTFOLIO SUMMARY
# ─────────────────────────────────────────────────────────

def print_portfolio(state):
    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0
    roi      = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    deployed = sum(t["stake"] for t in open_t)

    # Category breakdown of open positions
    cat_counts = {}
    for t in open_t:
        cat_counts[t.get("category","?")] = cat_counts.get(t.get("category","?"), 0) + 1

    print("\n" + "═" * 65)
    print("  NOBOT  ·  'Nothing Ever Happens'  ·  Base Rate NO Trader")
    print("═" * 65)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Deployed       ${deployed:.2f} across {len(open_t)} open positions")
    print(f"  Closed         {len(closed_t)}  ({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Total Scans    {state.get('scan_count', 0)}")
    print(f"  Telegram       {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    print("═" * 65)

    if cat_counts:
        print(f"\n  Open by category: {cat_counts}")

    if open_t:
        print(f"\n  OPEN POSITIONS ({len(open_t)}):")
        # Show first 20 — might have many
        for t in sorted(open_t, key=lambda x: x["closes_in_days"])[:20]:
            close_dt   = parse_utc(t.get("closes", ""))
            cid        = round(days_until(close_dt), 1) if close_dt else "?"
            closes_str = close_dt.strftime("%b %d") if close_dt else "?"
            print(f"  🔴 NO@{t['entry_no_price']}¢ | ${t['stake']:.2f} | {closes_str} ({cid}d) | {t['market'][:45]}")
        if len(open_t) > 20:
            print(f"  ... and {len(open_t) - 20} more")
    print()


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def single_scan():
    now = datetime.now(timezone.utc)
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  NOBOT  ·  'Nothing Ever Happens'  ·  Base Rate NO       ║")
    print(f"║  {now.strftime('%Y-%m-%d %H:%M UTC')}  |  Scan every 60min                  ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    # ── 12h summary ───────────────────────────────────────
    if should_send_summary(state):
        telegram_nobot_summary(state)
        state["last_summary_sent"] = now.isoformat()

    # ── Step 1: Resolve / check exits ────────────────────
    log("── Step 1: Resolve & check exits ───────────────────────")
    state = resolve_open_trades(state)

    # ── Step 2: Fetch candidates ──────────────────────────
    log("── Step 2: Fetch markets (NO in 45-62¢ window) ─────────")
    markets = fetch_markets()

    if not markets:
        log("No candidates this scan")
        save_state(state)
        print_portfolio(state)
        return

    # ── Step 3: News screen (single Haiku batch call) ─────
    log("── Step 3: News screen ──────────────────────────────────")
    headlines = get_recent_headlines()
    skip_ids  = haiku_news_screen(markets, headlines) if headlines else set()
    filtered  = [m for m in markets if m["id"] not in skip_ids]
    log(f"   {len(filtered)} markets pass news screen ({len(markets) - len(filtered)} skipped)")

    # ── Step 4: Place trades ──────────────────────────────
    log(f"── Step 4: Place trades ({len(filtered)} candidates) ────────────")
    new_trades = 0
    open_ids   = {t["market_id"] for t in state["trades"] if t["status"] == "open"}

    # Max deployment cap — never deploy more than 40% of starting bankroll at once
    max_deploy    = state["bankroll"] * 0.40
    current_deploy = sum(t["stake"] for t in state["trades"] if t["status"] == "open")

    for market in filtered:
        if market["id"] in open_ids:
            continue  # already open
        if current_deploy >= max_deploy:
            log(f"   💰 Deployment cap reached (${current_deploy:.2f} / ${max_deploy:.2f}) — holding fire")
            break
        prev_bankroll = state["bankroll"]
        state = place_trade(market, state)
        if state["bankroll"] < prev_bankroll:
            new_trades += 1
            current_deploy += (prev_bankroll - state["bankroll"])

    log(f"   {new_trades} new trade(s) placed")

    # ── Step 5: Save ──────────────────────────────────────
    log("── Step 5: Save ─────────────────────────────────────────")
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  NOBOT  ·  Continuous Mode  ·  Scanning every 60min     ║")
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
            log(f"❌ Error: {e} — retrying in 60s")
            time.sleep(60)


if __name__ == "__main__":
    if "--single-scan" in sys.argv:
        single_scan()
    else:
        run_loop()
