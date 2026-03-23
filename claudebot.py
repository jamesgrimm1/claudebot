"""
╔══════════════════════════════════════════════════════╗
║           CLAUDEBOT — Polymarket Paper Trader        ║
║           Autonomous AI-powered prediction market    ║
║           analysis and paper trading engine          ║
╚══════════════════════════════════════════════════════╝

SETUP:
  pip install anthropic requests

RUN:
  python claudebot.py

It will scan markets every 30 minutes, analyze with Claude,
and place paper trades automatically. All results are saved
to claudebot_log.json and printed to the terminal.

GOING LIVE (when ready):
  Set PAPER_TRADING = False and fill in your Polymarket
  credentials below. The bot will submit real CLOB orders.
"""

import time
import json
import os
import random
from datetime import datetime, timedelta
import anthropic

# ─────────────────────────────────────────────────────
#  CONFIG — edit these
# ─────────────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
PAPER_TRADING       = True                        # set False for real money (see LIVE section)
STARTING_BANKROLL   = 1000.00                     # USD / USDC
MAX_BET_PCT         = 5.0                         # max % of bankroll per trade
MIN_CONFIDENCE      = 55                          # minimum Claude confidence % to trade
MAX_OPEN_POSITIONS  = 5                           # max simultaneous open bets
SCAN_INTERVAL_MINS  = 30                          # how often to scan (minutes)
DAILY_LOSS_LIMIT    = 150.00                      # stop trading for the day if losses exceed this
LOG_FILE            = "claudebot_log.json"

# ─── LIVE TRADING CREDENTIALS (only needed if PAPER_TRADING = False) ───
POLYMARKET_API_KEY        = ""
POLYMARKET_API_SECRET     = ""
POLYMARKET_API_PASSPHRASE = ""
POLYMARKET_PRIVATE_KEY    = ""   # your wallet private key (0x...)

# ─────────────────────────────────────────────────────
#  DEMO MARKETS — realistic March 2026 markets
#  (replace with live Polymarket fetch once credentialed)
# ─────────────────────────────────────────────────────

DEMO_MARKETS = [
    # Crypto
    {"id": "m001", "question": "Will Bitcoin exceed $120,000 before June 2026?",
     "yes": 44, "volume": 8200000, "category": "crypto", "closes": "2026-06-30"},
    {"id": "m002", "question": "Will Ethereum exceed $4,000 before June 2026?",
     "yes": 51, "volume": 4100000, "category": "crypto", "closes": "2026-06-30"},
    {"id": "m003", "question": "Will Solana exceed $300 before end of 2026?",
     "yes": 38, "volume": 1800000, "category": "crypto", "closes": "2026-12-31"},
    {"id": "m004", "question": "Will Bitcoin dominance exceed 60% in Q2 2026?",
     "yes": 42, "volume": 960000, "category": "crypto", "closes": "2026-06-30"},

    # US Politics / Policy
    {"id": "m005", "question": "Will the US Federal Reserve cut rates at least once before July 2026?",
     "yes": 67, "volume": 5500000, "category": "politics", "closes": "2026-07-01"},
    {"id": "m006", "question": "Will US inflation (CPI) fall below 2.5% by June 2026?",
     "yes": 31, "volume": 2200000, "category": "economics", "closes": "2026-06-30"},
    {"id": "m007", "question": "Will the US enter a recession before end of 2026?",
     "yes": 34, "volume": 3800000, "category": "economics", "closes": "2026-12-31"},
    {"id": "m008", "question": "Will US unemployment exceed 5% before end of 2026?",
     "yes": 28, "volume": 1400000, "category": "economics", "closes": "2026-12-31"},
    {"id": "m009", "question": "Will there be a US government shutdown before July 2026?",
     "yes": 22, "volume": 1100000, "category": "politics", "closes": "2026-07-01"},
    {"id": "m010", "question": "Will the S&P 500 be higher on December 31 2026 than January 1 2026?",
     "yes": 61, "volume": 6700000, "category": "economics", "closes": "2026-12-31"},

    # Global / Geopolitics
    {"id": "m011", "question": "Will there be a ceasefire agreement in Ukraine before July 2026?",
     "yes": 29, "volume": 4200000, "category": "geopolitics", "closes": "2026-07-01"},
    {"id": "m012", "question": "Will China's GDP growth exceed 4.5% in 2026?",
     "yes": 48, "volume": 890000, "category": "economics", "closes": "2026-12-31"},

    # Tech / AI
    {"id": "m013", "question": "Will OpenAI release a new flagship model before July 2026?",
     "yes": 81, "volume": 2100000, "category": "tech", "closes": "2026-07-01"},
    {"id": "m014", "question": "Will any AI company reach $5 trillion market cap before end of 2026?",
     "yes": 19, "volume": 1300000, "category": "tech", "closes": "2026-12-31"},
    {"id": "m015", "question": "Will Apple release a new AR/VR device in 2026?",
     "yes": 57, "volume": 780000, "category": "tech", "closes": "2026-12-31"},

    # Sports
    {"id": "m016", "question": "Will the Golden State Warriors make the 2026 NBA playoffs?",
     "yes": 43, "volume": 340000, "category": "sports", "closes": "2026-05-01"},
    {"id": "m017", "question": "Will a European team win the 2026 FIFA World Cup?",
     "yes": 52, "volume": 2800000, "category": "sports", "closes": "2026-07-20"},
    {"id": "m018", "question": "Will there be a new Formula 1 World Champion in 2026?",
     "yes": 61, "volume": 1200000, "category": "sports", "closes": "2026-12-01"},
]


# ─────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            return json.load(f)
    return {
        "bankroll": STARTING_BANKROLL,
        "trades": [],
        "daily_loss": 0.0,
        "daily_reset": datetime.now().strftime("%Y-%m-%d"),
        "scan_count": 0,
        "started": datetime.now().isoformat(),
    }

def save_state(state):
    with open(LOG_FILE, "w") as f:
        json.dump(state, f, indent=2)

def reset_daily_loss_if_needed(state):
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 New day — daily loss counter reset.")
    return state


# ─────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ─────────────────────────────────────────────────────
#  MARKET FETCHING
#  Tries Polymarket Gamma API first, falls back to demo
# ─────────────────────────────────────────────────────

def fetch_markets():
    try:
        import requests
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=40&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        raw = r.json()
        markets = []
        for m in raw:
            if not m.get("question") or not m.get("outcomePrices"):
                continue
            prices = json.loads(m["outcomePrices"])
            yes = round(float(prices[0]) * 100)
            markets.append({
                "id": m.get("id", ""),
                "question": m["question"],
                "yes": yes,
                "volume": float(m.get("volume", 0)),
                "category": (m.get("tags") or [{}])[0].get("label", "general"),
                "closes": m.get("endDate", ""),
                "clobTokenIds": m.get("clobTokenIds", []),
            })
        log(f"✅ Fetched {len(markets)} live markets from Polymarket")
        return markets
    except Exception as e:
        log(f"⚠️  Polymarket API unavailable ({e}). Using demo markets.")
        return DEMO_MARKETS


# ─────────────────────────────────────────────────────
#  CLAUDE ANALYSIS
# ─────────────────────────────────────────────────────

def analyze_markets(markets, state):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    open_positions = [t for t in state["trades"] if t["status"] == "open"]
    open_ids = {t["market_id"] for t in open_positions}

    # Skip markets we already have open positions in
    candidates = [m for m in markets if m["id"] not in open_ids]

    if not candidates:
        log("No new markets to analyze (all open positions already filled).")
        return []

    mkt_list = "\n".join([
        f'- ID:{m["id"]} | "{m["question"]}" | YES={m["yes"]}¢ NO={100 - m["yes"]}¢ | Vol=${m["volume"]:,.0f} | Closes:{m["closes"]}'
        for m in candidates[:20]  # analyze top 20 by volume
    ])

    prompt = f"""You are a sharp, experienced prediction market trader. Today is {datetime.now().strftime("%B %d, %Y")}.

Scan these Polymarket markets and identify the BEST 3-5 trades where you detect a genuine mispricing.

Current bankroll: ${state['bankroll']:.2f}
Max bet: {MAX_BET_PCT}% per trade (${state['bankroll'] * MAX_BET_PCT / 100:.2f} max)
Min confidence to trade: {MIN_CONFIDENCE}%

Markets:
{mkt_list}

Rules:
- Find mispricings of at least 5-10%. Even a 6% edge is worth trading.
- Look for markets where crowd psychology, recency bias, or news cycles have skewed odds
- Low-volume markets are often less efficient — prefer those for edge
- Consider correlations — don't recommend two trades on the same underlying event
- Be decisive. If there's edge, recommend it.

Return ONLY a JSON array. No other text. No markdown. Example format:
[
  {{
    "market_id": "m001",
    "market": "exact question",
    "position": "YES",
    "market_prob": 44,
    "true_prob": 58,
    "confidence": 72,
    "size_pct": 3,
    "reason": "Brief 1-2 sentence reason for the edge"
  }}
]

If genuinely no edge exists anywhere, return an empty array: []"""

    log("🤖 Calling Claude to analyze markets...")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        # Try to extract JSON array if there's surrounding text
        if not raw.startswith("["):
            import re
            match = re.search(r'\[[\s\S]*\]', raw)
            raw = match.group(0) if match else "[]"

        recs = json.loads(raw)
        log(f"🤖 Claude returned {len(recs)} trade recommendation(s).")
        return recs
    except Exception as e:
        log(f"❌ Claude API error: {e}")
        return []


# ─────────────────────────────────────────────────────
#  KELLY SIZING
# ─────────────────────────────────────────────────────

def kelly_size(true_prob_pct, market_prob_pct, bankroll):
    """
    Half-Kelly bet sizing.
    Edge = true_prob - market_prob
    Odds = (1 - market_prob) / market_prob  (decimal odds minus 1)
    Kelly % = Edge / Odds
    Half-Kelly = Kelly % / 2
    """
    p = true_prob_pct / 100
    q = 1 - p
    b = (1 - market_prob_pct / 100) / (market_prob_pct / 100)  # net odds

    if b <= 0:
        return 0

    full_kelly = (b * p - q) / b
    half_kelly = full_kelly / 2

    # Cap at MAX_BET_PCT
    capped = min(max(half_kelly, 0), MAX_BET_PCT / 100)
    return round(capped * bankroll, 2)


# ─────────────────────────────────────────────────────
#  PAPER TRADE EXECUTION
# ─────────────────────────────────────────────────────

def place_paper_trade(rec, markets, state):
    market = next((m for m in markets if m["id"] == rec["market_id"]), None)
    if not market:
        # Try matching by question text
        market = next((m for m in markets if m["question"][:40] in rec["market"]), None)

    confidence = rec.get("confidence", 0)
    if confidence < MIN_CONFIDENCE:
        log(f"  ⏭  Skipping — confidence {confidence}% below threshold {MIN_CONFIDENCE}%")
        return state

    # Kelly sizing
    stake = kelly_size(rec["true_prob"], rec["market_prob"], state["bankroll"])
    if stake < 1.00:
        log(f"  ⏭  Skipping — stake too small (${stake:.2f})")
        return state

    open_count = sum(1 for t in state["trades"] if t["status"] == "open")
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"  ⏭  Skipping — max open positions ({MAX_OPEN_POSITIONS}) reached")
        return state

    if state["daily_loss"] >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit (${DAILY_LOSS_LIMIT}) hit — no more trades today")
        return state

    entry_price = rec["market_prob"] if rec["position"] == "YES" else (100 - rec["market_prob"])
    potential_return = round(stake * 100 / entry_price, 2)
    potential_profit = round(potential_return - stake, 2)

    trade = {
        "id": f"T{int(time.time())}",
        "market_id": rec["market_id"],
        "market": rec["market"],
        "position": rec["position"],
        "entry_price": entry_price,
        "stake": stake,
        "potential_return": potential_return,
        "potential_profit": potential_profit,
        "confidence": confidence,
        "true_prob": rec["true_prob"],
        "market_prob": rec["market_prob"],
        "reason": rec.get("reason", ""),
        "status": "open",
        "placed_at": datetime.now().isoformat(),
        "paper": True,
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  ✅ PAPER BET PLACED")
    log(f"     Market:   {trade['market'][:70]}")
    log(f"     Position: {trade['position']} @ {entry_price}¢")
    log(f"     Stake:    ${stake:.2f} | Win: ${potential_return:.2f} | Edge: +${potential_profit:.2f}")
    log(f"     Confidence: {confidence}% | Closes: {market['closes'] if market else 'unknown'}")
    log(f"     Reason:   {trade['reason']}")
    log(f"     Bankroll remaining: ${state['bankroll']:.2f}")

    return state


# ─────────────────────────────────────────────────────
#  LIVE TRADE EXECUTION (when PAPER_TRADING = False)
# ─────────────────────────────────────────────────────

def place_live_trade(rec, markets, state):
    """
    Submits a real order to Polymarket CLOB.
    Requires: pip install py-clob-client
    And valid credentials in the config above.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.constants import POLYGON

        market = next((m for m in markets if m["id"] == rec["market_id"]), None)
        if not market or not market.get("clobTokenIds"):
            log(f"  ❌ No CLOB token IDs for market {rec['market_id']}")
            return state

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=POLYGON,
            creds={
                "apiKey": POLYMARKET_API_KEY,
                "apiSecret": POLYMARKET_API_SECRET,
                "apiPassphrase": POLYMARKET_API_PASSPHRASE,
            }
        )

        # YES = token index 0, NO = token index 1
        token_idx = 0 if rec["position"] == "YES" else 1
        token_id = market["clobTokenIds"][token_idx]

        stake = kelly_size(rec["true_prob"], rec["market_prob"], state["bankroll"])
        entry_price = rec["market_prob"] / 100 if rec["position"] == "YES" else (100 - rec["market_prob"]) / 100

        order_args = OrderArgs(
            token_id=token_id,
            price=round(entry_price, 2),
            size=round(stake, 2),
            side="BUY"
        )

        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        log(f"  ✅ LIVE ORDER SUBMITTED: {resp}")

        trade = {
            "id": f"T{int(time.time())}",
            "market_id": rec["market_id"],
            "market": rec["market"],
            "position": rec["position"],
            "entry_price": round(entry_price * 100),
            "stake": stake,
            "confidence": rec.get("confidence", 0),
            "reason": rec.get("reason", ""),
            "status": "open",
            "placed_at": datetime.now().isoformat(),
            "paper": False,
            "order_response": str(resp),
        }
        state["bankroll"] = round(state["bankroll"] - stake, 2)
        state["trades"].append(trade)

    except ImportError:
        log("  ❌ py-clob-client not installed. Run: pip install py-clob-client")
    except Exception as e:
        log(f"  ❌ Live trade error: {e}")

    return state


# ─────────────────────────────────────────────────────
#  PORTFOLIO SUMMARY
# ─────────────────────────────────────────────────────

def print_portfolio(state):
    trades = state["trades"]
    open_t = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t = [t for t in closed_t if t.get("won")]
    lost_t = [t for t in closed_t if not t.get("won")]

    total_staked = sum(t["stake"] for t in trades)
    realized_pnl = sum(t.get("realized_pnl", 0) for t in closed_t)
    unrealized = sum(t["potential_profit"] for t in open_t)

    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0

    print("\n" + "═" * 55)
    print("  CLAUDEBOT PORTFOLIO SUMMARY")
    print("═" * 55)
    print(f"  Bankroll:        ${state['bankroll']:.2f}")
    print(f"  Realized P&L:    ${realized_pnl:+.2f}")
    print(f"  Unrealized (if all win): ${unrealized:+.2f}")
    print(f"  Total Staked:    ${total_staked:.2f}")
    print(f"  Open Positions:  {len(open_t)}")
    print(f"  Closed Trades:   {len(closed_t)}")
    print(f"  Win Rate:        {win_rate:.0f}% ({len(won_t)}W / {len(lost_t)}L)")
    print(f"  Scan Count:      {state['scan_count']}")
    print("═" * 55)

    if open_t:
        print("\n  OPEN POSITIONS:")
        for t in open_t:
            print(f"  • {t['position']} | ${t['stake']:.2f} stake | {t['market'][:55]}")
    print()


# ─────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────

def run():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║           CLAUDEBOT — Autonomous Paper Trader        ║")
    print(f"║  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}                           ║")
    print(f"║  Mode: {'PAPER TRADING 📝' if PAPER_TRADING else '🚨 LIVE TRADING 🚨'}                            ║")
    print(f"║  Bankroll: ${STARTING_BANKROLL:.2f} | Interval: {SCAN_INTERVAL_MINS}min           ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY":
        print("❌ ERROR: Set your ANTHROPIC_API_KEY at the top of this file.")
        print("   Get one at: https://console.anthropic.com/")
        return

    state = load_state()
    state = reset_daily_loss_if_needed(state)
    save_state(state)

    log(f"Loaded state. Bankroll: ${state['bankroll']:.2f} | Trades: {len(state['trades'])}")

    while True:
        try:
            state = reset_daily_loss_if_needed(state)
            state["scan_count"] = state.get("scan_count", 0) + 1

            log(f"\n{'─' * 50}")
            log(f"SCAN #{state['scan_count']} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            log(f"Bankroll: ${state['bankroll']:.2f} | Open: {sum(1 for t in state['trades'] if t['status']=='open')}")
            log(f"{'─' * 50}")

            # Fetch markets
            markets = fetch_markets()

            # Get Claude's recommendations
            recs = analyze_markets(markets, state)

            if not recs:
                log("No trades recommended this scan.")
            else:
                for rec in recs:
                    log(f"\n→ Evaluating: BUY {rec['position']} on \"{rec['market'][:60]}\"")
                    log(f"   Market: {rec['market_prob']}% | Claude: {rec['true_prob']}% | Confidence: {rec['confidence']}%")

                    if PAPER_TRADING:
                        state = place_paper_trade(rec, markets, state)
                    else:
                        state = place_live_trade(rec, markets, state)

            save_state(state)
            print_portfolio(state)

            log(f"💤 Sleeping {SCAN_INTERVAL_MINS} minutes until next scan...")
            log(f"   (Press Ctrl+C to stop)\n")
            time.sleep(SCAN_INTERVAL_MINS * 60)

        except KeyboardInterrupt:
            log("\n\n🛑 Bot stopped by user.")
            print_portfolio(state)
            save_state(state)
            break
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
            log("Retrying in 60 seconds...")
            time.sleep(60)


if __name__ == "__main__":
    import sys
    if "--single-scan" in sys.argv:
        # Run one scan and exit (for GitHub Actions)
        state = load_state()
        state = reset_daily_loss_if_needed(state)
        markets = fetch_markets()
        recs = analyze_markets(markets, state)
        for rec in recs:
            state = place_paper_trade(rec, markets, state)
        save_state(state)
        print_portfolio(state)
    else:
        run()
