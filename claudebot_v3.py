"""
╔══════════════════════════════════════════════════════════╗
║         CLAUDEBOT v13 — Three-Tier Polymarket Trader     ║
║                                                          ║
║  Step 0: RSS news monitor — flags breaking edge events   ║
║  Tier 1: Short-term  1-7d  | conf≥75% | edge≥15%        ║
║  Tier 2: Medium-term 8-30d | conf≥80% | edge≥20%        ║
║  Tier 3: Long-term  31-180d| conf≥90% | edge≥25%        ║
║                                                          ║
║  Fixes vs v12:                                           ║
║  • Screener diversity cap — max 2 per category in top N  ║
║  • Opus JSON parse hardened — no more empty-value errors ║
║  • News monitor layer fully integrated                   ║
║  • Health added as tracked category                      ║
║                                                          ║
║  SETUP:  pip install anthropic requests ddgs feedparser  ║
║  RUN:    python claudebot.py --single-scan               ║
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
    from ddgs import DDGS
    DDG_AVAILABLE = True
except ImportError:
    DDG_AVAILABLE = False

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
FINNHUB_API_KEY        = os.environ.get("FINNHUB_API_KEY", "")
VISUAL_CROSSING_API_KEY = os.environ.get("VISUAL_CROSSING_API_KEY", "")

SCREENER_MODEL     = "claude-haiku-4-5-20251001"
ANALYST_MODEL      = "claude-opus-4-6"
PAPER_TRADING      = True
STARTING_BANKROLL  = 1000.00
DAILY_LOSS_LIMIT   = 150.00
SCAN_INTERVAL_MINS = 180
LOG_FILE           = "claudebot_v3_log.json"

# Reflection loop — trade journal for graphify knowledge graph
REFLECTIONS_DIR    = "trade_reflections_v3"
GRAPH_REPORT_FILE  = "graphify-out/GRAPH_REPORT.md"

REASSESS_INTERVAL_DAYS = 3   # reassess open T2/T3 trades every N days
MIN_DAYS_REMAINING     = 5   # skip reassessment if trade closes within N days

BLOCKED_CATEGORIES = {
    "conflict",      # geopolitical military — high variance
    "geopolitics",   # same
}
# V3: sports/crypto/politics/economics are DATA COLLECTED
# Near-resolution sports filter applied in fetch_markets_for_tier
SPORTS_NEAR_RESOLUTION_ONLY = True   # re-enabled but <6h + YES>=88% only
SPORTS_MIN_YES = 88                  # only near-certain outcomes
SPORTS_MAX_CID = 0.25                # under 6h to close

TIERS = {
    1: {
        "name":           "Short-term",
        "label":          "T1",
        "min_hold_hours": 2,
        "max_hold_days":  7,
        "min_confidence": 75,
        "min_edge_pct":   15,
        "max_positions":  6,
        "fixed_pct":      None,
        "kelly": [
            {"min_conf": 90, "fraction": 1.0, "max_pct": 15.0},
            {"min_conf": 75, "fraction": 0.5, "max_pct": 10.0},
        ],
        "short_disc_1d":  0.65,
        "short_disc_2d":  0.80,
        "screener_top_n": 15,
        "screener_max_per_cat": 2,   # diversity cap in screener output
    },
    2: {
        "name":           "Medium-term",
        "label":          "T2",
        "min_hold_days":  8,
        "max_hold_days":  30,
        "min_confidence": 80,
        "min_edge_pct":   20,
        "max_positions":  3,
        "fixed_pct":      None,
        "kelly": [
            {"min_conf": 90, "fraction": 0.5, "max_pct": 8.0},
            {"min_conf": 80, "fraction": 0.25, "max_pct": 5.0},
        ],
        "time_discount":  0.75,
        "screener_top_n": 12,
        "screener_max_per_cat": 2,
    },

}

MAX_PER_CATEGORY = 1

NEWS_FEEDS = [
    ("Reuters World",  "https://feeds.reuters.com/reuters/worldNews"),
    ("BBC News",       "https://feeds.bbci.co.uk/news/rss.xml"),
    ("Al Jazeera",     "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Sky News",       "https://feeds.skynews.com/feeds/rss/world.xml"),
    ("CDC",            "https://tools.cdc.gov/api/v2/resources/media/404952.rss"),
    ("NOAA Alerts",    "https://alerts.weather.gov/cap/us.php?x=1"),
    ("MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
]
NEWS_LOOKBACK_HOURS = 6


# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─────────────────────────────────────────────────────────
#  CATEGORY HELPER  (defined early — used everywhere)
# ─────────────────────────────────────────────────────────

def get_category(question):
    q = question.lower()
    if any(k in q for k in ["temperature", "weather", "rain", "snow", "°c", "°f",
                              "celsius", "fahrenheit", "precipitation", "humid"]):
        return "weather"
    if any(k in q for k in ["measles", "outbreak", "disease", "pandemic", "virus",
                              "covid", "flu", "mpox", "cases", "cdc", "who",
                              "vaccination", "epidemic"]):
        return "health"
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "crypto",
                              "solana", "bnb", "xrp", "defi", "sol"]):
        return "crypto"
    if any(k in q for k in ["openai", "anthropic", "gpt", "gemini", "artificial intelligence",
                                "nvidia", "spacex", "starship", "neuralink", "waymo"]):
        return "tech"
    if any(k in q for k in ["oscar", "grammy", "emmy", "academy award",
                                "box office", "spotify", "tiktok", "youtube",
                                "taylor swift", "kanye", "beyonce", "marvel", "disney"]):
        return "culture"
    if any(k in q for k in ["nba", "nfl", "mlb", "nhl", "soccer", "football",
                              "tennis", "golf", "points", "goals", "score", "match",
                              "game", "fc ", " united", "spread", "o/u", "rebounds",
                              "assists", "esport", "valorant", "counter-strike", "dota",
                              "leverkusen", "barcelona", "atletico", "flyers", "capitals",
                              "lakers", "celtics", "bucks", "nets", "knicks", "bulls",
                              "heat", "hawks", "sixers", "suns", "nuggets", "warriors",
                              "west brom", "wrexham", "jokic",
                              "iceho", "rockets", "ahl:", "lol:", "fluxo", "leviatan",
                              "xspark", "xcrew", "prodigy", "rune eaters", "atputies",
                              "sinners", "jijiehao", "almeria", "kalieva", "urhobo",
                              "svrcina", "berrettini", "shinden", "melser", "real madrid",
                              "chengdu", "qingdao", "monchengladbach", "real sociedad"]):
        return "sports"
    if any(k in q for k in ["president", "election", "senate", "congress", "vote",
                              "government", "minister", "party", "trump", "biden",
                              "democrat", "republican", "musk", "tweets", "elon",
                              "policy", "tariff", "doge", "approval", "rogan",
                              "dana white", "ufc"]):
        return "politics"
    if any(k in q for k in ["wti", "crude oil", "brent", "oil price",
                              "natural gas", "gold price", "silver price",
                              "commodity", "barrel"]):
        return "commodities"
    if any(k in q for k in ["amazon", "amzn", "tesla", "tsla", "nvidia", "nvda",
                              "apple", "aapl", "microsoft", "msft", "google", "googl",
                              "meta", "netflix", "nflx", "palantir", "pltr",
                              "intel", "intc", "shopify", "shop", "robinhood", "hood",
                              "american express", "axp", "lockheed", "lmt",
                              "general dynamics", "honeywell", "hon", "moody", "mco",
                              "cbre", "procter", "american airlines", "aal",
                              "texas instruments", "txn", "at&t", "coursera", "cour",
                              "ss&c", "ssnc", "dow inc", "united bankshares", "ubsi",
                              "stock price", "close above", "close below",
                              "quarterly earnings", "beat earnings", "beat quarterly",
                              "finish week", "week of"]):
        return "stocks"
    if any(k in q for k in ["fed", "rate", "inflation", "gdp", "recession",
                              "unemployment", "economy", "s&p", "nasdaq", "dow",
                              "market cap", "ecb", "interest", "s&p 500", "spy"]):
        return "economics"
    if any(k in q for k in ["war", "military", "attack", "ceasefire", "hezbollah",
                              "ukraine", "russia", "israel", "hamas", "conflict",
                              "kyiv", "kostyantynivka", "borova", "troops", "nato",
                              "iran", "china", "taiwan", "north korea", "missile",
                              "strike", "yemen", "houthi"]):
        return "geopolitics"
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
        else:
            log(f"⚠️  Telegram error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"⚠️  Telegram failed: {e}")


def tier_badge(tier_num):
    return {1: "⚡", 2: "📅", 3: "🎯"}.get(tier_num, "📊")


def telegram_new_trade(trade, state):
    kelly_pct  = trade.get("kelly_tier", "").split("(")[-1].replace(")", "").strip()
    profit_pct = round((trade["potential_return"] / trade["stake"] - 1) * 100, 1) if trade["stake"] else 0
    pos_emoji  = "✅" if trade["position"] == "YES" else "🔴"
    edge       = abs(trade.get("true_prob", 0) - trade.get("market_prob", 0))
    roi        = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    closed_t   = [t for t in state["trades"] if t["status"] == "closed"]
    won_ct     = sum(1 for t in closed_t if t.get("won"))
    lost_ct    = len(closed_t) - won_ct
    t_num      = trade.get("tier", 1)
    badge      = tier_badge(t_num)
    tier_label = TIERS[t_num]["name"]
    news_flag  = "📰 <i>News-triggered</i>\n" if trade.get("news_triggered") else ""
    slug       = trade.get("market_slug", "")
    link_line  = (
        f"\n\n🔗 <a href=\"https://polymarket.com/event/{slug}\">Trade on Polymarket</a>"
        if slug else ""
    )

    public_msg = (
        f"🤖 <b>CLAUDEBOT SIGNAL</b>  {badge} <i>{tier_label}</i>\n"
        f"{'─' * 30}\n"
        f"{news_flag}"
        f"<b>{trade['market']}</b>\n\n"
        f"{pos_emoji} <b>BUY {trade['position']}</b>\n\n"
        f"📌 Entry: <b>{trade['entry_price']}¢</b>\n"
        f"🎯 Confidence: <b>{trade['confidence']}%</b>\n"
        f"📐 Sizing: <b>{kelly_pct if kelly_pct else '3% fixed'}</b>\n"
        f"💹 Potential profit: <b>+{profit_pct}%</b>\n"
        f"⏰ Closing: <b>{trade['closes'][:10]}</b>\n\n"
        f"{'─' * 30}\n"
        f"🔍 <b>Reasoning</b>\n"
        f"<i>{trade.get('research_summary', '')}</i>\n\n"
        f"⚠️ <b>Bear case</b>\n"
        f"<i>{trade.get('bear_case', '')}</i>"
        f"{link_line}"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

    if TELEGRAM_PERSONAL_ID:
        private_msg = (
            f"🤖 <b>CLAUDEBOT — PRIVATE</b>  {badge} {tier_label}\n"
            f"{'─' * 30}\n"
            f"{news_flag}"
            f"<b>{trade['market']}</b>\n\n"
            f"{pos_emoji} <b>BUY {trade['position']}</b>\n\n"
            f"💰 Entry: <b>{trade['entry_price']}¢</b>  |  True prob: <b>{trade['true_prob']}%</b>\n"
            f"📈 Edge: <b>+{edge}%</b>  |  Confidence: <b>{trade['confidence']}%</b>\n"
            f"📐 {trade.get('kelly_tier', '3% fixed')}\n"
            f"💵 Stake: <b>${trade['stake']:.2f}</b>  →  Win: <b>${trade['potential_return']:.2f}</b>\n"
            f"💹 Profit if wins: <b>+{profit_pct}%</b>\n"
            f"⏰ Closes: <b>{trade['closes'][:10]}</b> ({trade['closes_in_days']:.0f}d)\n\n"
            f"🔍 <i>{trade.get('research_summary', '')}</i>\n\n"
            f"⚠️ Bear: <i>{trade.get('bear_case', '')}</i>\n"
            f"{'─' * 30}\n"
            f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
            f"📊 Record: <b>{won_ct}W / {lost_ct}L</b>"
            f"{link_line}"
        )
        send_telegram(private_msg, TELEGRAM_PERSONAL_ID)


def telegram_trade_resolved(trade, state):
    won     = trade.get("won", False)
    emoji   = "✅" if won else "❌"
    result  = "WON" if won else "LOST"
    pnl     = trade.get("realized_pnl", 0)
    # Public: show % return on THIS trade only — no bankroll, no dollar P&L
    trade_pct = round(pnl / trade["stake"] * 100, 1) if trade.get("stake") else 0
    pct_str   = f"+{trade_pct}%" if trade_pct >= 0 else f"{trade_pct}%"
    roi     = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    closed  = [t for t in state["trades"] if t["status"] == "closed"]
    won_ct  = sum(1 for t in closed if t.get("won"))
    lost_ct = len(closed) - won_ct

    public_msg = (
        f"{emoji} <b>TRADE RESOLVED — {result}</b>\n"
        f"{'─' * 30}\n"
        f"<b>{trade['market']}</b>\n"
        f"Position: <b>{trade['position']} @ {trade['entry_price']}¢</b>\n"
        f"Return: <b>{pct_str} on this trade</b>\n"
        f"{'─' * 30}\n"
        f"📊 Record: <b>{won_ct}W / {lost_ct}L</b>"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

    if TELEGRAM_PERSONAL_ID:
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        wr = (won_ct / len(closed) * 100) if closed else 0
        private_msg = (
            f"{emoji} <b>TRADE RESOLVED — {result}</b>\n"
            f"{'─' * 30}\n"
            f"<b>{trade['market']}</b>\n"
            f"Position: <b>{trade['position']} @ {trade['entry_price']}¢</b>\n\n"
            f"💰 P&L: <b>{pnl_str}</b>\n"
            f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
            f"{'─' * 30}\n"
            f"📊 Record: <b>{won_ct}W / {lost_ct}L — {wr:.0f}% win rate</b>"
        )
        send_telegram(private_msg, TELEGRAM_PERSONAL_ID)


def telegram_daily_summary(state):
    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0
    roi      = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100

    pos_public  = ""
    pos_private = ""
    for t in open_t:
        close_dt   = parse_utc(t.get("closes", ""))
        closes_str = close_dt.strftime("%b %d") if close_dt else "?"
        badge      = tier_badge(t.get("tier", 1))
        pos_public  += f"  {badge} {t['position']} | {closes_str} | {t['market'][:40]}\n"
        pos_private += f"  {badge} {t['position']} | ${t['stake']:.2f} | {closes_str} | {t['market'][:40]}\n"

    # Daily summary goes to personal only — no bankroll/P&L on public channel
    if TELEGRAM_PERSONAL_ID:
        private_msg = (
            f"📅 <b>CLAUDEBOT DAILY — PRIVATE</b>\n"
            f"{'─' * 30}\n"
            f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b>\n"
            f"📈 ROI: <b>{roi:+.1f}%</b>  |  P&L: <b>${realized:+.2f}</b>\n"
            f"📊 Record: <b>{len(won_t)}W / {len(lost_t)}L — {win_rate:.0f}%</b>\n"
            f"🔄 Scans: <b>{state.get('scan_count', 0)}</b>\n"
            f"{'─' * 30}\n"
            f"📋 Open ({len(open_t)}):\n"
            + (pos_private if pos_private else "  None\n") +
            f"{'─' * 30}\n"
            f"⚡T1 short | 📅T2 medium | 🎯T3 long"
        )
        send_telegram(private_msg, TELEGRAM_PERSONAL_ID)
        send_telegram(f"📱 <a href=\"https://jamesgrimm1.github.io/claudebot/mobile.html\">Open mobile dashboard</a>", TELEGRAM_PERSONAL_ID)


def should_send_daily_summary(state):
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily_summary", "") == today:
        return False
    if now.hour == 9:
        state["last_daily_summary"] = today
        return True
    return False


# ─────────────────────────────────────────────────────────
#  SCAN SCHEDULING
# ─────────────────────────────────────────────────────────

def should_run_tier2(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.get("last_tier2_scan", "") != today


# ─────────────────────────────────────────────────────────
#  NEWS MONITOR — Step 0
# ─────────────────────────────────────────────────────────

def scan_news_feeds():
    if not FEEDPARSER_AVAILABLE:
        log("⚠️  feedparser not installed — skipping news monitor")
        return []

    headlines = []
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)

    for name, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                published = entry.get("published_parsed")
                if published:
                    try:
                        pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                        if pub_dt < cutoff:
                            continue
                    except Exception:
                        pass
                headlines.append({
                    "title":   entry.get("title", "")[:150],
                    "summary": entry.get("summary", "")[:200],
                    "source":  name,
                })
        except Exception as e:
            log(f"  ⚠️  Feed [{name}]: {e}")

    return headlines


def haiku_flag_news(headlines, state):
    if not headlines:
        return []

    client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    open_markets = [t["market"] for t in state["trades"] if t["status"] == "open"]

    headline_txt = "\n".join(
        f"[{h['source']}] {h['title']} — {h['summary'][:100]}"
        for h in headlines[:60]
    )

    prompt = (
        f"Monitor breaking news for prediction market trading edge.\n"
        f"Today: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n\n"
        f"HEADLINES (last {NEWS_LOOKBACK_HOURS}h):\n{headline_txt}\n\n"
        f"ALREADY OPEN (skip these):\n"
        + ("\n".join(f"  - {m[:70]}" for m in open_markets) or "  None") + "\n\n"
        f"Flag headlines creating GENUINE POLYMARKET EDGE:\n"
        f"  ✅ Confirmed fact not yet priced in (withdrawal, resignation, confirmed event)\n"
        f"  ✅ Health data release with specific numbers (CDC, WHO)\n"
        f"  ✅ Breaking geopolitical event changing a binary outcome\n"
        f"  ✅ Economic data release with clear directional signal\n"
        f"  ✅ Major weather emergency for a tracked city\n"
        f"  ❌ Sports scores, general commentary, already-open markets\n\n"
        f"Return ONLY JSON array (empty if nothing actionable):\n"
        f'[{{"headline":"title","reason":"why edge exists",'
        f'"search_query":"query to find matching market",'
        f'"category":"geopolitics|weather|economics|politics|health|other"}}]'
    )

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw   = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[[\s\S]*\]', raw)
        flags = json.loads(match.group(0) if match else "[]")

        if flags:
            log(f"📰 Flagged {len(flags)} news edge(s):")
            for f in flags:
                log(f"   🚨 {f.get('headline','')[:70]}")
        else:
            log(f"📰 No actionable news in last {NEWS_LOOKBACK_HOURS}h")

        return flags
    except Exception as e:
        log(f"⚠️  News flag error: {e}")
        return []


def find_markets_for_news(flags, all_markets):
    if not flags:
        return [], all_markets

    priority_ids = set()
    for flag in flags:
        query    = flag.get("search_query", "").lower()
        category = flag.get("category", "")
        words    = [w for w in query.split() if len(w) > 4]

        for m in all_markets:
            q   = m["question"].lower()
            cat = m.get("category") or get_category(m["question"])
            overlap = sum(1 for w in words if w in q)
            if overlap >= 2 or (category and cat == category and overlap >= 1):
                priority_ids.add(m["id"])
                log(f"   🎯 News match: {m['question'][:60]}")

    priority = [m for m in all_markets if m["id"] in priority_ids]
    normal   = [m for m in all_markets if m["id"] not in priority_ids]

    if priority:
        log(f"📰 {len(priority)} priority market(s) bypassing screener")

    return priority, normal


# ─────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            s = json.load(f)
        log(f"📂 Loaded — {len(s.get('trades', []))} trades | bankroll ${s.get('bankroll', STARTING_BANKROLL):.2f}")
        return s
    log("📂 No log found — starting fresh")
    return {
        "bankroll":           STARTING_BANKROLL,
        "trades":             [],
        "daily_loss":         0.0,
        "daily_reset":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scan_count":         0,
        "started":            datetime.now(timezone.utc).isoformat(),
        "last_daily_summary": "",
        "last_tier2_scan":    "",
        "last_reassessment":  "",
    }


def save_state(state):
    with open(LOG_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log(f"💾 Saved — bankroll ${state['bankroll']:.2f} | {len(state['trades'])} trades")


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

# ─────────────────────────────────────────────────────────
#  REFLECTION LOOP — trade journal + graphify knowledge graph
# ─────────────────────────────────────────────────────────

def write_trade_reflection(trade, state):
    """
    Write a markdown reflection file for every resolved trade.
    These files accumulate in trade_reflections/ and are fed into
    graphify to build a queryable knowledge graph of what works.
    """
    os.makedirs(REFLECTIONS_DIR, exist_ok=True)

    trade_id  = trade.get("id", "unknown")
    won       = trade.get("won", False)
    outcome   = "WON" if won else "LOST"
    pnl       = trade.get("realized_pnl", 0)
    tier      = trade.get("tier", 1)
    category  = trade.get("category", "other")
    position  = trade.get("position", "?")
    entry     = trade.get("entry_price", "?")
    stake     = trade.get("stake", 0)
    conf      = trade.get("confidence", "?")
    market_p  = trade.get("market_prob", "?")
    true_p    = trade.get("true_prob", "?")
    edge      = trade.get("edge_pct", "?")
    market    = trade.get("market", "")
    research  = trade.get("research_summary", "")
    bear      = trade.get("bear_case", "")
    key_facts = trade.get("key_factors", [])
    news_trig = trade.get("news_triggered", False)
    placed    = trade.get("placed_at", "")[:10]
    resolved  = trade.get("resolved_at", "")[:10]
    closes    = trade.get("closes", "")[:10]
    cid       = trade.get("closes_in_days", "?")
    kelly_t   = trade.get("kelly_tier", "")

    # Calculate hold duration
    try:
        p_dt = datetime.fromisoformat(trade["placed_at"].replace("Z", "+00:00"))
        r_dt = datetime.fromisoformat(trade["resolved_at"].replace("Z", "+00:00"))
        hold_h = (r_dt - p_dt).total_seconds() / 3600
        hold_str = f"{hold_h:.1f} hours"
    except Exception:
        hold_str = "unknown"

    # Win/loss diagnosis
    if won:
        diagnosis = "THESIS CORRECT"
        if conf != "?" and isinstance(conf, (int, float)):
            if conf >= 85:
                diagnosis = "HIGH CONFIDENCE — VALIDATED"
            elif conf >= 75:
                diagnosis = "MODERATE CONFIDENCE — VALIDATED"
        if news_trig:
            diagnosis += " · NEWS-TRIGGERED EDGE"
    else:
        diagnosis = "THESIS WRONG"
        if bear:
            diagnosis = f"BEAR CASE MATERIALISED: {bear[:80]}"

    roi_pct = (pnl / stake * 100) if stake else 0

    md = f"""# Trade Reflection — {trade_id}

## Outcome
- **Result:** {outcome} ${pnl:+.2f} ({roi_pct:+.1f}% ROI)
- **Diagnosis:** {diagnosis}
- **Market:** {market}
- **Category:** {category}
- **Position:** {position} @ {entry}¢
- **Tier:** T{tier} | **Sizing:** {kelly_t}
- **Stake:** ${stake:.2f}

## Timing
- **Placed:** {placed}
- **Resolved:** {resolved}
- **Closes:** {closes} ({cid}d remaining at entry)
- **Hold duration:** {hold_str}

## Pricing & Edge
- **Market implied:** {market_p}% YES
- **Opus true prob:** {true_p}% YES
- **Confidence:** {conf}%
- **Edge:** {edge}%
- **News triggered:** {news_trig}

## Research
{research}

## Key Factors
{chr(10).join(f'- {f}' for f in key_facts) if key_facts else '- None recorded'}

## Bear Case
{bear if bear else 'None recorded'}

## Bankroll After
${state['bankroll']:.2f}

## Patterns
- category: {category}
- outcome: {outcome}
- hold_hours: {hold_str}
- tier: T{tier}
- confidence_band: {f'{(conf//10)*10}-{(conf//10)*10+9}%' if isinstance(conf, (int,float)) else 'unknown'}
- position: {position}
- news_triggered: {news_trig}
- entry_bracket: {'45-52c' if isinstance(entry,(int,float)) and entry<=52 else '53-62c' if isinstance(entry,(int,float)) else 'unknown'}
"""

    fname = f"{REFLECTIONS_DIR}/{trade_id}_{category}_{outcome}.md"
    try:
        with open(fname, "w") as f:
            f.write(md)
        log(f"  📝 Reflection saved: {fname.split('/')[-1]}")
    except Exception as e:
        log(f"  ⚠️  Reflection write failed: {e}")


def load_graph_context():
    """
    Load the graphify GRAPH_REPORT.md if it exists.
    Returns a string to inject into Opus research context.
    Graphify builds this from trade_reflections/ — it captures
    what patterns, categories, and confidence bands are working.
    """
    if not os.path.exists(GRAPH_REPORT_FILE):
        return ""
    try:
        with open(GRAPH_REPORT_FILE, "r") as f:
            report = f.read()
        # Trim to avoid token overload — first 2000 chars covers god nodes + patterns
        trimmed = report[:2500].strip()
        log(f"  🧠 Graph context loaded ({len(trimmed)} chars)")
        return (
            "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "KNOWLEDGE GRAPH — PATTERNS FROM TRADE HISTORY\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{trimmed}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Use these patterns to inform your analysis. "
            "If the graph shows a category or pattern has a poor track record, "
            "weight that into your confidence and edge estimates.\n"
        )
    except Exception as e:
        log(f"  ⚠️  Graph context load failed: {e}")
        return ""



# ─────────────────────────────────────────────────────────
#  ECONOMIC CALENDAR — Finnhub
# ─────────────────────────────────────────────────────────

# Events that should block or flag economics/central bank trades
HIGH_IMPACT_BLOCK_KEYWORDS = [
    "rate decision", "interest rate", "fed", "fomc", "ecb", "boe", "boj",
    "rba", "rbnz", "monetary policy", "central bank",
    "nonfarm", "non-farm", "nfp", "employment", "unemployment",
    "cpi", "inflation", "pce", "gdp", "retail sales",
    "earnings", "quarterly results",
]

_eco_calendar_cache = {"data": None, "fetched": None}

def get_economic_calendar(days_ahead=7):
    """
    Fetch upcoming high-impact economic events from Finnhub.
    Cached per scan — only fetches once per workflow run.
    Returns list of events: {date, time, country, event, impact, actual, estimate, prev}
    """
    if not FINNHUB_API_KEY:
        return []

    # Use cache if fetched in last 55 mins
    now = datetime.now(timezone.utc)
    if _eco_calendar_cache["data"] is not None and _eco_calendar_cache["fetched"]:
        age = (now - _eco_calendar_cache["fetched"]).total_seconds()
        if age < 3300:
            return _eco_calendar_cache["data"]

    try:
        date_from = now.strftime("%Y-%m-%d")
        date_to   = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": date_from, "to": date_to, "token": FINNHUB_API_KEY},
            timeout=8
        )
        if r.status_code != 200:
            log(f"  ⚠️  Finnhub calendar: HTTP {r.status_code}")
            return []

        raw = r.json().get("economicCalendar", [])

        # Filter to high impact only
        events = []
        for e in raw:
            if e.get("impact") in ("high", "3"):
                events.append({
                    "date":     e.get("time", "")[:10],
                    "time":     e.get("time", "")[11:16],
                    "country":  e.get("country", ""),
                    "event":    e.get("event", ""),
                    "impact":   "HIGH",
                    "actual":   e.get("actual", ""),
                    "estimate": e.get("estimate", ""),
                    "prev":     e.get("prev", ""),
                })

        _eco_calendar_cache["data"]    = events
        _eco_calendar_cache["fetched"] = now
        log(f"  📅 Economic calendar: {len(events)} high-impact events in next {days_ahead}d")
        return events

    except Exception as e:
        log(f"  ⚠️  Finnhub calendar failed: {e}")
        return []


def check_calendar_risk(market_question, closes_in_days):
    """
    Check if a market's resolution window overlaps with high-impact
    economic events. Returns a warning string for Opus, or None.

    Used to flag markets where a known scheduled release could flip
    the outcome — ECB rate decision, FOMC, NFP, earnings etc.
    """
    events = get_economic_calendar(days_ahead=max(7, int(closes_in_days) + 2))
    if not events:
        return None

    q_lower = market_question.lower()

    # Find relevant events — ones that could affect this specific market
    relevant = []
    for e in events:
        ev_lower = e["event"].lower()
        country  = e["country"].upper()

        # Check if this event type could flip the market outcome
        is_macro = any(k in ev_lower for k in HIGH_IMPACT_BLOCK_KEYWORDS)

        # Check country relevance
        country_relevant = (
            ("usd" in q_lower or "fed" in q_lower or "us" in q_lower or "dollar" in q_lower) and country == "US"
            or ("eur" in q_lower or "ecb" in q_lower or "euro" in q_lower) and country in ("EU", "DE", "FR")
            or ("gbp" in q_lower or "boe" in q_lower or "uk" in q_lower) and country == "GB"
            or ("jpy" in q_lower or "boj" in q_lower or "japan" in q_lower) and country == "JP"
            or ("oil" in q_lower or "wti" in q_lower or "crude" in q_lower) and is_macro
            or ("stock" in q_lower or "spx" in q_lower or "nasdaq" in q_lower or "s&p" in q_lower) and country == "US" and is_macro
            or ("earn" in q_lower or "eps" in q_lower or "revenue" in q_lower or "quarterly" in q_lower)
        )

        if is_macro and country_relevant:
            relevant.append(e)

    if not relevant:
        return None

    warning_lines = ["⚠️ ECONOMIC CALENDAR RISK — high-impact events in resolution window:"]
    for e in relevant[:4]:
        warning_lines.append(
            f"  {e['date']} {e['time']} UTC | {e['country']} | {e['event']}"
            + (f" | est: {e['estimate']}" if e['estimate'] else "")
            + (f" | prev: {e['prev']}" if e['prev'] else "")
        )
    warning_lines.append(
        "These scheduled releases could significantly move the market before resolution. "
        "Require extra edge and confidence before entering. "
        "For central bank decisions — only trade if you have CONFIRMED forward guidance, "
        "not analyst forecasts."
    )
    return "\n".join(warning_lines)


# ─────────────────────────────────────────────────────────
#  WEATHER FORECAST — Visual Crossing
# ─────────────────────────────────────────────────────────

# City name normalisation — Polymarket uses various formats
CITY_ALIASES = {
    "new york city": "New York,US",
    "nyc":           "New York,US",
    "los angeles":   "Los Angeles,US",
    "san francisco": "San Francisco,US",
    "buenos aires":  "Buenos Aires,Argentina",
    "mexico city":   "Mexico City,Mexico",
    "hong kong":     "Hong Kong",
    "kuala lumpur":  "Kuala Lumpur,Malaysia",
}

_weather_cache = {}   # city+date → result

def get_weather_forecast(market_question, target_date_str=None):
    """
    Fetch the actual forecast for the city and date mentioned in a
    weather market question from Visual Crossing.

    Returns a formatted string with tempmax, tempmin, precip, conditions
    for the target date — ready to inject into the Opus research brief.

    target_date_str: 'YYYY-MM-DD' — if None we parse from the market question
    """
    if not VISUAL_CROSSING_API_KEY:
        return None

    q = market_question.lower()

    # ── Extract city from question ────────────────────────
    city = None
    for alias, canonical in CITY_ALIASES.items():
        if alias in q:
            city = canonical
            break

    if not city:
        # Try to extract "in <City>" pattern
        m = re.search(r'\bin ([A-Z][a-zA-Z\s]+?)(?:\s+be|\s+have|\s+reach|\s+exceed|\s+on|\?)', market_question)
        if m:
            city = m.group(1).strip()

    if not city:
        return None

    # ── Extract target date ───────────────────────────────
    if not target_date_str:
        m = re.search(r'(\w+ \d{1,2},?\s*\d{4}|\d{4}-\d{2}-\d{2}|April \d+|on (\w+ \d+))', market_question)
        if m:
            try:
                from datetime import datetime as _dt
                raw = m.group(0).replace("on ", "").strip()
                for fmt in ("%B %d %Y", "%B %d, %Y", "%Y-%m-%d", "%B %d"):
                    try:
                        parsed = _dt.strptime(raw, fmt)
                        if parsed.year < 2000:
                            parsed = parsed.replace(year=datetime.now(timezone.utc).year)
                        target_date_str = parsed.strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

    if not target_date_str:
        # Default to tomorrow
        target_date_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    cache_key = f"{city}_{target_date_str}"
    if cache_key in _weather_cache:
        return _weather_cache[cache_key]

    try:
        url = (
            f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services"
            f"/timeline/{requests.utils.quote(city)}/{target_date_str}"
            f"?unitGroup=metric&include=days&key={VISUAL_CROSSING_API_KEY}&contentType=json"
        )
        r = requests.get(url, timeout=8)

        if r.status_code != 200:
            log(f"     ⚠️  Visual Crossing HTTP {r.status_code} for {city} {target_date_str}")
            return None

        data  = r.json()
        days  = data.get("days", [])
        if not days:
            return None

        day       = days[0]
        tempmax   = day.get("tempmax")
        tempmin   = day.get("tempmin")
        temp      = day.get("temp")
        precip    = day.get("precip", 0)
        precipprob = day.get("precipprob", 0)
        conditions = day.get("conditions", "")
        description = day.get("description", "")
        resolved_addr = data.get("resolvedAddress", city)

        # Convert to Fahrenheit if question mentions °F
        def to_f(c):
            return round(c * 9/5 + 32, 1) if c is not None else None

        has_f = "°f" in q or "fahrenheit" in q
        if has_f:
            unit      = "°F"
            hi        = to_f(tempmax)
            lo        = to_f(tempmin)
            avg       = to_f(temp)
        else:
            unit      = "°C"
            hi        = round(tempmax, 1) if tempmax is not None else None
            lo        = round(tempmin, 1) if tempmin is not None else None
            avg       = round(temp, 1) if temp is not None else None

        log(f"     🌤  Visual Crossing {city} {target_date_str}: high={hi}{unit} low={lo}{unit}")

        result = (
            f"\nLIVE WEATHER FORECAST (Visual Crossing, fetched now):\n"
            f"  Location:    {resolved_addr}\n"
            f"  Date:        {target_date_str}\n"
            f"  High:        {hi}{unit}\n"
            f"  Low:         {lo}{unit}\n"
            f"  Avg:         {avg}{unit}\n"
            f"  Conditions:  {conditions}\n"
            f"  Precip prob: {precipprob}%  ({precip}mm)\n"
            f"  Summary:     {description}\n"
            f"Use these ACTUAL forecast values when evaluating temperature threshold markets.\n"
        )

        _weather_cache[cache_key] = result
        return result

    except Exception as e:
        log(f"     ⚠️  Visual Crossing failed: {e}")
        return None


def _settle(trade, won, state):
    # Guard: don't re-settle already closed trades
    if trade.get("status") == "closed":
        return

    trade["status"]      = "closed"
    trade["won"]         = won
    trade["resolved_at"] = datetime.now(timezone.utc).isoformat()

    if won:
        payout               = round(trade["stake"] * 100 / trade["entry_price"], 2)
        trade["realized_pnl"] = round(payout - trade["stake"], 2)
        state["bankroll"]    = round(state["bankroll"] + payout, 2)
        log(f"  ✅ WON   +${trade['realized_pnl']:.2f}  {trade['market'][:55]}")
    else:
        trade["realized_pnl"] = round(-trade["stake"], 2)
        state["daily_loss"]  = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST  -${trade['stake']:.2f}  {trade['market'][:55]}")

    log(f"     Bankroll now: ${state['bankroll']:.2f}")
    write_trade_reflection(trade, state)
    telegram_trade_resolved(trade, state)


def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state
    log(f"🔍 Checking {len(open_trades)} open position(s)...")
    now = datetime.now(timezone.utc)

    for trade in open_trades:
        market_id = trade.get("market_id", "")
        close_dt  = parse_utc(trade.get("closes"))

        # Don't attempt resolution before the market's close time
        if close_dt and now < close_dt:
            continue

        hours_past = (now - close_dt).total_seconds() / 3600 if close_dt else 0

        if not market_id or market_id.startswith("d0"):
            if close_dt and hours_past > 1:
                import random
                _settle(trade, random.random() > 0.5, state)
            continue

        resolved = False
        for url in [
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            f"https://gamma-api.polymarket.com/markets?id={market_id}",
        ]:
            if resolved:
                break
            try:
                r = requests.get(url, timeout=12)
                if r.status_code != 200:
                    continue
                raw = r.json()
                mkt = raw[0] if isinstance(raw, list) and raw else raw

                active      = mkt.get("active", True)
                closed_flag = mkt.get("closed", False)

                # Time-based override: if Gamma still shows active but market
                # is >2h past close, ignore the flag and check prices directly.
                # Gamma flags lag significantly — prices snap to resolution fast.
                gamma_lagging = active and not closed_flag and hours_past > 2

                if active and not closed_flag and not gamma_lagging:
                    continue  # genuinely still live

                prices_raw = mkt.get("outcomePrices")
                if not prices_raw:
                    continue

                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                prices = [float(p) for p in prices]

                if len(prices) >= 2:
                    yes_price = prices[0]
                    no_price  = prices[1]

                    if yes_price >= 0.99 or no_price >= 0.99:
                        if len(prices) > 2:
                            won = (yes_price >= 0.99) if trade["position"] == "YES" else (yes_price < 0.01)
                        else:
                            won = (yes_price >= 0.99) if trade["position"] == "YES" else (no_price >= 0.99)
                        if gamma_lagging:
                            log(f"  ⚡ Gamma lag override — settling via prices: {trade['market'][:50]}")
                        _settle(trade, won, state)
                        resolved = True
                    else:
                        if gamma_lagging:
                            log(f"  ⏳ {trade['market'][:50]} — {hours_past:.0f}h past close, prices not snapped ({yes_price:.2f}/{no_price:.2f})")
                        else:
                            log(f"  ⏳ {trade['market'][:50]} — closed flag set but prices not snapped yet")

            except Exception as e:
                log(f"  ⚠️  Resolve attempt failed for {market_id}: {e}")

        if not resolved and trade["status"] == "open" and close_dt:
            if hours_past > 24:
                log(f"  ⚠️  {trade['market'][:55]} — {hours_past:.0f}h past close, needs manual check")

    return state

def fetch_markets_for_tier(tier_num):
    tcfg     = TIERS[tier_num]
    min_days = tcfg.get("min_hold_days", tcfg.get("min_hold_hours", 2) / 24)
    max_days = tcfg["max_hold_days"]

    try:
        # Pass 1: top 500 by volume (established markets)
        r1 = requests.get(
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=500&order=volume&ascending=false",
            timeout=12
        )
        r1.raise_for_status()
        raw = r1.json()
    except Exception as e:
        log(f"⚠️  Polymarket unavailable ({e})")
        return []

    try:
        # Pass 2: 100 newest markets — catches recently listed before arb compresses
        r2 = requests.get(
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=100&order=startDate&ascending=false",
            timeout=10
        )
        if r2.status_code == 200:
            new_markets = r2.json()
            existing_ids = {m.get("id") for m in raw}
            raw.extend(m for m in new_markets if m.get("id") not in existing_ids)
            log(f"  + {len(new_markets)} newest markets fetched (deduped into pool)")
    except Exception:
        pass  # second pass is best-effort

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
        if cid < min_days or cid > max_days:
            skipped += 1
            continue
        try:
            prices = json.loads(m["outcomePrices"])
            yes    = round(float(prices[0]) * 100)
        except Exception:
            continue
        if yes >= 95 or yes <= 5:
            continue
        cat = get_category(m["question"])
        if cat in BLOCKED_CATEGORIES:
            continue
        # V3: sports allowed ONLY near resolution with near-certain price
        if cat == "sports" and SPORTS_NEAR_RESOLUTION_ONLY:
            if cid > SPORTS_MAX_CID or yes < SPORTS_MIN_YES:
                skipped += 1
                continue
            # Block exact-score and O/U sports — too much variance
            q_lower_check = m["question"].lower()
            if any(k in q_lower_check for k in ["o/u", "over/under", "exact score",
                                                   "total goals", "total points",
                                                   "how many", "what will the score"]):
                skipped += 1
                continue
        q_lower = m["question"].lower()
        if any(k in q_lower for k in ["up or down", "odd or even", "odd/even", "total kills"]):
            skipped += 1
            continue

        # Block negRisk grouped markets (e.g. "between $120-$130") —
        # these are multi-outcome buckets and resolve incorrectly with binary logic
        if m.get("negRisk", False):
            skipped += 1
            continue
        if cid < (1 / 24):
            skipped += 1
            continue

        # Hard filter: skip exact-integer weather markets — prefer above/below/or higher/or lower
        # Exact matches (e.g. "be 14°C on April 22") have higher variance than
        # direction/threshold markets ("be 14°C or higher") — data confirms lower WR
        if cat == "weather":
            is_direction = any(k in q_lower for k in [
                "or higher", "or lower", "or above", "or below",
                "above ", "below ", "at least", "at most",
                "more than", "less than", "exceed", "between"
            ])
            if not is_direction:
                skipped += 1
                continue
        markets.append({
            "id":             str(m.get("id", "")),
            "slug":           m.get("slug", ""),
            "question":       m["question"],
            "yes":            yes,
            "volume":         float(m.get("volume", 0)),
            "category":       cat,
            "closes":         end_dt.isoformat(),
            "closes_in_days": round(cid, 2),
            "clobTokenIds":   m.get("clobTokenIds", []),
        })

    markets.sort(key=lambda x: x["closes_in_days"])
    log(f"✅ [{TIERS[tier_num]['label']}] {len(markets)} markets in {min_days:.0f}-{max_days}d window")
    return markets


# ─────────────────────────────────────────────────────────
#  DIVERSIFICATION
# ─────────────────────────────────────────────────────────

def open_positions_for_tier(tier_num, state):
    return [t for t in state["trades"]
            if t["status"] == "open" and t.get("tier", 1) == tier_num]


def category_slots_available(category, state, tier_num=None):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    # T2 does not block T1 — only count same-tier open positions for the cap
    # T3 is independent too — each tier manages its own category cap
    if tier_num is not None:
        same_tier = [t for t in open_trades if t.get("tier", 1) == tier_num]
    else:
        same_tier = open_trades
    cat_count = sum(
        1 for t in same_tier
        if (t.get("category") or get_category(t.get("market", ""))) == category
    )
    return cat_count < MAX_PER_CATEGORY


# ─────────────────────────────────────────────────────────
#  HAIKU SCREENER  (with diversity cap)
# ─────────────────────────────────────────────────────────

def haiku_screen(markets, state, tier_num):
    if not markets:
        return []

    tcfg       = TIERS[tier_num]
    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    open_ids   = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    candidates = [m for m in markets if m["id"] not in open_ids]

    if not candidates:
        return []

    mkt_list = "\n".join(
        f'ID:{m["id"]} | {m["closes_in_days"]:.0f}d | YES={m["yes"]}¢ | '
        f'Vol=${m["volume"]:,.0f} | cat:{m["category"]} | "{m["question"]}"'
        for m in candidates
    )

    tier_guidance = {
        1: (
            "Prioritise: confirmed facts not yet priced in, weather forecasts clearly "
            "contradicting odds, ongoing verifiable situations. "
            "IMPORTANT: pick a DIVERSE mix of categories — do not select 5 weather markets "
            "when geopolitics, economics, or health markets also have edge.\n\n"
            "WEATHER MARKET PREFERENCE: strongly prefer weather markets phrased as "
            "direction + threshold (e.g. 'above 30°C', 'below 10°C', '25°C or higher') "
            "over exact integer matches (e.g. 'be exactly 28°C', 'be 14°C on April 22'). "
            "Exact-integer markets have higher variance because they require a precise "
            "outcome — direction/threshold markets have a wider range of winning outcomes "
            "and are more reliably mispriced. Score exact-integer weather markets 1-2 "
            "points lower than equivalent direction/threshold markets."
        ),
        2: (
            "Prioritise: structural mispricings over 8-30 days. Economic trajectories, "
            "political situations with known timelines, health outbreak trajectories. "
            "Pick diverse categories.\n\n"
            "WEATHER MARKET PREFERENCE: prefer direction + threshold weather markets "
            "(above/below/or higher/or lower) over exact integer temperature matches."
        ),
    }.get(tier_num, "")

    prompt = (
        f"Screen prediction markets for {TIERS[tier_num]['name'].lower()} strategy.\n"
        f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"Score each 1-10 for MISPRICING POTENTIAL.\n"
        f"High = genuine information asymmetry. Low = efficient.\n\n"
        f"{tier_guidance}\n\n"
        f"Markets:\n{mkt_list}\n\n"
        f"Return ONLY a JSON array:\n"
        f'[{{"id":"market_id","score":7}}, ...]'
    )

    log(f"⚡ [{TIERS[tier_num]['label']}] Haiku screening {len(candidates)} markets...")

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[[\s\S]*?\](?=\s*$|\s*[^,\[\{])', raw)
        if not match:
            match = re.search(r'\[[\s\S]*\]', raw)
        raw = match.group(0) if match else "[]"

        scores = json.loads(raw)
        scores.sort(key=lambda x: x.get("score", 0), reverse=True)

        # ── Diversity cap: max screener_max_per_cat per category ──
        top_n        = tcfg["screener_top_n"]
        max_per_cat  = tcfg.get("screener_max_per_cat", 2)
        seen_cats    = {}
        diverse_ids  = []

        for s in scores:
            mkt = next((m for m in candidates if m["id"] == s["id"]), None)
            if not mkt:
                continue
            cat = mkt.get("category") or get_category(mkt["question"])
            if seen_cats.get(cat, 0) < max_per_cat:
                diverse_ids.append(s["id"])
                seen_cats[cat] = seen_cats.get(cat, 0) + 1
            if len(diverse_ids) >= top_n:
                break

        top_markets = [m for m in candidates if m["id"] in diverse_ids]

        log(f"⚡ [{TIERS[tier_num]['label']}] Selected {len(top_markets)} (diverse):")
        id_to_score = {s["id"]: s.get("score", 0) for s in scores}
        for m in top_markets:
            log(f"   {id_to_score.get(m['id'],0)}/10 [{m['category']}] — {m['question'][:55]}")

        return top_markets

    except Exception as e:
        log(f"⚠️  Haiku screener error ({e})")
        return candidates[:tcfg["screener_top_n"]]


# ─────────────────────────────────────────────────────────
#  DDG SEARCH + HAIKU INTERPRET
# ─────────────────────────────────────────────────────────

def build_search_query(market):
    q          = market["question"]
    cat        = get_category(q)
    now        = datetime.now(timezone.utc)
    month_year = now.strftime("%B %Y")
    date_full  = now.strftime("%B %d %Y")

    q_clean = re.sub(r'^Will\s+', '', q, flags=re.IGNORECASE)
    q_clean = re.sub(r'\?$', '', q_clean).strip()

    if cat == "weather":
        city_match = re.search(r'in ([A-Z][a-zA-Z\s]+?)(?:\s+be|\s+have|\s+reach|\s+exceed)', q)
        city = city_match.group(1).strip() if city_match else q_clean
        return f"{city} weather forecast high temperature {date_full}"
    elif cat == "health":
        return f"{q_clean} latest data {month_year}"
    elif cat == "economics":
        q_clean = re.sub(r'between [\$\d,k\s]+and [\$\d,k\s]+', '', q_clean).strip()
        return f"{q_clean} {month_year} forecast"
    elif cat == "crypto":
        coin = "bitcoin"
        for c in ["ethereum", "eth", "solana", "bnb", "xrp"]:
            if c in q.lower():
                coin = c
                break
        if "btc" in q.lower():
            coin = "bitcoin"
        return f"{coin} price {date_full} USD"
    elif cat == "geopolitics":
        return f"{q_clean} latest news {month_year}"
    elif cat == "politics":
        return f"{q_clean} {month_year}"
    else:
        return f"{q_clean} {month_year}"


def ddg_search(query, max_results=5):
    if not DDG_AVAILABLE:
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        log(f"     ⚠️  DDG: {e}")
        return []


# ── Polymarket CLOB — live order book depth ───────────────
# Public endpoint, no auth required
CLOB_URL = "https://clob.polymarket.com"

def get_order_book_depth(market):
    """
    Fetch live CLOB order book for a market using its clobTokenIds.
    Returns a formatted string summarising depth, spread, and informed-money signals.
    No authentication required — fully public endpoint.

    Signals we extract:
    - Total bid/ask depth (liquidity available)
    - Spread (tighter = more efficient/sharper market)
    - Bid wall: large block at a single price (informed money signal)
    - Book imbalance: bid depth vs ask depth ratio
    """
    clob_ids = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    if not clob_ids:
        # Try to parse from string if stored as JSON string
        raw = market.get("clobTokenIds", "[]")
        if isinstance(raw, str):
            try:
                import json as _json
                clob_ids = _json.loads(raw)
            except Exception:
                clob_ids = []

    if not clob_ids:
        return None

    # Fetch order book for the YES token (index 0)
    token_id = clob_ids[0] if isinstance(clob_ids, list) else clob_ids
    try:
        r = requests.get(
            f"{CLOB_URL}/book?token_id={token_id}",
            timeout=6
        )
        if r.status_code != 200:
            return None

        book = r.json()
        bids = book.get("bids", [])  # [{price, size}, ...]
        asks = book.get("asks", [])

        if not bids and not asks:
            return None

        # Compute depth
        bid_depth  = sum(float(b.get("size", 0)) for b in bids)
        ask_depth  = sum(float(a.get("size", 0)) for a in asks)
        total_depth = bid_depth + ask_depth

        # Best bid/ask and spread
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        spread   = round(best_ask - best_bid, 4)

        # Detect bid wall — single price level with >30% of total bid depth
        bid_wall = None
        for b in bids[:5]:
            sz = float(b.get("size", 0))
            if bid_depth > 0 and sz / bid_depth > 0.30:
                bid_wall = f"{float(b['price']):.2f}¢ (${sz:,.0f})"
                break

        # Book imbalance
        imbalance = "balanced"
        if bid_depth + ask_depth > 0:
            bid_pct = bid_depth / (bid_depth + ask_depth)
            if bid_pct > 0.70:
                imbalance = f"bid-heavy ({bid_pct:.0%} bids) — buyers dominating"
            elif bid_pct < 0.30:
                imbalance = f"ask-heavy ({1-bid_pct:.0%} asks) — sellers dominating"

        # Signal interpretation
        signals = []
        if total_depth < 500:
            signals.append("⚠️ thin book (<$500 depth) — price easily moved, be cautious")
        elif total_depth < 2000:
            signals.append("moderate liquidity ($500-2k depth)")
        else:
            signals.append(f"good liquidity (${total_depth:,.0f} total depth)")

        if spread > 0.05:
            signals.append(f"wide spread ({spread:.3f}) — inefficient market, potential edge")
        elif spread < 0.02:
            signals.append(f"tight spread ({spread:.3f}) — efficient market, sharps present")

        if bid_wall:
            signals.append(f"bid wall at {bid_wall} — possible informed support")

        log(f"     📊 CLOB depth: ${total_depth:,.0f} | spread: {spread:.3f} | {imbalance}")

        return (
            f"\nLIVE ORDER BOOK (CLOB, fetched now):\n"
            f"  Best bid:    {best_bid:.3f} YES  |  Best ask: {best_ask:.3f} YES\n"
            f"  Spread:      {spread:.3f}\n"
            f"  Bid depth:   ${bid_depth:,.0f}\n"
            f"  Ask depth:   ${ask_depth:,.0f}\n"
            f"  Total depth: ${total_depth:,.0f}\n"
            f"  Imbalance:   {imbalance}\n"
            f"  Signals:     {' | '.join(signals)}\n"
            f"  {'Bid wall: ' + bid_wall if bid_wall else ''}\n"
            f"Note: Thin book or tight spread with heavy bids = sharp money present = "
            f"higher bar required for NO entry. Wide spread = potential inefficiency.\n"
        )

    except Exception as e:
        log(f"     ⚠️  CLOB depth fetch failed: {e}")
        return None


# ── Coin slug mapping ─────────────────────────────────────
COIN_MAP = {
    "bitcoin":  {"binance": "BTCUSDT",  "coingecko": "bitcoin"},
    "btc":      {"binance": "BTCUSDT",  "coingecko": "bitcoin"},
    "ethereum": {"binance": "ETHUSDT",  "coingecko": "ethereum"},
    "eth":      {"binance": "ETHUSDT",  "coingecko": "ethereum"},
    "solana":   {"binance": "SOLUSDT",  "coingecko": "solana"},
    "sol":      {"binance": "SOLUSDT",  "coingecko": "solana"},
    "xrp":      {"binance": "XRPUSDT",  "coingecko": "ripple"},
    "bnb":      {"binance": "BNBUSDT",  "coingecko": "binancecoin"},
    "hyperliquid": {"binance": None,    "coingecko": "hyperliquid"},
}

def get_crypto_price(question):
    """
    Fetch live price data for the coin mentioned in a market question.
    Tries Binance first (no key, real-time), falls back to CoinGecko.
    Returns a formatted string ready to inject into the research prompt.
    """
    q = question.lower()
    coin_key = None
    for k in COIN_MAP:
        if k in q:
            coin_key = k
            break
    if not coin_key:
        return None

    cfg = COIN_MAP[coin_key]

    # ── Try Binance first ─────────────────────────────────
    if cfg["binance"]:
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={cfg['binance']}",
                timeout=6
            )
            if r.status_code == 200:
                d = r.json()
                price    = float(d["lastPrice"])
                chg      = float(d["priceChangePercent"])
                high     = float(d["highPrice"])
                low      = float(d["lowPrice"])
                vol      = float(d["volume"])
                log(f"     💰 Binance {coin_key.upper()}: ${price:,.2f} ({chg:+.2f}% 24h)")
                return (
                    f"LIVE PRICE DATA ({coin_key.upper()} via Binance, fetched now):\n"
                    f"  Current price: ${price:,.2f}\n"
                    f"  24h change:    {chg:+.2f}%\n"
                    f"  24h high:      ${high:,.2f}\n"
                    f"  24h low:       ${low:,.2f}\n"
                    f"  24h volume:    {vol:,.0f} {coin_key.upper()}\n"
                )
        except Exception as e:
            log(f"     ⚠️  Binance price fetch failed: {e}")

    # ── Fallback: CoinGecko (no key needed) ───────────────
    try:
        cg_id = cfg["coingecko"]
        r = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={cg_id}&vs_currencies=usd"
            f"&include_24hr_change=true&include_24hr_vol=true&include_24hr_high_low=true",
            timeout=6
        )
        if r.status_code == 200:
            d = r.json().get(cg_id, {})
            price = d.get("usd", 0)
            chg   = d.get("usd_24h_change", 0)
            log(f"     💰 CoinGecko {coin_key.upper()}: ${price:,.2f} ({chg:+.2f}% 24h)")
            return (
                f"LIVE PRICE DATA ({coin_key.upper()} via CoinGecko, fetched now):\n"
                f"  Current price: ${price:,.2f}\n"
                f"  24h change:    {chg:+.2f}%\n"
            )
    except Exception as e:
        log(f"     ⚠️  CoinGecko price fetch failed: {e}")

    return None


def search_market(market):
    query   = build_search_query(market)
    results = ddg_search(query)
    if not results:
        fallback = f"{market['question'][:80]} {datetime.now(timezone.utc).strftime('%B %Y')}"
        results  = ddg_search(fallback, max_results=3)
        if results:
            query = fallback
    log(f"     🔍 \"{query}\" → {len(results)} results")
    return query, results


def haiku_interpret(client, market, query, raw_results):
    if not raw_results:
        return (
            f"No search results. Market: {market['question']} | "
            f"Odds: YES={market['yes']}¢. Insufficient data — do not recommend."
        )

    results_txt = "\n\n".join(
        f"[{i+1}] Title: {r.get('title','N/A')}\n"
        f"    URL: {r.get('href','N/A')}\n"
        f"    Snippet: {r.get('body','N/A')}"
        for i, r in enumerate(raw_results[:5])
    )

    prompt = (
        f"Research brief for prediction market trader.\n\n"
        f"MARKET: \"{market['question']}\"\n"
        f"ODDS: YES={market['yes']}¢ | NO={100-market['yes']}¢\n"
        f"CLOSES: {market['closes_in_days']:.0f}d ({market['closes'][:10]})\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n\n"
        f"SEARCH RESULTS:\n{results_txt}\n\n"
        f"3-5 sentence factual brief:\n"
        f"1. Key current facts relevant to YES/NO\n"
        f"2. Directional implication\n"
        f"3. Specific numbers/dates/forecasts\n\n"
        f"Only use search data. If irrelevant, say so clearly."
    )

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Research failed: {e}"



def get_stock_price(question):
    """
    Fetch live stock price via Finnhub for stock/earnings markets.
    Returns a formatted string ready to inject into the research prompt.
    """
    if not FINNHUB_API_KEY:
        return None

    q = question.lower()

    # Map common tickers mentioned in market questions
    TICKER_MAP = {
        "amazon": "AMZN", "amzn": "AMZN",
        "tesla": "TSLA",  "tsla": "TSLA",
        "nvidia": "NVDA",  "nvda": "NVDA",
        "google": "GOOGL", "googl": "GOOGL", "alphabet": "GOOGL",
        "apple": "AAPL",   "aapl": "AAPL",
        "microsoft": "MSFT", "msft": "MSFT",
        "meta": "META",
        "netflix": "NFLX", "nflx": "NFLX",
        "palantir": "PLTR", "pltr": "PLTR",
        "s&p 500": "SPY",  "spy": "SPY",
        "spy": "SPY",
        "nasdaq": "QQQ",   "qqq": "QQQ",
        "shopify": "SHOP",
        "intel": "INTC",   "intc": "INTC",
        "american express": "AXP", "axp": "AXP",
        "lockheed": "LMT",  "lmt": "LMT",
        "general dynamics": "GD", " gd ": "GD",
        "honeywell": "HON",  "hon": "HON",
        "dow ": "DOW",
        "at&t": "T",
        "moody": "MCO",
        "cbre": "CBRE",
        "procter": "PG",    " pg ": "PG",
        "american airlines": "AAL", "aal": "AAL",
        "texas instruments": "TXN", "txn": "TXN",
        # Commodities — Finnhub futures
        "wti": "USOIL", "crude oil": "USOIL", "oil ": "USOIL",
        "brent": "UKOIL",
        "gold": "XAUUSD", " xau": "XAUUSD",
        "silver": "XAGUSD",
        "natural gas": "NG1:NYMEX",
    }

    ticker = None
    for keyword, sym in TICKER_MAP.items():
        if keyword in q:
            ticker = sym
            break

    if not ticker:
        return None

    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=6
        )
        if r.status_code == 200:
            d = r.json()
            price   = d.get("c", 0)   # current price
            prev    = d.get("pc", 0)  # previous close
            high    = d.get("h", 0)
            low     = d.get("l", 0)
            if price and price > 0:
                chg_pct = ((price - prev) / prev * 100) if prev else 0
                log(f"     📈 Finnhub {ticker}: ${price:.2f} ({chg_pct:+.2f}% vs prev close)")
                return (
                    f"LIVE PRICE DATA ({ticker} via Finnhub, fetched now):\n"
                    f"  Current price:  ${price:.2f}\n"
                    f"  Previous close: ${prev:.2f}\n"
                    f"  Change:         {chg_pct:+.2f}%\n"
                    f"  Today high:     ${high:.2f}\n"
                    f"  Today low:      ${low:.2f}\n"
                    f"  NOTE: Use this live price as the definitive current price — "
                    f"ignore any stale prices from web search results.\n"
                )
    except Exception as e:
        log(f"     ⚠️  Finnhub price fetch failed for {ticker}: {e}")

    return None

def research_all_markets(markets):
    client        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    research      = {}
    graph_context = load_graph_context()   # load once per scan
    log(f"🔬 Researching {len(markets)} markets...")
    for i, market in enumerate(markets):
        log(f"  [{i+1}/{len(markets)}] {market['question'][:65]}")

        # Fetch live crypto price before DDG search
        live_price = None
        if get_category(market["question"]) == "crypto":
            live_price = get_crypto_price(market["question"])

        # Fetch live stock price for stock/economics markets
        if not live_price and get_category(market["question"]) in ("stocks", "economics", "other", "commodities", "crypto"):
            live_price = get_stock_price(market["question"])

        # Fetch live weather forecast for weather markets
        weather_forecast = None
        if get_category(market["question"]) == "weather":
            weather_forecast = get_weather_forecast(
                market["question"],
                target_date_str=market.get("closes")[:10] if market.get("closes") else None
            )

        # Fetch live CLOB order book depth
        book_depth = get_order_book_depth(market)

        # Check economic calendar risk
        cal_risk = check_calendar_risk(
            market["question"],
            market.get("closes_in_days", 7)
        )

        query, raw = search_market(market)
        brief = haiku_interpret(client, market, query, raw)

        # Prepend live price data
        if live_price:
            brief = live_price + "\n" + brief

        # Prepend live weather forecast
        if weather_forecast:
            brief = weather_forecast + "\n" + brief

        # Append CLOB order book depth
        if book_depth:
            brief = brief + book_depth

        # Append calendar risk warning
        if cal_risk:
            log(f"     📅 Calendar risk flagged")
            brief = brief + "\n\n" + cal_risk

        # Append graph context (patterns from trade history)
        if graph_context:
            brief = brief + graph_context

        log(f"     📋 {brief[:100]}...")
        research[market["id"]] = brief
    return research


# ─────────────────────────────────────────────────────────
#  KELLY SIZING
# ─────────────────────────────────────────────────────────

def kelly_size(win_prob, market_win_prob, bankroll, tier_num, closes_in_days=7.0, confidence=75):
    tcfg = TIERS[tier_num]
    if not (0 < market_win_prob < 100) or not (0 < win_prob < 100):
        return 0.0

    p = win_prob / 100
    q = 1 - p
    b = (1 - market_win_prob / 100) / (market_win_prob / 100)
    if b <= 0:
        return 0.0

    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0.0

    fraction = tcfg["kelly"][-1]["fraction"]
    cap_pct  = tcfg["kelly"][-1]["max_pct"]
    for tier in tcfg["kelly"]:
        if confidence >= tier["min_conf"]:
            fraction = tier["fraction"]
            cap_pct  = tier["max_pct"]
            break

    sized = full_kelly * fraction

    if tier_num == 1:
        if closes_in_days <= 1.0:
            sized *= tcfg.get("short_disc_1d", 0.65)
        elif closes_in_days <= 2.0:
            sized *= tcfg.get("short_disc_2d", 0.80)
    if tier_num == 2:
        sized *= tcfg.get("time_discount", 0.75)

    return round(min(max(sized, 0.0), cap_pct / 100) * bankroll, 2)


def get_tier_name(confidence, tier_num):
    tcfg = TIERS[tier_num]
    if tcfg.get("fixed_pct"):
        return f"fixed {tcfg['fixed_pct']}%"
    for tier in tcfg["kelly"]:
        if confidence >= tier["min_conf"]:
            fname = {1.0: "full", 0.5: "half", 0.25: "quarter"}.get(tier["fraction"], "?")
            return f"{fname}-Kelly ({tier['max_pct']}%)"
    return f"Kelly ({tcfg['kelly'][-1]['max_pct']}%)"


# ─────────────────────────────────────────────────────────
#  JSON PARSE HELPER  (hardened)
# ─────────────────────────────────────────────────────────

def parse_json_array(text):
    """Robustly extract a JSON array from Opus output."""
    text = text.strip().replace("```json", "").replace("```", "").strip()
    if not text or text in ("{}", "null", ""):
        return []
    if not text.startswith("["):
        match = re.search(r'\[[\s\S]*\]', text)
        text  = match.group(0) if match else "[]"
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────
#  OPUS ANALYST — TIER 1 & 2
# ─────────────────────────────────────────────────────────

def opus_analyze_short_medium(markets, research, state, tier_num):
    if not markets:
        return []

    tcfg      = TIERS[tier_num]
    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    open_pos  = open_positions_for_tier(tier_num, state)
    available = tcfg["max_positions"] - len(open_pos)

    if available <= 0:
        log(f"[{tcfg['label']}] Max positions reached")
        return []

    closed = [t for t in state["trades"] if t["status"] == "closed" and t.get("tier", 1) == tier_num]
    won    = [t for t in closed if t.get("won")]
    lost   = [t for t in closed if not t.get("won")]

    history_ctx = ""
    if closed:
        # NOTE: Win rate deliberately excluded — prevents Opus anchoring on
        # recent success and loosening its edge requirements.
        history_ctx = f"\nRECENT TRADES [{tcfg['label']}] (evaluate each on its own merits, ignore overall record):\n"
        for t in closed[-8:]:
            result = "WON" if t.get("won") else "LOST"
            edge   = abs(t.get("true_prob", 0) - t.get("market_prob", 0))
            history_ctx += f"  {result} | conf {t.get('confidence',0)}% | edge {edge}% | {t['market'][:55]}\n"
        if lost:
            history_ctx += "\nLoss patterns to avoid repeating:\n"
            for t in lost[-4:]:
                history_ctx += f"  LOST — {t.get('bear_case','?')[:70]}\n"

    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    open_ctx    = ""
    if open_trades:
        open_ctx = "\nOPEN POSITIONS (do NOT re-recommend):\n"
        for t in open_trades:
            open_ctx += f"  [{TIERS[t.get('tier',1)]['label']}] {t['position']} | {t.get('category','?')} | {t['market'][:65]}\n"
        cat_counts = {}
        for t in open_trades:
            c = t.get("category") or get_category(t.get("market", ""))
            cat_counts[c] = cat_counts.get(c, 0) + 1
        t1_cat_counts = {}
        for t in open_trades:
            if t.get("tier", 1) == tier_num:
                c = t.get("category") or get_category(t.get("market", ""))
                t1_cat_counts[c] = t1_cat_counts.get(c, 0) + 1
        open_ctx += f"\nALL OPEN CATEGORY COUNTS: {cat_counts}"
        open_ctx += f"\nT{tier_num} CATEGORY COUNTS (cap applies here): {t1_cat_counts} | MAX: {MAX_PER_CATEGORY}"

    mkt_sections = []
    for m in markets:
        brief = research.get(m["id"], "No research.")
        mkt_sections.append(
            f"─── ID:{m['id']} [{m['category']}] ───\n"
            f"Question: \"{m['question']}\"\n"
            f"Odds: YES={m['yes']}¢ | NO={100-m['yes']}¢ | Vol=${m['volume']:,.0f} | "
            f"Closes {m['closes'][:10]} ({m['closes_in_days']:.0f}d)\n"
            f"Research: {brief}\n"
        )

    prompt = (
        f"Expert prediction market trader — {tcfg['name']} strategy.\n\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n"
        f"BANKROLL: ${state['bankroll']:.2f} | SLOTS: {available} | "
        f"MIN CONF: {tcfg['min_confidence']}% | MIN EDGE: {tcfg['min_edge_pct']}%\n"
        f"{history_ctx}\n{open_ctx}\n\n"
        f"MARKETS:\n{''.join(mkt_sections)}\n\n"
        f"REAL EDGE only:\n"
        f"  ✅ Weather forecast clearly contradicts odds\n"
        f"  ✅ Confirmed fact not priced in\n"
        f"  ✅ Verifiable situation with strong directional signal\n"
        f"  ✅ Health/economic data with clear trajectory\n"
        f"  ❌ Vague research, uncertain outcomes, sports\n"
        f"  ❌ Central bank/Fed/ECB decisions based only on analyst forecasts\n"
        f"     or conditional scenarios — these require CONFIRMED data releases\n"
        f"     or explicit official forward guidance, not 'Bank X thinks Y might happen'\n"
        f"  ❌ Weather markets asking for an EXACT integer temperature (e.g. 'be exactly\n"
        f"     28°C') — these have high variance. Prefer direction/threshold markets\n"
        f"     ('above 28°C', 'below 10°C', '25°C or higher') which have a wider range\n"
        f"     of winning outcomes. Only take exact-integer weather bets with very high\n"
        f"     confidence (≥88%) AND strong forecast data.\n"
        f"  ❌ Markets where LIVE ORDER BOOK shows tight spread (<0.02) AND heavy bid\n"
        f"     depth — this signals sharp/informed money has already priced the market\n"
        f"     efficiently. Require extra edge (≥5 points above min) before entering.\n"
        f"  ✅ Markets with wide spread (>0.05) and thin book — potential inefficiency\n"
        f"     that hasn't been arbed away yet.\n\n"
        f"0 trades beats a bad trade.\n\n"
        f"CONFIDENCE CALIBRATION — READ CAREFULLY:\n"
        f"  Your confidence must reflect OUTCOME CERTAINTY, not reasoning quality.\n"
        f"  Strong logical arguments do NOT justify high confidence on uncertain events.\n"
        f"  Apply these hard caps:\n"
        f"  • Politics / human behaviour / speeches / social media:\n"
        f"    Hard cap: 78%. These are inherently unpredictable regardless of context.\n"
        f"    Even if Trump 'always' says something, cap at 78%.\n"
        f"  • Geopolitics / diplomacy / military events:\n"
        f"    Hard cap: 80%. Situations can shift rapidly without warning.\n"
        f"  • Economics / central bank / earnings:\n"
        f"    Hard cap: 82%. Only if confirmed data release, not analyst forecasts.\n"
        f"  • Crypto / stocks closing same day with current price data:\n"
        f"    Allow up to 88% ONLY if price is already beyond threshold.\n"
        f"  • Weather with specific day forecast within 6 hours:\n"
        f"    Allow up to 88% ONLY if same-day forecast is clearly outside threshold.\n"
        f"  • Verifiable events already confirmed in research:\n"
        f"    Allow up to 90%.\n"
        f"  If you find yourself assigning >80% to a political or behavioural market,\n"
        f"  that is a calibration error — reduce it.\n\n"
        f"PROBABILITIES: report as YES probability (0-100).\n"
        f"CONFIDENCE: ≥{tcfg['min_confidence']}% required."
        f"DIVERSIFICATION: Max {MAX_PER_CATEGORY} per category across ALL tiers.\n\n"
        f"Return ONLY valid JSON array ([] if nothing qualifies):\n"
        f'[{{"market_id":"ID","market":"question","position":"YES or NO",'
        f'"market_prob":27,"true_prob":5,"confidence":88,"category":"weather",'
        f'"research_summary":"facts","key_factors":["f1","f2","f3"],'
        f'"bear_case":"risk"}}]'
    )

    log(f"🧠 [{tcfg['label']}] Opus analyzing {len(markets)} markets...")

    try:
        response = client.messages.create(
            model=ANALYST_MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}]
        )

        thinking_txt = ""
        full_text    = ""
        for block in response.content:
            if hasattr(block, "type"):
                if block.type == "thinking":
                    thinking_txt = block.thinking
                    log(f"  💭 Thought for {len(thinking_txt)} chars")
                elif block.type == "text":
                    full_text += block.text

        return _validate_recs(parse_json_array(full_text), markets, state, tier_num)

    except Exception as e:
        log(f"❌ Opus error: {e}")
        return []


# ─────────────────────────────────────────────────────────
#  OPUS ANALYST — TIER 3
# ─────────────────────────────────────────────────────────

def opus_analyze_long(markets, state):
    if not markets:
        return []

    tcfg      = TIERS[3]
    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    open_pos  = open_positions_for_tier(3, state)
    available = tcfg["max_positions"] - len(open_pos)

    if available <= 0:
        log("[T3] Max long-term positions reached")
        return []

    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    open_ctx    = ""
    if open_trades:
        open_ctx = "\nOPEN POSITIONS (do NOT re-recommend):\n"
        for t in open_trades:
            open_ctx += f"  [{TIERS[t.get('tier',1)]['label']}] {t['position']} | {t.get('category','?')} | {t['market'][:65]}\n"
        cat_counts = {}
        for t in open_trades:
            c = t.get("category") or get_category(t.get("market", ""))
            cat_counts[c] = cat_counts.get(c, 0) + 1
        open_ctx += f"\nCATEGORY COUNTS: {cat_counts} | MAX: {MAX_PER_CATEGORY}"

    mkt_list = "\n".join(
        f'ID:{m["id"]} | YES={m["yes"]}¢ NO={100-m["yes"]}¢ | Vol=${m["volume"]:,.0f} | '
        f'Closes {m["closes"][:10]} ({m["closes_in_days"]:.0f}d) | [{m["category"]}] "{m["question"]}"'
        for m in markets
    )

    prompt = (
        f"Expert prediction market trader — LONG-TERM strategy (31-180 days).\n\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n"
        f"BANKROLL: ${state['bankroll']:.2f} | SLOTS: {available}\n"
        f"MIN CONFIDENCE: {tcfg['min_confidence']}% | MIN EDGE: {tcfg['min_edge_pct']}%\n"
        f"SIZING: Fixed {tcfg['fixed_pct']}% per trade\n"
        f"{open_ctx}\n\n"
        f"MARKETS:\n{mkt_list}\n\n"
        f"Use knowledge of current events, base rates, and trajectory analysis "
        f"to find genuine long-term mispricings.\n\n"
        f"GOOD TRADES: geopolitical momentum, political fundamentals, economic trends, "
        f"health outbreak trajectories, near-certain or near-impossible binary events.\n"
        f"BAD TRADES: sports, crypto, anything easily reversed in 6 months.\n\n"
        f"Think: base rates, current trajectory, what reversal requires, crowd anchoring.\n\n"
        f"CONFIDENCE ≥90% required. Below 90% = do NOT recommend.\n"
        f"DIVERSIFICATION: Max {MAX_PER_CATEGORY} per category.\n\n"
        f"Return ONLY valid JSON array ([] if nothing qualifies):\n"
        f'[{{"market_id":"ID","market":"question","position":"YES or NO",'
        f'"market_prob":27,"true_prob":5,"confidence":92,"category":"geopolitics",'
        f'"research_summary":"structural reasoning","key_factors":["f1","f2","f3"],'
        f'"bear_case":"what invalidates thesis"}}]'
    )

    log(f"🧠 [T3] Opus analyzing {len(markets)} long-term markets...")

    try:
        response = client.messages.create(
            model=ANALYST_MODEL,
            max_tokens=10000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}]
        )

        thinking_txt = ""
        full_text    = ""
        for block in response.content:
            if hasattr(block, "type"):
                if block.type == "thinking":
                    thinking_txt = block.thinking
                    log(f"  💭 Thought for {len(thinking_txt)} chars")
                elif block.type == "text":
                    full_text += block.text

        log(f"  📊 Thinking depth: {len(thinking_txt)} chars")
        return _validate_recs(parse_json_array(full_text), markets, state, 3)

    except Exception as e:
        log(f"❌ Opus T3 error: {e}")
        return []


# ─────────────────────────────────────────────────────────
#  RECOMMENDATION VALIDATION
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
#  UTTERANCE MARKET FILTER
# ─────────────────────────────────────────────────────────

import re as _re

_UTTERANCE_PAT = _re.compile(
    r"will\s+\w+.*?(?:say|post|tweet|mention).*?['\"]",
    _re.IGNORECASE
)

def is_utterance_market(question):
    """Quoted-phrase utterance markets are adversarially priced."""
    return bool(_UTTERANCE_PAT.search(question))


def _validate_recs(recs, markets, state, tier_num):
    tcfg  = TIERS[tier_num]
    valid = []

    for r in recs:
        edge = abs(r.get("true_prob", 0) - r.get("market_prob", 0))
        if edge < tcfg["min_edge_pct"]:
            log(f"  ⏭  Edge {edge}% < {tcfg['min_edge_pct']}% — {r.get('market','')[:50]}")
            continue
        if r.get("confidence", 0) < tcfg["min_confidence"]:
            log(f"  ⏭  Conf {r.get('confidence')}% < {tcfg['min_confidence']}% — {r.get('market','')[:50]}")
            continue
        if not any(m["id"] == r.get("market_id") for m in markets):
            log(f"  ⚠️  Unknown market_id {r.get('market_id')} — skip")
            continue
        cat = r.get("category") or get_category(r.get("market", ""))
        if cat in BLOCKED_CATEGORIES:
            log(f"  ⛔ '{cat}' blocked — {r.get('market','')[:50]}")
            continue
        if not category_slots_available(cat, state, tier_num):
            log(f"  ⏭  '{cat}' full (T{tier_num}) — {r.get('market','')[:50]}")
            continue
        if is_utterance_market(r.get("market", "")):
            log(f"  ⛔ Utterance market blocked — {r.get('market','')[:65]}")
            continue
        valid.append(r)

    log(f"🤖 [{TIERS[tier_num]['label']}] Recommends {len(valid)} trade(s)")
    for r in valid:
        edge = abs(r.get("true_prob", 0) - r.get("market_prob", 0))
        cat  = r.get("category") or get_category(r.get("market", ""))
        log(f"  📋 BUY {r['position']} | {cat} | conf {r['confidence']}% | edge {edge}% | "
            f"YES_mkt={r['market_prob']}% → true={r['true_prob']}%")
        log(f"     {r['market'][:70]}")
        log(f"     {r.get('research_summary','')[:110]}")
        log(f"     Bear: {r.get('bear_case','')[:75]}")

    return valid


# ─────────────────────────────────────────────────────────
#  TRADE EXECUTION
# ─────────────────────────────────────────────────────────

def place_paper_trade(rec, markets, state, tier_num, news_triggered=False):
    tcfg = TIERS[tier_num]
    conf = rec.get("confidence", 0)

    if conf < tcfg["min_confidence"]:
        log(f"  ⏭  Conf {conf}% < {tcfg['min_confidence']}%")
        return state
    if rec.get("market_id") in {t["market_id"] for t in state["trades"] if t["status"] == "open"}:
        log(f"  ⏭  Already open")
        return state
    if len(open_positions_for_tier(tier_num, state)) >= tcfg["max_positions"]:
        log(f"  ⏭  [{tcfg['label']}] Max positions")
        return state
    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit")
        return state

    cat = rec.get("category") or get_category(rec.get("market", ""))
    if cat in BLOCKED_CATEGORIES:
        log(f"  ⛔ '{cat}' blocked")
        return state
    if not category_slots_available(cat, state, tier_num):
        log(f"  ⏭  '{cat}' full (T{tier_num})")
        return state

    mkt = next((m for m in markets if m["id"] == rec["market_id"]), None)
    if not mkt:
        log(f"  ⏭  Market not found")
        return state

    end_dt = parse_utc(mkt["closes"])
    if not end_dt:
        log(f"  ⏭  Cannot parse close date")
        return state

    cid   = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    min_d = tcfg.get("min_hold_days", tcfg.get("min_hold_hours", 2) / 24)
    if cid < min_d or cid > tcfg["max_hold_days"]:
        log(f"  ⏭  {cid:.1f}d outside window")
        return state

    yes_true   = rec["true_prob"]
    yes_market = rec["market_prob"]

    if tcfg.get("fixed_pct"):
        stake      = round(state["bankroll"] * tcfg["fixed_pct"] / 100, 2)
        tier_label = f"fixed {tcfg['fixed_pct']}%"
    else:
        kw = (100 - yes_true)   if rec["position"] == "NO" else yes_true
        km = (100 - yes_market) if rec["position"] == "NO" else yes_market
        log(f"  📐 Kelly [{tcfg['label']}]: {rec['position']} | win={kw}% | market={km}%")
        stake      = kelly_size(kw, km, state["bankroll"], tier_num, cid, conf)
        tier_label = get_tier_name(conf, tier_num)

        # Fix 1: Calibration adjustment — 80-89% P(win) band has 43% actual WR
        # Halve stake in this band until calibration improves to 60%+ WR
        if 80 <= kw < 90:
            original_stake = stake
            stake = round(stake * 0.5, 2)
            log(f"  ⚠️  Calibration adj: P(win)={kw}% in broken band — "
                f"stake halved ${original_stake:.2f} → ${stake:.2f}")
            tier_label += " [cal-halved]"

    # Fix 2: Volume filter — prevent large stakes in thin markets
    market_volume = mkt.get("volume", 0) if mkt else 0
    MIN_VOL_ABS   = 1000   # never trade below this volume
    MIN_VOL_LARGE = 5000   # markets below this cap stake at 5% bankroll
    STAKE_PCT_CAP = 0.05
    if market_volume < MIN_VOL_ABS:
        log(f"  ⛔ Volume ${market_volume:.0f} < $1,000 minimum — skip")
        return state
    if market_volume < MIN_VOL_LARGE and stake > state["bankroll"] * STAKE_PCT_CAP:
        _pct = round(stake / state["bankroll"] * 100, 1)
        log(f"  ⛔ Volume ${market_volume:.0f} < $5,000 and stake ${stake:.2f} ({_pct}%) > 5% — skip")
        return state

    if stake < 1.00:
        log(f"  ⏭  Stake ${stake:.2f} too small")
        return state

    entry  = yes_market if rec["position"] == "YES" else (100 - yes_market)
    payout = round(stake * 100 / entry, 2)
    profit = round(payout - stake, 2)

    trade = {
        "id":               f"T{int(time.time())}",
        "tier":             tier_num,
        "market_id":        mkt["id"],
        "market_slug":      mkt.get("slug", ""),
        "market":           rec["market"],
        "position":         rec["position"],
        "entry_price":      entry,
        "stake":            stake,
        "potential_return": payout,
        "potential_profit": profit,
        "confidence":       conf,
        "true_prob":        yes_true,
        "market_prob":      yes_market,
        "category":         cat,
        "closes_in_days":   round(cid, 1),
        "closes":           end_dt.isoformat(),
        "research_summary": rec.get("research_summary", ""),
        "key_factors":      rec.get("key_factors", []),
        "bear_case":        rec.get("bear_case", ""),
        "kelly_tier":       tier_label,
        "news_triggered":   news_triggered,
        "status":           "open",
        "placed_at":        datetime.now(timezone.utc).isoformat(),
        "paper":            True,
        "model":            ANALYST_MODEL,
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    flag = "📰 " if news_triggered else ""
    log(f"  ✅ [{tcfg['label']}] {flag}BET — {trade['position']} @ {entry}¢  [{tier_label}]")
    log(f"     {trade['market'][:70]}")
    log(f"     {cat} | Closes {end_dt.strftime('%b %d')} ({cid:.0f}d) | "
        f"Stake ${stake:.2f} | Win ${payout:.2f} | Conf {conf}%")
    log(f"     Bankroll now ${state['bankroll']:.2f}")

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

    print("\n" + "═" * 65)
    print("  CLAUDEBOT v13  ·  Three-Tier + News Monitor")
    print("═" * 65)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Closed         {len(closed_t)}  ({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Total Scans    {state.get('scan_count', 0)}")

    for tn in [1, 2]:
        op = len(open_positions_for_tier(tn, state))
        mx = TIERS[tn]["max_positions"]
        print(f"  {tier_badge(tn)} T{tn} {TIERS[tn]['name']:<12} {op}/{mx} open | "
              f"conf≥{TIERS[tn]['min_confidence']}% | edge≥{TIERS[tn]['min_edge_pct']}%")

    print(f"  📰 News        {'✅ active' if FEEDPARSER_AVAILABLE else '❌ pip install feedparser'}")
    print(f"  DDG            {'✅ available' if DDG_AVAILABLE else '❌ not installed'}")
    print(f"  Telegram       {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    print("═" * 65)

    if open_t:
        print("\n  OPEN POSITIONS:")
        for t in open_t:
            close_dt   = parse_utc(t.get("closes", ""))
            cid        = round(days_until(close_dt), 1) if close_dt else "?"
            closes_str = close_dt.strftime("%b %d") if close_dt else "?"
            cat        = t.get("category", "?")
            badge      = tier_badge(t.get("tier", 1))
            news_flag  = "📰" if t.get("news_triggered") else ""
            print(f"  {badge}{news_flag} {t['position']} | ${t['stake']:.2f} | {cat} | "
                  f"{closes_str} ({cid}d) | {t['market'][:40]}")
    print()


# ─────────────────────────────────────────────────────────
#  RUN TIER
# ─────────────────────────────────────────────────────────

def run_tier(tier_num, state, priority_markets=None):
    tcfg = TIERS[tier_num]
    log(f"{'─'*50}")
    log(f"🔄 Running {tcfg['name']} (T{tier_num}) scan")

    markets = fetch_markets_for_tier(tier_num)
    if not markets:
        log(f"[T{tier_num}] No markets in window")
        return state

    # Priority markets from news bypass screener (T1 only)
    if priority_markets and tier_num == 1:
        valid_ids      = {m["id"] for m in markets}
        priority_valid = [m for m in priority_markets if m["id"] in valid_ids]

        if priority_valid:
            log(f"📰 Processing {len(priority_valid)} news-priority market(s)...")
            p_research = research_all_markets(priority_valid)
            p_recs     = opus_analyze_short_medium(priority_valid, p_research, state, 1)
            for rec in p_recs:
                state = place_paper_trade(rec, markets, state, 1, news_triggered=True)

        priority_ids = {m["id"] for m in priority_valid} if priority_markets else set()
        markets      = [m for m in markets if m["id"] not in priority_ids]

    if tier_num == 3:
        log(f"🧠 [T3] Passing {len(markets)} markets to Opus (pure reasoning)...")
        recs = opus_analyze_long(markets, state)
    else:
        top = haiku_screen(markets, state, tier_num)
        if not top:
            log(f"[T{tier_num}] No candidates after screening")
            return state
        research = research_all_markets(top)
        recs     = opus_analyze_short_medium(top, research, state, tier_num)

    if not recs:
        log(f"[T{tier_num}] No trades this scan")
    else:
        for rec in recs:
            state = place_paper_trade(rec, markets, state, tier_num)

    return state


# ─────────────────────────────────────────────────────────
#  TRADE REASSESSMENT — Two-Strike Brain (T2 + T3)
# ─────────────────────────────────────────────────────────

def should_run_reassessment(state):
    """True if REASSESS_INTERVAL_DAYS have passed since last reassessment."""
    now  = datetime.now(timezone.utc)
    last = state.get("last_reassessment", "")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (now - last_dt).total_seconds() >= REASSESS_INTERVAL_DAYS * 86400
    except Exception:
        return True


def telegram_watch_alert(trade, reason, state):
    """Private Telegram alert when a trade is flagged on first strike."""
    if not TELEGRAM_PERSONAL_ID:
        return
    badge     = tier_badge(trade.get("tier", 2))
    close_dt  = parse_utc(trade.get("closes", ""))
    days_left = round(days_until(close_dt), 1) if close_dt else "?"
    msg = (
        f"👁 <b>WATCH FLAGGED — Strike 1</b>  {badge} T{trade.get('tier',2)}\n"
        f"{'─' * 30}\n"
        f"<b>{trade['market']}</b>\n\n"
        f"⚠️ <b>Concern:</b> <i>{reason}</i>\n\n"
        f"📌 {trade['position']} @ {trade['entry_price']}¢  |  Stake: ${trade['stake']:.2f}\n"
        f"⏰ {days_left}d remaining\n\n"
        f"🔄 No action yet. Will reassess in {REASSESS_INTERVAL_DAYS} days.\n"
        f"If concern persists → trade will be closed."
    )
    send_telegram(msg, TELEGRAM_PERSONAL_ID)


def telegram_reassess_close(trade, reason, strike, state):
    """Private Telegram alert when reassessment closes a trade."""
    if not TELEGRAM_PERSONAL_ID:
        return
    badge     = tier_badge(trade.get("tier", 2))
    close_dt  = parse_utc(trade.get("closes", ""))
    days_left = round(days_until(close_dt), 1) if close_dt else "?"
    strike_txt = "EMERGENCY" if strike == "emergency" else f"Strike {strike}"
    msg = (
        f"🚨 <b>REASSESSMENT CLOSE — {strike_txt}</b>  {badge}\n"
        f"{'─' * 30}\n"
        f"<b>{trade['market']}</b>\n\n"
        f"❌ <b>Thesis broken:</b> <i>{reason}</i>\n\n"
        f"📌 {trade['position']} @ {trade['entry_price']}¢  |  "
        f"Stake: ${trade['stake']:.2f}\n"
        f"⏰ {days_left}d remaining — closing to limit loss\n\n"
        f"ℹ️ Original thesis:\n"
        f"<i>{trade.get('research_summary','')[:200]}</i>"
    )
    send_telegram(msg, TELEGRAM_PERSONAL_ID)


def opus_reassess_trade(trade, state):
    """
    Opus reassesses a single open T2/T3 trade with fresh research.
    Returns: {"verdict": "hold|watch|close|emergency_close", "reason": str}

    Verdicts:
      hold           — thesis intact, no action
      watch          — concern flagged (strike 1), check again next cycle
      close          — thesis weakened, close on strike 2 (or directly)
      emergency_close — thesis completely invalidated, close immediately
    """
    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    close_dt  = parse_utc(trade.get("closes", ""))
    days_left = round(days_until(close_dt), 1) if close_dt else 99

    # Fresh DDG research
    query   = f"{trade['market'][:80]} latest news {datetime.now(timezone.utc).strftime('%B %Y')}"
    results = ddg_search(query, max_results=5)

    results_txt = "\n\n".join(
        f"[{i+1}] {r.get('title','')}\n{r.get('body','')[:200]}"
        for i, r in enumerate(results[:5])
    ) if results else "No search results found."

    # If already on watch, tell Opus — this is the deciding reassessment
    watch_ctx = ""
    if trade.get("watch"):
        watch_ctx = (
            f"\n⚠️ PREVIOUSLY FLAGGED ON WATCH (since {trade.get('watch_since','?')}):\n"
            f"Prior concern: {trade.get('watch_reason','?')}\n"
            f"This is the SECOND assessment. If concern still valid → recommend CLOSE.\n"
        )

    prompt = (
        f"Reassess an open prediction market trade. Has the original thesis changed?\n\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n"
        f"MARKET: \"{trade['market']}\"\n"
        f"POSITION: {trade['position']} @ {trade['entry_price']}¢\n"
        f"CLOSES: {trade.get('closes','?')[:10]} ({days_left:.0f}d remaining)\n"
        f"CATEGORY: {trade.get('category','?')}\n\n"
        f"ORIGINAL THESIS:\n{trade.get('research_summary','N/A')}\n\n"
        f"KEY FACTORS:\n"
        + "\n".join(f"  - {f}" for f in trade.get("key_factors", []))
        + f"\n\nBEAR CASE:\n{trade.get('bear_case','N/A')}\n"
        f"{watch_ctx}\n"
        f"FRESH RESEARCH:\n{results_txt}\n\n"
        f"RULES:\n"
        f"  - Judge on FACTS ON THE GROUND only. Ignore price movement.\n"
        f"  - Small noise or uncertainty = HOLD\n"
        f"  - Clear new facts contradicting thesis = WATCH or CLOSE\n"
        f"  - Thesis completely invalidated = EMERGENCY_CLOSE\n\n"
        f"VERDICTS:\n"
        f"  hold           — thesis intact\n"
        f"  watch          — something concerning, flag for next cycle\n"
        f"  close          — thesis broken (use after prior WATCH, or if clearly wrong)\n"
        f"  emergency_close — outcome already determined against us or confirmed impossible\n\n"
        f"Return ONLY JSON: {{\"verdict\": \"hold\", \"reason\": \"brief explanation\"}}"
    )

    try:
        resp = client.messages.create(
            model=ANALYST_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw   = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        data  = json.loads(match.group(0) if match else '{}')
        verdict = data.get("verdict", "hold").lower().replace(" ", "_")
        reason  = data.get("reason", "No reason provided.")
        return {"verdict": verdict, "reason": reason}
    except Exception as e:
        log(f"  ⚠️  Reassess error: {e}")
        return {"verdict": "hold", "reason": f"Error: {e}"}


def run_trade_reassessments(state):
    """
    Two-strike reassessment brain for open T2 and T3 trades.

    Strike 1 (watch):      Concern flagged → sets watch flag, private Telegram alert, no close
    Strike 2 (close):      Concern persists on next cycle → closes the trade
    Emergency close:        Thesis fully invalidated → closes immediately, no second strike needed
    """
    open_t2t3 = [
        t for t in state["trades"]
        if t["status"] == "open" and t.get("tier", 1) in [2, 3]
    ]

    if not open_t2t3:
        log("🔍 Reassessment: no open T2/T3 positions to check")
        return state

    log(f"🔍 Reassessing {len(open_t2t3)} open T2/T3 position(s)...")

    for trade in open_t2t3:
        close_dt  = parse_utc(trade.get("closes", ""))
        days_left = days_until(close_dt) if close_dt else 99

        if days_left < MIN_DAYS_REMAINING:
            log(f"  ⏭  Skipping ({days_left:.1f}d left — too close to close): {trade['market'][:50]}")
            continue

        log(f"  🔬 [{TIERS[trade['tier']]['label']}] {trade['market'][:60]}")
        result  = opus_reassess_trade(trade, state)
        verdict = result["verdict"]
        reason  = result["reason"]
        log(f"     Verdict: {verdict.upper()} — {reason[:90]}")

        if verdict == "hold":
            if trade.get("watch"):
                log(f"     ✅ Watch cleared — thesis back on track")
                trade.pop("watch", None)
                trade.pop("watch_reason", None)
                trade.pop("watch_since", None)
            else:
                log(f"     ✅ Holding — thesis intact")

        elif verdict == "watch":
            if trade.get("watch"):
                # Already flagged last cycle — this is strike 2, close it
                log(f"     🚨 Strike 2 — concern persists, closing trade")
                telegram_reassess_close(trade, reason, strike=2, state=state)
                _settle(trade, False, state)
            else:
                # First flag — set watch, send private alert, no action yet
                log(f"     👁  Strike 1 — flagging for watch, no action yet")
                trade["watch"]        = True
                trade["watch_reason"] = reason
                trade["watch_since"]  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                telegram_watch_alert(trade, reason, state)

        elif verdict in ("close", "emergency_close"):
            strike = "emergency" if verdict == "emergency_close" else 2
            log(f"     🚨 {'Emergency' if strike == 'emergency' else 'Direct'} close — {reason[:60]}")
            telegram_reassess_close(trade, reason, strike=strike, state=state)
            _settle(trade, False, state)

        else:
            log(f"     ⚠️  Unknown verdict '{verdict}' — treating as hold")

    return state



# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def single_scan():
    now = datetime.now(timezone.utc)
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v13  ·  Three-Tier + News Monitor             ║")
    print(f"║  {now.strftime('%Y-%m-%d %H:%M UTC')}  |  T1 always | T2 daily | T3 weekly  ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    if not DDG_AVAILABLE:
        print("❌  ddgs not installed. Run: pip install ddgs")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    if should_send_daily_summary(state):
        telegram_daily_summary(state)

    # ── Step 0: News monitor ──────────────────────────────
    log("── Step 0: News monitor ─────────────────────────────────")
    headlines        = scan_news_feeds()
    news_flags       = haiku_flag_news(headlines, state) if headlines else []
    priority_markets = []

    if news_flags:
        t1_markets           = fetch_markets_for_tier(1)
        priority_markets, _  = find_markets_for_news(news_flags, t1_markets)

    # ── Step 1: Resolve ───────────────────────────────────
    log("── Resolve open trades ──────────────────────────────────")
    state = resolve_open_trades(state)

    # ── Step 1b: Two-strike reassessment brain (T2/T3) ───────
    if should_run_reassessment(state):
        log("── Reassessing T2/T3 positions (two-strike brain) ──────")
        state = run_trade_reassessments(state)
        state["last_reassessment"] = now.isoformat()
    else:
        log("── Reassessment: skipped (< 3 days since last) ──────────")

    # ── Tier 1 — always ──────────────────────────────────
    log("── Tier 1: Short-term (1-7 days) ────────────────────────")
    state = run_tier(1, state, priority_markets=priority_markets)

    # ── Tier 2 — once daily ───────────────────────────────
    if should_run_tier2(state):
        log("── Tier 2: Medium-term (8-30 days) ──────────────────────")
        state = run_tier(2, state)
        state["last_tier2_scan"] = now.strftime("%Y-%m-%d")
    else:
        log("── Tier 2: Skipped (already ran today) ──────────────────")



    log("── Save ──────────────────────────────────────────────────")
    save_state(state)

    # Backfill reflections for any closed trades that don't have one yet
    os.makedirs(REFLECTIONS_DIR, exist_ok=True)
    existing = set(os.listdir(REFLECTIONS_DIR))
    for t in state["trades"]:
        if t["status"] == "closed":
            fname = f"{t.get('id','unknown')}_{t.get('category','other')}_{'WON' if t.get('won') else 'LOST'}.md"
            if fname not in existing:
                write_trade_reflection(t, state)
                existing.add(fname)

    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v13  ·  Three-Tier + News Monitor             ║")
    print(f"║  Interval: {SCAN_INTERVAL_MINS}min | T1 every | T2 daily | T3 weekly  ║")
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
        single_scan()
    else:
        run_loop()
