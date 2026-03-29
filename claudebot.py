"""
╔══════════════════════════════════════════════════════════╗
║         CLAUDEBOT v10 — Polymarket Paper Trader          ║
║                                                          ║
║  Research pipeline:                                      ║
║  1. Python searches DuckDuckGo for each market           ║
║     → guaranteed real data, no AI deciding to skip       ║
║  2. Haiku reads raw results → writes clean research brief ║
║  3. Opus reads briefs → makes trading decisions          ║
║                                                          ║
║  SETUP:  pip install anthropic requests ddgs             ║
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
MAX_BET_PCT        = 8.0
MIN_CONFIDENCE     = 60
MIN_EDGE_PCT       = 7
MAX_OPEN_POSITIONS = 10
MAX_HOLD_DAYS      = 7
MIN_HOLD_HOURS     = 2
DAILY_LOSS_LIMIT   = 200.00
SCAN_INTERVAL_MINS = 180
SCREENER_TOP_N     = 10
LOG_FILE           = "claudebot_log.json"

KELLY_TIERS = [
    {"min_conf": 90, "kelly_fraction": 1.0, "max_pct": 8.0},
    {"min_conf": 75, "kelly_fraction": 0.5, "max_pct": 5.0},
    {"min_conf": 60, "kelly_fraction": 0.25, "max_pct": 3.0},
]

MAX_PER_CATEGORY = 2


# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


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


def telegram_new_trade(trade, state):
    kelly_pct  = trade.get("kelly_tier", "").split("(")[-1].replace(")", "").strip()
    profit_pct = round((trade["potential_return"] / trade["stake"] - 1) * 100, 1) if trade["stake"] else 0
    pos_emoji  = "✅" if trade["position"] == "YES" else "🔴"
    edge       = abs(trade.get("true_prob", 0) - trade.get("market_prob", 0))
    roi        = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    closed_t   = [t for t in state["trades"] if t["status"] == "closed"]
    won_ct     = sum(1 for t in closed_t if t.get("won"))
    lost_ct    = len(closed_t) - won_ct

    # ── PUBLIC — clean signal, no financials ──────────────
    public_msg = (
        f"🤖 <b>CLAUDEBOT SIGNAL</b>\n"
        f"{'─' * 30}\n"
        f"<b>{trade['market']}</b>\n\n"
        f"{pos_emoji} <b>BUY {trade['position']}</b>\n\n"
        f"📌 Entry: <b>{trade['entry_price']}¢</b>\n"
        f"🎯 Confidence: <b>{trade['confidence']}%</b>\n"
        f"📐 Sizing: <b>{kelly_pct}</b>\n"
        f"💹 Potential profit: <b>+{profit_pct}%</b>\n"
        f"⏰ Closing: <b>{trade['closes'][:10]}</b>\n\n"
        f"{'─' * 30}\n"
        f"🔍 <b>Reasoning</b>\n"
        f"<i>{trade.get('research_summary', '')}</i>\n\n"
        f"⚠️ <b>Bear case</b>\n"
        f"<i>{trade.get('bear_case', '')}</i>"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

    # ── PRIVATE — full details with bankroll ──────────────
    if TELEGRAM_PERSONAL_ID:
        tier_emoji = {"full": "🔥", "half": "💪", "quarter": "📊"}.get(
            trade.get("kelly_tier", "").split("-")[0], "📊"
        )
        private_msg = (
            f"🤖 <b>CLAUDEBOT — PRIVATE</b>\n"
            f"{'─' * 30}\n"
            f"<b>{trade['market']}</b>\n\n"
            f"{pos_emoji} <b>BUY {trade['position']}</b>\n\n"
            f"💰 Entry: <b>{trade['entry_price']}¢</b>  |  True prob: <b>{trade['true_prob']}%</b>\n"
            f"📈 Edge: <b>+{edge}%</b>  |  Confidence: <b>{trade['confidence']}%</b>\n"
            f"{tier_emoji} {trade.get('kelly_tier', '')}\n"
            f"💵 Stake: <b>${trade['stake']:.2f}</b>  →  Win: <b>${trade['potential_return']:.2f}</b>\n"
            f"💹 Profit if wins: <b>+{profit_pct}%</b>\n"
            f"⏰ Closes: <b>{trade['closes'][:10]}</b> ({trade['closes_in_days']:.1f}d)\n\n"
            f"🔍 <i>{trade.get('research_summary', '')}</i>\n\n"
            f"⚠️ Bear: <i>{trade.get('bear_case', '')}</i>\n"
            f"{'─' * 30}\n"
            f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
            f"📊 Record: <b>{won_ct}W / {lost_ct}L</b>"
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
    wr      = (won_ct / len(closed) * 100) if closed else 0

    public_msg = (
        f"{emoji} <b>TRADE RESOLVED — {result}</b>\n"
        f"{'─' * 30}\n"
        f"<b>{trade['market']}</b>\n"
        f"Position: <b>{trade['position']} @ {trade['entry_price']}¢</b>\n\n"
        f"💰 P&L: <b>{pnl_str}</b>\n"
        f"{'─' * 30}\n"
        f"📊 Record: <b>{won_ct}W / {lost_ct}L — {wr:.0f}% win rate</b>"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

    if TELEGRAM_PERSONAL_ID:
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
        pos_public  += f"  • {t['position']} | {closes_str} | {t['market'][:45]}\n"
        pos_private += f"  • {t['position']} | ${t['stake']:.2f} | {closes_str} | {t['market'][:45]}\n"

    public_msg = (
        f"📅 <b>CLAUDEBOT DAILY SUMMARY</b>\n"
        f"{'─' * 30}\n"
        f"📊 Record: <b>{len(won_t)}W / {len(lost_t)}L — {win_rate:.0f}% win rate</b>\n"
        f"{'─' * 30}\n"
        f"📋 Open Positions ({len(open_t)}):\n"
        + (pos_public if pos_public else "  None\n") +
        f"{'─' * 30}\n"
        f"⏰ Next scan in ~3 hours"
    )
    send_telegram(public_msg, TELEGRAM_CHANNEL_ID)

    if TELEGRAM_PERSONAL_ID:
        private_msg = (
            f"📅 <b>CLAUDEBOT DAILY SUMMARY — PRIVATE</b>\n"
            f"{'─' * 30}\n"
            f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b>\n"
            f"📈 ROI: <b>{roi:+.1f}%</b>  |  Realized P&L: <b>${realized:+.2f}</b>\n"
            f"📊 Record: <b>{len(won_t)}W / {len(lost_t)}L — {win_rate:.0f}% win rate</b>\n"
            f"🔄 Scans: <b>{state.get('scan_count', 0)}</b>\n"
            f"{'─' * 30}\n"
            f"📋 Open Positions ({len(open_t)}/{MAX_OPEN_POSITIONS}):\n"
            + (pos_private if pos_private else "  None\n") +
            f"{'─' * 30}\n"
            f"⏰ Next scan in ~3 hours"
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
        log("📅 New day — daily loss reset")
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
        log(f"  ✅ WON  +${trade['realized_pnl']:.2f}  |  {trade['market'][:60]}")
    else:
        trade["realized_pnl"] = round(-trade["stake"], 2)
        state["daily_loss"]   = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST -${trade['stake']:.2f}  |  {trade['market'][:60]}")
    log(f"         Bankroll now: ${state['bankroll']:.2f}")
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

def get_demo_markets():
    now = datetime.now(timezone.utc)
    return [
        {"id": "d001", "question": "Will Bitcoin close above $85,000 today?",               "yes": 52, "volume": 1200000, "closes_in_days": 0.5,  "closes": (now + timedelta(hours=12)).isoformat()},
        {"id": "d002", "question": "Will the S&P 500 close up on Friday?",                  "yes": 48, "volume":  890000, "closes_in_days": 2.0,  "closes": (now + timedelta(days=2)).isoformat()},
        {"id": "d003", "question": "Will Ethereum be above $2,000 by end of week?",         "yes": 61, "volume":  740000, "closes_in_days": 4.0,  "closes": (now + timedelta(days=4)).isoformat()},
        {"id": "d004", "question": "Will the Fed make any emergency statement this week?",   "yes":  8, "volume":  430000, "closes_in_days": 5.0,  "closes": (now + timedelta(days=5)).isoformat()},
        {"id": "d005", "question": "Will BTC dominance exceed 55% by end of week?",         "yes": 44, "volume":  320000, "closes_in_days": 6.0,  "closes": (now + timedelta(days=6)).isoformat()},
        {"id": "d006", "question": "Will there be a major crypto exchange hack this week?",  "yes":  6, "volume":  210000, "closes_in_days": 7.0,  "closes": (now + timedelta(days=7)).isoformat()},
    ]


def fetch_markets():
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=100&order=volume&ascending=false",
            timeout=12
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log(f"⚠️  Polymarket unavailable ({e}) — using demo markets")
        return get_demo_markets()

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
            prices = json.loads(m["outcomePrices"])
            yes    = round(float(prices[0]) * 100)
        except Exception:
            continue
        if yes >= 95 or yes <= 5:
            continue
        markets.append({
            "id":             str(m.get("id", "")),
            "question":       m["question"],
            "yes":            yes,
            "volume":         float(m.get("volume", 0)),
            "category":       (m.get("tags") or [{}])[0].get("label", "general"),
            "closes":         end_dt.isoformat(),
            "closes_in_days": round(cid, 2),
            "clobTokenIds":   m.get("clobTokenIds", []),
        })

    markets.sort(key=lambda x: x["closes_in_days"])
    log(f"✅ {len(markets)} markets within {MAX_HOLD_DAYS}d window (skipped {skipped})")
    return markets


# ─────────────────────────────────────────────────────────
#  DIVERSIFICATION
# ─────────────────────────────────────────────────────────

def get_category(question):
    q = question.lower()
    if any(k in q for k in ["temperature", "weather", "rain", "snow", "°c", "°f",
                              "celsius", "fahrenheit", "precipitation", "humid"]):
        return "weather"
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "crypto",
                              "solana", "bnb", "xrp", "defi", "sol"]):
        return "crypto"
    if any(k in q for k in ["nba", "nfl", "mlb", "nhl", "soccer", "football",
                              "tennis", "golf", "points", "goals", "score", "match",
                              "game", "fc ", " united", "spread", "o/u", "rebounds",
                              "assists", "esport", "valorant", "counter-strike", "dota",
                              "leverkusen", "barcelona", "atletico", "flyers", "capitals",
                              "lakers", "celtics", "west brom", "wrexham", "jokic",
                              "harris", "molcan", "kills", "bestia", "iceho", "rockets",
                              "ahl:", "lol:", "fluxo", "leviatan", "honduras", "wd fc",
                              "xspark", "xcrew", "prodigy", "rune eaters", "atputies",
                              "sinners", "jijiehao", "monchengladbach", "almeria",
                              "real sociedad", "spain", "georgia", "lithuania"]):
        return "sports"
    if any(k in q for k in ["president", "election", "senate", "congress", "vote",
                              "government", "minister", "party", "trump", "biden",
                              "democrat", "republican", "musk", "tweets", "elon",
                              "policy", "tariff", "doge"]):
        return "politics"
    if any(k in q for k in ["fed", "rate", "inflation", "gdp", "recession",
                              "unemployment", "economy", "s&p", "nasdaq", "dow",
                              "jobs", "robinhood", "hood", "amazon", "amzn",
                              "stock", "market cap", "earnings"]):
        return "economics"
    if any(k in q for k in ["war", "military", "attack", "ceasefire", "hezbollah",
                              "ukraine", "russia", "israel", "hamas", "conflict",
                              "kyiv", "kostyantynivka", "borova", "troops", "nato"]):
        return "geopolitics"
    return "other"


def category_slots_available(category, state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    cat_count   = sum(
        1 for t in open_trades
        if (t.get("category") or get_category(t.get("market", ""))) == category
    )
    return (MAX_PER_CATEGORY - cat_count) > 0


# ─────────────────────────────────────────────────────────
#  STAGE 1 — HAIKU SCREENER
# ─────────────────────────────────────────────────────────

def haiku_screen(markets, state):
    if not markets:
        return []

    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    open_ids   = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    candidates = [m for m in markets if m["id"] not in open_ids]

    if not candidates:
        return []

    mkt_list = "\n".join(
        f'ID:{m["id"]} | {m["closes_in_days"]:.1f}d | YES={m["yes"]}¢ | Vol=${m["volume"]:,.0f} | "{m["question"]}"'
        for m in candidates
    )

    prompt = (
        f"You are a fast prediction market screener. Today is {datetime.now(timezone.utc).strftime('%Y-%m-%d')}.\n\n"
        f"Score each market from 1-10 for MISPRICING POTENTIAL.\n"
        f"High score = odds likely wrong, real edge exists.\n"
        f"Low score = efficient market, skip it.\n\n"
        f"Scoring factors:\n"
        f"- Low volume (<$500k) = less efficient = higher score\n"
        f"- Near 50/50 odds on genuinely uncertain events = potential edge\n"
        f"- Time-sensitive events where news could shift things\n"
        f"- Markets where crowd bias (fear/greed) is likely\n"
        f"- Exact score / narrow range markets = harder to price = more edge\n\n"
        f"Markets:\n{mkt_list}\n\n"
        f"Return ONLY a JSON array, no other text:\n"
        f'[{{"id":"market_id","score":7}}, ...]'
    )

    log(f"⚡ Haiku screening {len(candidates)} markets...")

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        if not raw.startswith("["):
            match = re.search(r'\[[\s\S]*\]', raw)
            raw = match.group(0) if match else "[]"

        scores      = json.loads(raw)
        scores.sort(key=lambda x: x.get("score", 0), reverse=True)
        top_ids     = [s["id"] for s in scores[:SCREENER_TOP_N]]
        top_markets = [m for m in candidates if m["id"] in top_ids]

        log(f"⚡ Haiku selected top {len(top_markets)} markets:")
        for s in scores[:SCREENER_TOP_N]:
            mkt = next((m for m in candidates if m["id"] == s["id"]), None)
            if mkt:
                log(f"   Score {s['score']}/10 — {mkt['question'][:60]}")

        return top_markets

    except Exception as e:
        log(f"⚠️  Haiku screener error ({e}) — passing all to research")
        return candidates[:SCREENER_TOP_N]


# ─────────────────────────────────────────────────────────
#  STAGE 2a — SMART SEARCH QUERY BUILDER
# ─────────────────────────────────────────────────────────

def build_search_query(market):
    """
    Builds a smart, targeted search query from a market question.
    Different categories need different query strategies to find results.
    """
    q   = market["question"]
    cat = get_category(q)
    now = datetime.now(timezone.utc)
    month_year = now.strftime("%B %Y")
    date_full  = now.strftime("%B %d %Y")

    # Strip prediction market boilerplate
    q_clean = re.sub(r'^Will\s+', '', q, flags=re.IGNORECASE)
    q_clean = re.sub(r'\?$', '', q_clean).strip()

    if cat == "weather":
        # Extract city name — usually first proper noun or city mentioned
        # Search for the actual forecast, not the prediction market
        city_match = re.search(r'in ([A-Z][a-zA-Z\s]+?)(?:\s+be|\s+have|\s+reach|\s+exceed)', q)
        city = city_match.group(1).strip() if city_match else q_clean
        return f"{city} weather forecast high temperature {date_full}"

    elif cat == "sports":
        # Strip market-specific syntax for cleaner results
        q_clean = re.sub(r':\s*O/U\s*[\d.]+', '', q_clean)
        q_clean = re.sub(r'Spread:\s*', '', q_clean)
        q_clean = re.sub(r'\(BO[123]\).*', '', q_clean)
        q_clean = re.sub(r'\s*[-–]\s*\w[\w\s]+(?:Championship|League|Series|Cup|Season).*', '', q_clean)
        q_clean = re.sub(r'AHL:\s*|LoL:\s*|Dota 2:\s*|Valorant:\s*|Counter-Strike:\s*', '', q_clean)
        q_clean = q_clean.strip()
        return f"{q_clean} match result odds {month_year}"

    elif cat == "economics":
        # Remove narrow range brackets for broader search
        q_clean = re.sub(r'between \$[\d,]+ and \$[\d,]+', '', q_clean)
        q_clean = re.sub(r'at \$[\d,]+[-–]\$[\d,]+', '', q_clean)
        q_clean = q_clean.strip()
        return f"{q_clean} {month_year} forecast"

    elif cat == "crypto":
        # Extract coin and get current price
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


# ─────────────────────────────────────────────────────────
#  STAGE 2b — PYTHON DDG SEARCH
# ─────────────────────────────────────────────────────────

def ddg_search(query, max_results=5):
    """Search DuckDuckGo and return raw results. Returns empty list on failure."""
    if not DDG_AVAILABLE:
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        log(f"     ⚠️  DDG error: {e}")
        return []


def search_market(market):
    """
    Search DuckDuckGo for a market using a smart query.
    Falls back to a simpler query if first attempt returns nothing.
    Returns (query_used, results_list).
    """
    query   = build_search_query(market)
    results = ddg_search(query)

    # If no results, try a simpler fallback query
    if not results:
        fallback = f"{market['question'][:80]} {datetime.now(timezone.utc).strftime('%B %Y')}"
        results  = ddg_search(fallback, max_results=3)
        if results:
            query = fallback

    log(f"     🔍 \"{query}\" → {len(results)} results")
    return query, results


# ─────────────────────────────────────────────────────────
#  STAGE 2c — HAIKU INTERPRETER
#  Reads raw DDG results and writes a clean research brief
# ─────────────────────────────────────────────────────────

def haiku_interpret(client, market, query, raw_results):
    """
    Haiku reads raw search results and distills them into a
    concise, factual research brief for Opus to use.
    """
    if not raw_results:
        return (
            f"No search results found for this market. "
            f"Market: {market['question']} | Odds: YES={market['yes']}¢ NO={100-market['yes']}¢. "
            f"Unable to find current data — proceed with caution."
        )

    results_txt = "\n\n".join(
        f"[{i+1}] Title: {r.get('title', 'N/A')}\n"
        f"    URL: {r.get('href', 'N/A')}\n"
        f"    Snippet: {r.get('body', 'N/A')}"
        for i, r in enumerate(raw_results[:5])
    )

    prompt = (
        f"You are a research analyst. Read these web search results and write a concise factual brief.\n\n"
        f"MARKET: \"{market['question']}\"\n"
        f"CURRENT ODDS: YES={market['yes']}¢ | NO={100-market['yes']}¢\n"
        f"CLOSES IN: {market['closes_in_days']:.1f} days ({market['closes'][:10]})\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n\n"
        f"SEARCH RESULTS:\n{results_txt}\n\n"
        f"Write a 3-5 sentence brief that covers:\n"
        f"1. The most relevant current facts from the results\n"
        f"2. What the data implies about whether YES or NO is likely\n"
        f"3. Any specific numbers, prices, forecasts or dates from the results\n\n"
        f"Rules:\n"
        f"- Only use information from the search results — do not add outside knowledge\n"
        f"- Be specific and factual, not vague\n"
        f"- If results are irrelevant or unhelpful, say so clearly\n"
        f"- Keep it under 5 sentences"
    )

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log(f"     ⚠️  Haiku interpret error: {e}")
        return f"Research interpretation failed. Raw data: {results_txt[:200]}"


# ─────────────────────────────────────────────────────────
#  STAGE 2 — FULL RESEARCH PIPELINE
# ─────────────────────────────────────────────────────────

def research_all_markets(markets):
    """
    For each market:
    1. Python searches DuckDuckGo with a smart query (guaranteed)
    2. Haiku interprets the raw results into a clean brief
    Returns dict of market_id -> research brief string
    """
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    research = {}

    log(f"🔬 Researching {len(markets)} markets...")

    for i, market in enumerate(markets):
        log(f"  [{i+1}/{len(markets)}] {market['question'][:65]}")

        # Step 1: Python searches DDG
        query, raw_results = search_market(market)

        # Step 2: Haiku interprets results
        brief = haiku_interpret(client, market, query, raw_results)
        log(f"     📋 {brief[:110]}...")

        research[market["id"]] = brief

    return research


# ─────────────────────────────────────────────────────────
#  KELLY SIZING  ·  confidence-tiered
#  Always called with the probability of winning the bet.
#  Caller inverts for NO bets before calling.
# ─────────────────────────────────────────────────────────

def kelly_size(win_prob, market_win_prob, bankroll, closes_in_days=7.0, confidence=70):
    """
    win_prob:        your estimated probability of winning this bet (0-100)
    market_win_prob: the market's implied probability of winning (0-100)
    For YES bets: pass true_prob and market_prob directly
    For NO bets:  pass (100-true_prob) and (100-market_prob)
    """
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

    # Pick fraction and cap based on confidence tier
    fraction = 0.25
    cap_pct  = 3.0
    for tier in KELLY_TIERS:
        if confidence >= tier["min_conf"]:
            fraction = tier["kelly_fraction"]
            cap_pct  = tier["max_pct"]
            break

    sized = full_kelly * fraction

    # Short-term discount — higher variance near expiry
    if closes_in_days <= 1.0:
        sized *= 0.65
    elif closes_in_days <= 2.0:
        sized *= 0.80

    capped = min(max(sized, 0.0), cap_pct / 100)
    return round(capped * bankroll, 2)


def get_tier_name(confidence):
    for tier in KELLY_TIERS:
        if confidence >= tier["min_conf"]:
            fname = {1.0: "full", 0.5: "half", 0.25: "quarter"}.get(tier["kelly_fraction"], "?")
            return f"{fname}-Kelly ({tier['max_pct']}%)"
    return "quarter-Kelly (3%)"


# ─────────────────────────────────────────────────────────
#  STAGE 3 — OPUS 4.6 ANALYST
#  Receives pre-researched briefs and makes trading decisions
# ─────────────────────────────────────────────────────────

def opus_analyze(markets, research, state):
    if not markets:
        return []

    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    available = MAX_OPEN_POSITIONS - sum(1 for t in state["trades"] if t["status"] == "open")

    if available <= 0:
        log("Max positions reached")
        return []

    closed = [t for t in state["trades"] if t["status"] == "closed"]
    won    = [t for t in closed if t.get("won")]
    lost   = [t for t in closed if not t.get("won")]

    history_ctx = ""
    if closed:
        win_rate    = len(won) / len(closed) * 100
        history_ctx = (
            f"\nYOUR TRACK RECORD: {win_rate:.0f}% win rate "
            f"({len(won)}W / {len(lost)}L from {len(closed)} trades)\n\n"
            f"Recent closed trades:\n"
        )
        for t in closed[-10:]:
            result       = "WON" if t.get("won") else "LOST"
            edge         = abs(t.get("true_prob", 0) - t.get("market_prob", 0))
            history_ctx += (
                f"  {result} | {t['position']} @ {t['entry_price']}¢ | "
                f"edge {edge}% | conf {t.get('confidence', 0)}% | {t['market'][:60]}\n"
            )
        if lost:
            history_ctx += "\nPatterns in losses — avoid repeating:\n"
            for t in lost[-5:]:
                history_ctx += f"  LOST {t['position']} — bear: {t.get('bear_case', 'unknown')[:80]}\n"

    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    open_ctx    = ""
    if open_trades:
        open_ctx = "\nCURRENT OPEN POSITIONS (do NOT re-recommend any of these):\n"
        open_ctx += "\n".join(
            f"  {t['position']} @ {t['entry_price']}¢ | "
            f"cat:{t.get('category', get_category(t.get('market', '')))} | "
            f"{t['market'][:70]}"
            for t in open_trades
        )
        cat_counts = {}
        for t in open_trades:
            cat = t.get("category") or get_category(t.get("market", ""))
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        open_ctx += f"\n\nCATEGORY COUNTS: {cat_counts} | MAX PER CATEGORY: {MAX_PER_CATEGORY}"

    # Build market list with embedded research briefs
    mkt_sections = []
    for m in markets:
        brief = research.get(m["id"], "No research available.")
        section = (
            f"─── MARKET ID:{m['id']} ───\n"
            f"Question: \"{m['question']}\"\n"
            f"Odds: YES={m['yes']}¢ | NO={100-m['yes']}¢ | "
            f"Vol=${m['volume']:,.0f} | Closes {m['closes'][:10]} ({m['closes_in_days']:.1f}d)\n"
            f"Live Research: {brief}\n"
        )
        mkt_sections.append(section)

    mkt_list = "\n".join(mkt_sections)

    prompt = (
        f"You are an expert algorithmic prediction market trader.\n\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n"
        f"BANKROLL: ${state['bankroll']:.2f} | AVAILABLE SLOTS: {available} | MAX BET: {MAX_BET_PCT}%\n"
        f"MIN EDGE: {MIN_EDGE_PCT}% | MIN CONFIDENCE: {MIN_CONFIDENCE}%\n"
        f"{history_ctx}\n"
        f"{open_ctx}\n\n"
        f"MARKETS WITH LIVE RESEARCH (all close within {MAX_HOLD_DAYS} days):\n"
        f"{mkt_list}\n\n"
        f"The 'Live Research' above was gathered from real web searches seconds ago.\n"
        f"Base your probability estimates on this research. If research is unavailable\n"
        f"or irrelevant for a market, do NOT recommend it — insufficient data.\n\n"
        f"CRITICAL — HOW TO REPORT PROBABILITIES:\n"
        f"Always report true_prob and market_prob as the probability of YES (0-100).\n"
        f"Do NOT adjust these for the position direction — always report the YES probability.\n"
        f"Examples:\n"
        f"  Market says YES=27¢, you think YES has 10% true chance → true_prob=10, market_prob=27, position=NO\n"
        f"  Market says YES=45¢, you think YES has 65% true chance → true_prob=65, market_prob=45, position=YES\n"
        f"The system automatically inverts probabilities for NO bets in Kelly sizing.\n\n"
        f"CONFIDENCE GUIDE — be rigorous:\n"
        f"  90-99%: Near-certain. Research shows overwhelming evidence.\n"
        f"  75-89%: High confidence. Strong evidence pointing clearly one way.\n"
        f"  60-74%: Moderate confidence. Edge exists but meaningful uncertainty.\n\n"
        f"DIVERSIFICATION RULE:\n"
        f"  Max {MAX_PER_CATEGORY} open positions per category at any time.\n"
        f"  Do NOT recommend a 3rd position in a category already at {MAX_PER_CATEGORY}.\n\n"
        f"Return ONLY a valid JSON array, no preamble, no explanation:\n"
        f"[\n"
        f"  {{\n"
        f'    "market_id": "exact ID from list above",\n'
        f'    "market": "exact question text",\n'
        f'    "position": "YES or NO",\n'
        f'    "market_prob": 27,\n'
        f'    "true_prob": 10,\n'
        f'    "confidence": 88,\n'
        f'    "category": "geopolitics",\n'
        f'    "research_summary": "2-3 sentences citing specific facts from the live research",\n'
        f'    "key_factors": ["specific factor 1", "specific factor 2", "specific factor 3"],\n'
        f'    "bear_case": "the main reason this trade could go wrong"\n'
        f"  }}\n"
        f"]\n\n"
        f"If no market has genuine edge >= {MIN_EDGE_PCT}% after reviewing the research, return: []"
    )

    log(f"🧠 Opus 4.6 analyzing {len(markets)} researched markets...")

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
                    log(f"  💭 Opus thought for {len(thinking_txt)} chars")
                elif block.type == "text":
                    full_text += block.text

        log(f"  📊 Thinking: {'yes' if thinking_txt else 'no'}")

        raw = full_text.strip().replace("```json", "").replace("```", "").strip()
        if not raw.startswith("["):
            match = re.search(r'\[[\s\S]*\]', raw)
            raw = match.group(0) if match else "[]"

        recs  = json.loads(raw)
        valid = []

        for r in recs:
            edge = abs(r.get("true_prob", 0) - r.get("market_prob", 0))
            if edge < MIN_EDGE_PCT:
                log(f"  ⏭  Edge {edge}% too low — {r.get('market', '')[:50]}")
                continue
            if r.get("confidence", 0) < MIN_CONFIDENCE:
                log(f"  ⏭  Conf {r.get('confidence')}% too low — {r.get('market', '')[:50]}")
                continue
            if not any(m["id"] == r.get("market_id") for m in markets):
                log(f"  ⚠️  Unknown market_id {r.get('market_id')} — skip")
                continue
            cat = r.get("category") or get_category(r.get("market", ""))
            if not category_slots_available(cat, state):
                log(f"  ⏭  Category '{cat}' full — {r.get('market', '')[:50]}")
                continue
            valid.append(r)

        log(f"🤖 Opus recommends {len(valid)} trade(s)")
        for r in valid:
            edge = abs(r.get("true_prob", 0) - r.get("market_prob", 0))
            cat  = r.get("category") or get_category(r.get("market", ""))
            tier = get_tier_name(r.get("confidence", 0))
            log(f"  📋 BUY {r['position']} | {cat} | {tier} | YES_mkt={r['market_prob']}% | YES_true={r['true_prob']}% | edge {edge}% | conf {r['confidence']}%")
            log(f"     {r['market'][:70]}")
            log(f"     {r.get('research_summary', '')[:120]}")
            log(f"     Bear: {r.get('bear_case', '')[:80]}")

        return valid

    except Exception as e:
        log(f"❌ Opus error: {e}")
        return []


# ─────────────────────────────────────────────────────────
#  TRADE EXECUTION
# ─────────────────────────────────────────────────────────

def place_paper_trade(rec, markets, state):
    conf = rec.get("confidence", 0)
    if conf < MIN_CONFIDENCE:
        log(f"  ⏭  Conf {conf}% < {MIN_CONFIDENCE}%")
        return state

    if rec.get("market_id") in {t["market_id"] for t in state["trades"] if t["status"] == "open"}:
        log(f"  ⏭  Already open in this market")
        return state

    if sum(1 for t in state["trades"] if t["status"] == "open") >= MAX_OPEN_POSITIONS:
        log(f"  ⏭  Max positions ({MAX_OPEN_POSITIONS}) reached")
        return state

    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit hit")
        return state

    cat = rec.get("category") or get_category(rec.get("market", ""))
    if not category_slots_available(cat, state):
        log(f"  ⏭  Category '{cat}' already at max ({MAX_PER_CATEGORY})")
        return state

    mkt = next((m for m in markets if m["id"] == rec["market_id"]), None)
    if not mkt:
        log(f"  ⏭  Market {rec['market_id']} not found in fetched list")
        return state

    end_dt = parse_utc(mkt["closes"])
    if not end_dt:
        log(f"  ⏭  Cannot parse close date for market {rec['market_id']}")
        return state

    cid = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    if cid < (MIN_HOLD_HOURS / 24) or cid > MAX_HOLD_DAYS:
        log(f"  ⏭  Closes in {cid:.1f}d — outside [{MIN_HOLD_HOURS}h, {MAX_HOLD_DAYS}d] window")
        return state

    # Opus always reports YES probabilities.
    # For NO bets: invert so Kelly sees the probability of winning.
    yes_true   = rec["true_prob"]
    yes_market = rec["market_prob"]

    if rec["position"] == "NO":
        kelly_win_prob    = 100 - yes_true
        kelly_market_prob = 100 - yes_market
    else:
        kelly_win_prob    = yes_true
        kelly_market_prob = yes_market

    log(f"  📐 Kelly: position={rec['position']} | win_prob={kelly_win_prob}% | market_win_prob={kelly_market_prob}%")

    stake = kelly_size(
        win_prob        = kelly_win_prob,
        market_win_prob = kelly_market_prob,
        bankroll        = state["bankroll"],
        closes_in_days  = cid,
        confidence      = conf
    )

    if stake < 1.00:
        log(f"  ⏭  Stake ${stake:.2f} too small — insufficient edge for Kelly to size")
        return state

    tier   = get_tier_name(conf)
    # Entry price = cost per share of the position we're taking
    entry  = yes_market if rec["position"] == "YES" else (100 - yes_market)
    payout = round(stake * 100 / entry, 2)
    profit = round(payout - stake, 2)

    trade = {
        "id":               f"T{int(time.time())}",
        "market_id":        mkt["id"],
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
        "closes_in_days":   round(cid, 2),
        "closes":           end_dt.isoformat(),
        "research_summary": rec.get("research_summary", ""),
        "key_factors":      rec.get("key_factors", []),
        "bear_case":        rec.get("bear_case", ""),
        "kelly_tier":       tier,
        "status":           "open",
        "placed_at":        datetime.now(timezone.utc).isoformat(),
        "paper":            True,
        "model":            ANALYST_MODEL,
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  ✅ BET PLACED — {trade['position']} @ {entry}¢  [{tier}]")
    log(f"     {trade['market'][:70]}")
    log(f"     Category: {cat} | Closes {end_dt.strftime('%b %d')} ({cid:.1f}d)")
    log(f"     Stake ${stake:.2f} | Win ${payout:.2f} | Edge +${profit:.2f} | Conf {conf}%")
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
    print("  CLAUDEBOT v10  ·  DDG + Haiku + Opus 4.6 · Telegram")
    print("═" * 65)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Open           {len(open_t)} / {MAX_OPEN_POSITIONS}")
    print(f"  Closed         {len(closed_t)}  ({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Total Scans    {state.get('scan_count', 0)}")
    print(f"  DDG Search     {'✅ available' if DDG_AVAILABLE else '❌ not installed (pip install ddgs)'}")
    print(f"  Telegram       {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    print("═" * 65)

    if open_t:
        print("\n  OPEN POSITIONS:")
        for t in open_t:
            close_dt   = parse_utc(t.get("closes", ""))
            cid        = round(days_until(close_dt), 1) if close_dt else "?"
            closes_str = close_dt.strftime("%b %d") if close_dt else "?"
            cat        = t.get("category", get_category(t.get("market", "")))
            tier       = t.get("kelly_tier", "")
            print(f"  • {t['position']} | ${t['stake']:.2f} | {cat} | closes {closes_str} ({cid}d) | {t['market'][:45]}")
            if tier:
                print(f"    [{tier}] conf {t.get('confidence', 0)}%")
    print()


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def single_scan():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v10  ·  DDG Search + Haiku + Opus 4.6        ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  |  Screen→Search→Interpret→Analyse ║")
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

    log("── Step 1: Resolve open trades ──────────────────────────")
    state = resolve_open_trades(state)

    log("── Step 2: Fetch short-term markets ─────────────────────")
    markets = fetch_markets()

    if not markets:
        log(f"No markets within {MAX_HOLD_DAYS} days — nothing to do")
        save_state(state)
        print_portfolio(state)
        return

    log("── Step 3: Haiku fast screen ─────────────────────────────")
    top_markets = haiku_screen(markets, state)

    if not top_markets:
        log("No candidates after screening")
        save_state(state)
        print_portfolio(state)
        return

    log("── Step 4: DDG search + Haiku interpret (per market) ────")
    research = research_all_markets(top_markets)

    log("── Step 5: Opus 4.6 analysis ────────────────────────────")
    recs = opus_analyze(top_markets, research, state)

    log("── Step 6: Place trades ──────────────────────────────────")
    if not recs:
        log("No trades this scan")
    else:
        for rec in recs:
            state = place_paper_trade(rec, markets, state)

    log("── Step 7: Save ──────────────────────────────────────────")
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v10  ·  Continuous Mode                       ║")
    print(f"║  Interval: {SCAN_INTERVAL_MINS}min (3h) | 10 slots | DDG+Haiku+Opus  ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        return

    while True:
        try:
            single_scan()
            log(f"💤 Sleeping {SCAN_INTERVAL_MINS} min (3 hours)...\n")
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
