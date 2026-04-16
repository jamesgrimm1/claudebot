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

SCREENER_MODEL     = "claude-haiku-4-5-20251001"
ANALYST_MODEL      = "claude-opus-4-6"
PAPER_TRADING      = True
STARTING_BANKROLL  = 1000.00
DAILY_LOSS_LIMIT   = 150.00
SCAN_INTERVAL_MINS = 180
LOG_FILE           = "claudebot_log.json"

BLOCKED_CATEGORIES = {"sports"}

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
        "screener_top_n": 10,
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
            {"min_conf": 90, "fraction": 0.5,  "max_pct": 8.0},
            {"min_conf": 80, "fraction": 0.25, "max_pct": 5.0},
        ],
        "time_discount":  0.75,
        "screener_top_n": 8,
        "screener_max_per_cat": 2,
    },
    3: {
        "name":           "Long-term",
        "label":          "T3",
        "min_hold_days":  31,
        "max_hold_days":  180,
        "min_confidence": 90,
        "min_edge_pct":   25,
        "max_positions":  4,
        "fixed_pct":      3.0,
        "kelly":          [],
        "screener_top_n": 8,
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
    if any(k in q for k in ["fed", "rate", "inflation", "gdp", "recession",
                              "unemployment", "economy", "s&p", "nasdaq", "dow",
                              "jobs", "robinhood", "hood", "amazon", "amzn",
                              "stock", "market cap", "earnings", "ecb", "interest"]):
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
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    roi     = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    closed  = [t for t in state["trades"] if t["status"] == "closed"]
    won_ct  = sum(1 for t in closed if t.get("won"))
    lost_ct = len(closed) - won_ct

    public_msg = (
        f"{emoji} <b>TRADE RESOLVED — {result}</b>\n"
        f"{'─' * 30}\n"
        f"<b>{trade['market']}</b>\n"
        f"Position: <b>{trade['position']} @ {trade['entry_price']}¢</b>\n\n"
        f"💰 P&L: <b>{pnl_str}</b>"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

    if TELEGRAM_PERSONAL_ID:
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

    public_msg = (
        f"📅 <b>CLAUDEBOT DAILY SUMMARY</b>\n"
        f"{'─' * 30}\n"
        f"📊 Record: <b>{len(won_t)}W / {len(lost_t)}L — {win_rate:.0f}% win rate</b>\n"
        f"{'─' * 30}\n"
        f"📋 Open ({len(open_t)}):\n"
        + (pos_public if pos_public else "  None\n") +
        f"{'─' * 30}\n"
        f"⚡T1 short | 📅T2 medium | 🎯T3 long"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

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


def should_run_tier3(state):
    week = datetime.now(timezone.utc).strftime("%Y-W%W")
    return state.get("last_tier3_scan", "") != week


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
        "last_tier3_scan":    "",
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

def _settle(trade, won, state):
    trade["status"]       = "closed"
    trade["won"]          = won
    trade["resolved_at"]  = datetime.now(timezone.utc).isoformat()
    if won:
        payout                = trade.get("potential_return", trade["stake"])
        trade["realized_pnl"] = round(payout - trade["stake"], 2)
        state["bankroll"]     = round(state["bankroll"] + payout, 2)
        log(f"  ✅ WON  +${trade['realized_pnl']:.2f}  [{TIERS[trade.get('tier',1)]['label']}]  {trade['market'][:55]}")
    else:
        trade["realized_pnl"] = round(-trade["stake"], 2)
        state["daily_loss"]   = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST -${trade['stake']:.2f}  [{TIERS[trade.get('tier',1)]['label']}]  {trade['market'][:55]}")
    log(f"     Bankroll now: ${state['bankroll']:.2f}")
    telegram_trade_resolved(trade, state)


def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state
    log(f"🔍 Checking {len(open_trades)} open position(s)...")
    for trade in open_trades:
        market_id = trade.get("market_id", "")
        if market_id and not market_id.startswith("d0"):
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
                prices    = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
                yes_price = float(prices[0])
                no_price  = float(prices[1])
                won = (yes_price >= 0.99) if trade["position"] == "YES" else (no_price >= 0.99)
                _settle(trade, won, state)
            except Exception as e:
                log(f"  ⚠️  Could not check {market_id}: {e}")
        else:
            close_dt = parse_utc(trade.get("closes"))
            if close_dt and datetime.now(timezone.utc) > close_dt:
                import random
                _settle(trade, random.random() > 0.5, state)
    return state


# ─────────────────────────────────────────────────────────
#  MARKET FETCHING
# ─────────────────────────────────────────────────────────

def fetch_markets_for_tier(tier_num):
    tcfg     = TIERS[tier_num]
    min_days = tcfg.get("min_hold_days", tcfg.get("min_hold_hours", 2) / 24)
    max_days = tcfg["max_hold_days"]

    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=200&order=volume&ascending=false",
            timeout=12
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log(f"⚠️  Polymarket unavailable ({e})")
        return []

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
        q_lower = m["question"].lower()
        if any(k in q_lower for k in ["up or down", "odd or even", "odd/even", "total kills"]):
            skipped += 1
            continue
        if cid < (1 / 24):
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


def category_slots_available(category, state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    cat_count   = sum(
        1 for t in open_trades
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
            "when geopolitics, economics, or health markets also have edge."
        ),
        2: (
            "Prioritise: structural mispricings over 8-30 days. Economic trajectories, "
            "political situations with known timelines, health outbreak trajectories. "
            "Pick diverse categories."
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


def research_all_markets(markets):
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    research = {}
    log(f"🔬 Researching {len(markets)} markets...")
    for i, market in enumerate(markets):
        log(f"  [{i+1}/{len(markets)}] {market['question'][:65]}")
        query, raw = search_market(market)
        brief = haiku_interpret(client, market, query, raw)
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
        open_ctx += f"\nCATEGORY COUNTS: {cat_counts} | MAX: {MAX_PER_CATEGORY}"

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
        f"     or explicit official forward guidance, not 'Bank X thinks Y might happen'\n\n"
        f"0 trades beats a bad trade.\n\n"
        f"PROBABILITIES: report as YES probability (0-100).\n"
        f"CONFIDENCE: ≥{tcfg['min_confidence']}% required.\n"
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
        if not category_slots_available(cat, state):
            log(f"  ⏭  '{cat}' full — {r.get('market','')[:50]}")
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
    if not category_slots_available(cat, state):
        log(f"  ⏭  '{cat}' full")
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

    for tn in [1, 2, 3]:
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

    # ── Tier 3 — once weekly ──────────────────────────────
    if should_run_tier3(state):
        log("── Tier 3: Long-term (31-180 days) ──────────────────────")
        state = run_tier(3, state)
        state["last_tier3_scan"] = now.strftime("%Y-W%W")
    else:
        log("── Tier 3: Skipped (already ran this week) ──────────────")

    log("── Save ──────────────────────────────────────────────────")
    save_state(state)
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
