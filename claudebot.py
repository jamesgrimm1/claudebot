"""
╔══════════════════════════════════════════════════════════╗
║         CLAUDEBOT v7 — Polymarket Paper Trader           ║
║                                                          ║
║  New in v7:                                              ║
║  • Telegram signal channel integration                   ║
║  • Fires on every new bet placed                         ║
║  • Fires on every win/loss resolution                    ║
║  • Daily 9am UTC summary message                        ║
║  • Run frequency: every 3 hours                          ║
║                                                          ║
║  SETUP:  pip install anthropic requests                  ║
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

# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID  = os.environ.get("TELEGRAM_CHANNEL_ID", "")

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
SCAN_INTERVAL_MINS = 180       # 3 hours in continuous mode
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

def send_telegram(msg):
    """Send a message to the Telegram channel. Silently fails if not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id":    TELEGRAM_CHANNEL_ID,
            "text":       msg,
            "parse_mode": "HTML",
        }
        r = requests.post(url, json=data, timeout=10)
        if r.status_code == 200:
            log("📨 Telegram sent")
        else:
            log(f"⚠️  Telegram error: {r.status_code} — {r.text[:100]}")
    except Exception as e:
        log(f"⚠️  Telegram failed: {e}")


def telegram_new_trade(trade, state):
    tier_emoji = {"full": "🔥", "half": "💪", "quarter": "📊"}.get(
        trade.get("kelly_tier", "").split("-")[0], "📊"
    )
    edge = abs(trade.get("true_prob", 0) - trade.get("market_prob", 0))
    roi  = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100

    msg = (
        f"🤖 <b>CLAUDEBOT SIGNAL</b>\n"
        f"{'─' * 30}\n"
        f"{'✅' if trade['position'] == 'YES' else '❌'} <b>BUY {trade['position']}</b>\n"
        f"📋 {trade['market']}\n\n"
        f"💰 Entry: <b>{trade['entry_price']}¢</b>  |  True prob: <b>{trade['true_prob']}%</b>\n"
        f"📈 Edge: <b>+{edge}%</b>  |  Confidence: <b>{trade['confidence']}%</b>\n"
        f"{tier_emoji} Sizing: <b>{trade.get('kelly_tier', '')}</b>\n"
        f"💵 Stake: <b>${trade['stake']:.2f}</b>  →  Win: <b>${trade['potential_return']:.2f}</b>\n"
        f"⏰ Closes: <b>{trade['closes'][:10]}</b> ({trade['closes_in_days']:.1f}d)\n\n"
        f"🔍 <i>{trade.get('research_summary', '')}</i>\n\n"
        f"⚠️ Bear case: {trade.get('bear_case', '')}\n"
        f"{'─' * 30}\n"
        f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
        f"📊 Record: <b>{sum(1 for t in state['trades'] if t.get('won'))}W "
        f"/ {sum(1 for t in state['trades'] if t['status']=='closed' and not t.get('won'))}L</b>"
    )
    send_telegram(msg)


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

    msg = (
        f"{emoji} <b>TRADE RESOLVED — {result}</b>\n"
        f"{'─' * 30}\n"
        f"📋 {trade['market']}\n"
        f"Position: <b>{trade['position']} @ {trade['entry_price']}¢</b>\n\n"
        f"💰 P&L: <b>{pnl_str}</b>\n"
        f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
        f"{'─' * 30}\n"
        f"📊 Overall: <b>{won_ct}W / {lost_ct}L — {wr:.0f}% win rate</b>"
    )
    send_telegram(msg)


def telegram_daily_summary(state):
    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0
    roi      = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100

    positions_txt = ""
    for t in open_t:
        close_dt   = parse_utc(t.get("closes", ""))
        closes_str = close_dt.strftime("%b %d") if close_dt else "?"
        positions_txt += f"  • {t['position']} | ${t['stake']:.2f} | {closes_str} | {t['market'][:45]}\n"

    msg = (
        f"📅 <b>CLAUDEBOT DAILY SUMMARY</b>\n"
        f"{'─' * 30}\n"
        f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b>\n"
        f"📈 ROI: <b>{roi:+.1f}%</b>  |  Realized P&L: <b>${realized:+.2f}</b>\n"
        f"📊 Record: <b>{len(won_t)}W / {len(lost_t)}L — {win_rate:.0f}% win rate</b>\n"
        f"🔄 Total Scans: <b>{state.get('scan_count', 0)}</b>\n"
        f"{'─' * 30}\n"
        f"📋 Open Positions ({len(open_t)}/{MAX_OPEN_POSITIONS}):\n"
        f"{positions_txt if positions_txt else '  None\n'}"
        f"{'─' * 30}\n"
        f"⏰ Next scan in ~3 hours"
    )
    send_telegram(msg)


def should_send_daily_summary(state):
    """Send daily summary once per day around 9am UTC."""
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    last  = state.get("last_daily_summary", "")
    if last == today:
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

    prompt = f"""You are a fast prediction market screener. Today is {datetime.now(timezone.utc).strftime("%Y-%m-%d")}.

Score each market from 1-10 for MISPRICING POTENTIAL.
High score = odds likely wrong, real edge exists.
Low score = efficient market, skip it.

Factors: low volume (<$500k), near 50/50 odds, time-sensitive events,
crowd bias likely, breaking news not yet priced in, stats vs market mismatch.

Markets:
{mkt_list}

Return ONLY a JSON array, no other text:
[{{"id":"market_id","score":7}}, ...]"""

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
        log(f"⚠️  Haiku screener error ({e}) — passing all to Opus")
        return candidates[:SCREENER_TOP_N]


# ─────────────────────────────────────────────────────────
#  KELLY SIZING  ·  confidence-tiered
# ─────────────────────────────────────────────────────────

def kelly_size(true_prob_pct, market_prob_pct, bankroll, closes_in_days=7.0, confidence=70):
    if not (0 < market_prob_pct < 100) or not (0 < true_prob_pct < 100):
        return 0.0

    p = true_prob_pct / 100
    q = 1 - p
    b = (1 - market_prob_pct / 100) / (market_prob_pct / 100)

    if b <= 0:
        return 0.0

    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0.0

    fraction = 0.25
    cap_pct  = 3.0
    for tier in KELLY_TIERS:
        if confidence >= tier["min_conf"]:
            fraction = tier["kelly_fraction"]
            cap_pct  = tier["max_pct"]
            break

    sized = full_kelly * fraction

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
#  DIVERSIFICATION
# ─────────────────────────────────────────────────────────

def get_category(question):
    q = question.lower()
    if any(k in q for k in ["temperature", "weather", "rain", "snow", "°c", "°f", "celsius", "fahrenheit"]):
        return "weather"
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "defi"]):
        return "crypto"
    if any(k in q for k in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis", "golf", "points", "goals", "score", "match", "game", "fc ", " united", "spread", "o/u"]):
        return "sports"
    if any(k in q for k in ["president", "election", "senate", "congress", "vote", "government", "minister", "party", "trump", "biden", "democrat", "republican"]):
        return "politics"
    if any(k in q for k in ["fed", "rate", "inflation", "gdp", "recession", "unemployment", "economy", "s&p", "nasdaq", "dow"]):
        return "economics"
    if any(k in q for k in ["war", "military", "attack", "ceasefire", "hezbollah", "ukraine", "russia", "israel", "hamas", "conflict"]):
        return "geopolitics"
    return "other"


def category_slots_available(category, state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    cat_count   = sum(1 for t in open_trades if (t.get("category") or get_category(t.get("market", ""))) == category)
    return (MAX_PER_CATEGORY - cat_count) > 0


# ─────────────────────────────────────────────────────────
#  STAGE 2 — OPUS 4.6 DEEP ANALYST
# ─────────────────────────────────────────────────────────

def opus_analyze(markets, state):
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
        history_ctx = f"\nYOUR TRACK RECORD: {win_rate:.0f}% win rate ({len(won)}W / {len(lost)}L from {len(closed)} trades)\n\nRecent trades:\n"
        for t in closed[-10:]:
            result       = "WON" if t.get("won") else "LOST"
            edge         = abs(t.get("true_prob", 0) - t.get("market_prob", 0))
            history_ctx += f"  {result} | {t['position']} @ {t['entry_price']}¢ | edge {edge}% | conf {t.get('confidence', 0)}% | {t['market'][:60]}\n"
        if lost:
            history_ctx += "\nPatterns in losses — avoid repeating:\n"
            for t in lost[-5:]:
                history_ctx += f"  LOST {t['position']} — bear case: {t.get('bear_case', 'unknown')[:80]}\n"

    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    open_ctx    = ""
    if open_trades:
        open_ctx = "\nCURRENT OPEN POSITIONS (do NOT re-recommend):\n"
        open_ctx += "\n".join(
            f"  {t['position']} @ {t['entry_price']}¢ | cat:{t.get('category', get_category(t.get('market', '')))} | {t['market'][:70]}"
            for t in open_trades
        )
        cat_counts = {}
        for t in open_trades:
            cat = t.get("category") or get_category(t.get("market", ""))
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        open_ctx += f"\n\nCATEGORY COUNTS: {cat_counts} | MAX PER CATEGORY: {MAX_PER_CATEGORY}"

    mkt_list = "\n".join(
        f'ID:{m["id"]} | Closes {m["closes"][:10]} ({m["closes_in_days"]:.1f}d) | '
        f'YES={m["yes"]}¢ NO={100 - m["yes"]}¢ | Vol=${m["volume"]:,.0f} | "{m["question"]}"'
        for m in markets
    )

    prompt = f"""You are an expert algorithmic prediction market trader.

TODAY: {datetime.now(timezone.utc).strftime("%A %B %d %Y %H:%M UTC")}
BANKROLL: ${state['bankroll']:.2f} | AVAILABLE SLOTS: {available} | MAX BET: {MAX_BET_PCT}%
MIN EDGE: {MIN_EDGE_PCT}% | MIN CONFIDENCE: {MIN_CONFIDENCE}%
{history_ctx}
{open_ctx}

CANDIDATE MARKETS (all close within {MAX_HOLD_DAYS} days):
{mkt_list}

PROCESS:
1. Use web_search to find SPECIFIC, CURRENT data for each promising market
2. Estimate true probability using base rates, current evidence, momentum, crowd bias
3. Only recommend if genuine edge >= {MIN_EDGE_PCT}% after research

CONFIDENCE GUIDE — be honest and calibrated:
  90-99%: Near-certain. Verified specific evidence (confirmed withdrawal,
          real-time stats showing outcome essentially decided)
  75-89%: High confidence. Strong evidence pointing clearly one way
  60-74%: Moderate confidence. Edge exists but meaningful uncertainty remains

DIVERSIFICATION RULE — STRICT:
  Max {MAX_PER_CATEGORY} open positions per category
  Do NOT recommend a 3rd in any category already at {MAX_PER_CATEGORY}

NOTE: Do NOT include size_pct — sizing is handled automatically by confidence tier.

Return ONLY a valid JSON array, no other text:
[
  {{
    "market_id": "exact ID from list",
    "market": "exact question text",
    "position": "YES or NO",
    "market_prob": 48,
    "true_prob": 63,
    "confidence": 74,
    "category": "sports",
    "research_summary": "2-3 sentences: specific data found and why it gives edge",
    "key_factors": ["specific factor 1", "specific factor 2", "specific factor 3"],
    "bear_case": "main reason you could be wrong"
  }}
]

If no genuine edge found after research, return: []"""

    log(f"🧠 Opus 4.6 analyzing {len(markets)} markets with adaptive thinking + web search...")

    try:
        response = client.messages.create(
            model=ANALYST_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )

        searches     = 0
        thinking_txt = ""
        full_text    = ""

        for block in response.content:
            if hasattr(block, "type"):
                if block.type == "thinking":
                    thinking_txt = block.thinking
                    log(f"  💭 Opus thought for {len(thinking_txt)} chars")
                elif block.type == "tool_use" and block.name == "web_search":
                    searches += 1
                    log(f"  🔍 Searched: \"{block.input.get('query', '')}\"")
                elif block.type == "text":
                    full_text += block.text

        log(f"  📊 {searches} search(es) | thinking: {'yes' if thinking_txt else 'no'}")

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
                log(f"  ⏭  Confidence {r.get('confidence')}% too low — {r.get('market', '')[:50]}")
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
            log(f"  📋 BUY {r['position']} | {cat} | {tier} | market={r['market_prob']}% → true={r['true_prob']}% (+{edge}%) | conf {r['confidence']}%")
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
        log(f"  ⏭  Market {rec['market_id']} not found")
        return state

    end_dt = parse_utc(mkt["closes"])
    if not end_dt:
        log(f"  ⏭  Can't parse close date")
        return state

    cid = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    if cid < (MIN_HOLD_HOURS / 24) or cid > MAX_HOLD_DAYS:
        log(f"  ⏭  Closes in {cid:.1f}d — outside window")
        return state

    stake = kelly_size(
        true_prob_pct   = rec["true_prob"],
        market_prob_pct = rec["market_prob"],
        bankroll        = state["bankroll"],
        closes_in_days  = cid,
        confidence      = conf
    )

    if stake < 1.00:
        log(f"  ⏭  Stake ${stake:.2f} too small")
        return state

    tier   = get_tier_name(conf)
    entry  = rec["market_prob"] if rec["position"] == "YES" else (100 - rec["market_prob"])
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
        "true_prob":        rec["true_prob"],
        "market_prob":      rec["market_prob"],
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
    print("  CLAUDEBOT v7  ·  Opus 4.6 · Tiered Kelly · Telegram")
    print("═" * 65)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Open           {len(open_t)} / {MAX_OPEN_POSITIONS}")
    print(f"  Closed         {len(closed_t)}  ({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Total Scans    {state.get('scan_count', 0)}")
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
    print("║  CLAUDEBOT v7  ·  Tiered Kelly · Telegram · 3h interval  ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  |  Haiku screen → Opus deep dive   ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    # Daily summary at 9am UTC
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

    log("── Step 4: Opus 4.6 deep research + analysis ────────────")
    recs = opus_analyze(top_markets, state)

    log("── Step 5: Place trades ──────────────────────────────────")
    if not recs:
        log("No trades this scan")
    else:
        for rec in recs:
            state = place_paper_trade(rec, markets, state)

    log("── Step 6: Save ──────────────────────────────────────────")
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v7  ·  Continuous Mode                        ║")
    print(f"║  Interval: {SCAN_INTERVAL_MINS}min (3h)  |  10 slots  |  Telegram     ║")
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
