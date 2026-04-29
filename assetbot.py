“””
╔══════════════════════════════════════════════════════════╗
║  ASSETBOT — Price Mismatch Scanner                       ║
║                                                          ║
║  Scans Polymarket for markets where the required price   ║
║  move is implausibly large given current asset price.    ║
║                                                          ║
║  Entry rule: required move > min(10% × days, 40%)        ║
║  Sizing: flat 2% of bankroll per trade                   ║
║  No Opus — fully mechanical after Haiku parsing          ║
║                                                          ║
║  RUN: python assetbot.py –single-scan                   ║
╚══════════════════════════════════════════════════════════╝
“””

import os, sys, re, json, time, requests
from datetime import datetime, timezone, timedelta
import anthropic

ANTHROPIC_API_KEY = os.environ.get(“ANTHROPIC_API_KEY”, “”)
FINNHUB_API_KEY   = os.environ.get(“FINNHUB_API_KEY”, “”)
LOG_FILE          = “assetbot_log.json”
PAPER_TRADING     = True
STARTING_BANKROLL = 1000.00
STAKE_PCT         = 0.02        # 2% of bankroll per trade
MAX_DAYS          = 7           # only look at markets closing within 7 days
MIN_DAYS_HOURS    = 2           # ignore markets closing in under 2 hours
HAIKU_MODEL       = “claude-haiku-4-5-20251001”

# ── Required move threshold ───────────────────────────────

# Required move must exceed min(10% × days_remaining, 40%)

MOVE_PCT_PER_DAY  = 0.10
MOVE_PCT_CAP      = 0.30

# ── Asset registry ───────────────────────────────────────

# ticker -> (asset_type, [question keywords], price_symbol)

# price_symbol: Binance symbol for crypto, Finnhub ticker for stocks,

# Finnhub forex symbol for commodities

ASSET_REGISTRY = {
# ── Stocks (20) ──────────────────────────────────────
“AMZN”: (“stock”,     [“amazon”, “amzn”],                        “AMZN”),
“TSLA”: (“stock”,     [“tesla”, “tsla”],                         “TSLA”),
“NVDA”: (“stock”,     [“nvidia”, “nvda”],                        “NVDA”),
“AAPL”: (“stock”,     [“apple”, “aapl”],                         “AAPL”),
“MSFT”: (“stock”,     [“microsoft”, “msft”],                     “MSFT”),
“GOOGL”:(“stock”,     [“google”, “googl”, “alphabet”],           “GOOGL”),
“META”: (“stock”,     [“meta”, “facebook”],                      “META”),
“NFLX”: (“stock”,     [“netflix”, “nflx”],                       “NFLX”),
“INTC”: (“stock”,     [“intel”, “intc”],                         “INTC”),
“PLTR”: (“stock”,     [“palantir”, “pltr”],                      “PLTR”),
“SHOP”: (“stock”,     [“shopify”, “shop”],                       “SHOP”),
“AMD”:  (“stock”,     [“amd”, “advanced micro”],                 “AMD”),
“COIN”: (“stock”,     [“coinbase”, “(coin)”],                    “COIN”),
“UBER”: (“stock”,     [“uber”],                                   “UBER”),
“DIS”:  (“stock”,     [“disney”, “ dis “],                       “DIS”),
“JPM”:  (“stock”,     [“jpmorgan”, “jp morgan”, “jpm”],          “JPM”),
“SPY”:  (“stock”,     [“s&p 500”, “spy”, “s&p500”],              “SPY”),
“QQQ”:  (“stock”,     [“nasdaq”, “qqq”],                         “QQQ”),
“DIA”:  (“stock”,     [“dow jones”, “dow”, “djia”, “dia”],       “DIA”),
“BABA”: (“stock”,     [“alibaba”, “baba”],                       “BABA”),
# ── Crypto (7) ───────────────────────────────────────
“BTC”:  (“crypto”,    [“bitcoin”, “btc”],                        “BTCUSDT”),
“ETH”:  (“crypto”,    [“ethereum”, “eth”],                       “ETHUSDT”),
“SOL”:  (“crypto”,    [“solana”, “sol”],                         “SOLUSDT”),
“XRP”:  (“crypto”,    [“xrp”, “ripple”],                         “XRPUSDT”),
“BNB”:  (“crypto”,    [“bnb”, “binance coin”],                   “BNBUSDT”),
“DOGE”: (“crypto”,    [“dogecoin”, “doge”],                      “DOGEUSDT”),
“ADA”:  (“crypto”,    [“cardano”, “ada”],                        “ADAUSDT”),
# ── Commodities (5) ──────────────────────────────────
“WTI”:  (“commodity”, [“wti”, “crude oil”, “oil price”],         “USOIL”),
“BRENT”:(“commodity”, [“brent”],                                  “UKOIL”),
“GOLD”: (“commodity”, [“gold”, “xau”],                           “XAUUSD”),
“SILVER”:(“commodity”,[“silver”, “xag”],                         “XAGUSD”),
“NATGAS”:(“commodity”,[“natural gas”, “nat gas”],                “NG1:NMX”),
}

# Flat ASSETS list — sorted longest keyword first to prevent substring matches

ASSETS = sorted([
(kw, data[2], data[0])
for ticker, data in ASSET_REGISTRY.items()
for kw in data[1]
], key=lambda x: -len(x[0]))

# Unique tickers for snapshot

SNAPSHOT_TICKERS = [
(ticker, data[0], data[2])
for ticker, data in ASSET_REGISTRY.items()
]

# ─────────────────────────────────────────────────────────

# LOGGING

# ─────────────────────────────────────────────────────────

def log(msg):
print(f”[{datetime.now().strftime(’%H:%M:%S’)}] {msg}”)

# ─────────────────────────────────────────────────────────

# STATE

# ─────────────────────────────────────────────────────────

def load_state():
if os.path.exists(LOG_FILE):
with open(LOG_FILE) as f:
s = json.load(f)
log(f”📂 Loaded — {len(s[‘trades’])} trades | bankroll ${s[‘bankroll’]:.2f}”)
return s
log(“📂 Fresh start”)
return {
“bankroll”: STARTING_BANKROLL,
“trades”:   [],
“scan_count”: 0,
“started”:  datetime.now(timezone.utc).isoformat(),
}

def save_state(state):
with open(LOG_FILE, “w”) as f:
json.dump(state, f, indent=2)
log(f”💾 Saved — bankroll ${state[‘bankroll’]:.2f} | {len(state[‘trades’])} trades”)

# ─────────────────────────────────────────────────────────

# CLOSING PRICE SNAPSHOT

# ─────────────────────────────────────────────────────────

# US market closes 4pm EST = 9pm UTC.

# We snapshot closing prices once daily after close.

# If no snapshot by midnight UTC (8am Bali), force fetch.

SNAPSHOT_HOUR_UTC = 21   # 9pm UTC = US market close
FORCE_BY_HOUR_UTC = 0    # midnight UTC = 8am Bali — force fetch if missed

def should_take_snapshot(state):
now = datetime.now(timezone.utc)
today = now.strftime(”%Y-%m-%d”)
snapshot = state.get(“price_snapshot”, {})
snap_date = snapshot.get(“date”, “”)

```
if snap_date == today:
    return False  # already have today's snapshot

# If no snapshot ever taken, fetch immediately
if not snap_date:
    return True

# Take snapshot if: after 9pm UTC (US market close)
if now.hour >= SNAPSHOT_HOUR_UTC:
    return True

# Past midnight UTC and still no snapshot for today — force it (8am Bali)
if now.hour < FORCE_BY_HOUR_UTC + 1:
    return True

return False
```

def take_price_snapshot(state):
log(“📸 Taking closing price snapshot…”)
snapshot = {“date”: datetime.now(timezone.utc).strftime(”%Y-%m-%d”)}
fetched = 0
for name, asset_type, price_symbol in SNAPSHOT_TICKERS:
price = get_live_price(price_symbol, asset_type)
if price:
snapshot[price_symbol] = price
fetched += 1
state[“price_snapshot”] = snapshot
log(f”📸 Snapshot complete — {fetched} prices captured”)
return state

# ─────────────────────────────────────────────────────────

# LIVE PRICE FETCH

# ─────────────────────────────────────────────────────────

_price_cache = {}

def get_reference_price(price_symbol, asset_type, state):
“”“Use snapshot price if available, otherwise fetch live.”””
snapshot = state.get(“price_snapshot”, {})
if price_symbol in snapshot:
return snapshot[price_symbol]
return get_live_price(price_symbol, asset_type)

# Known approximate price floors for sanity checking

_PRICE_FLOORS = {
“NFLX”: 500,   # Netflix trades $500+
“AMZN”: 150,   # Amazon
“MSFT”: 200,   # Microsoft
“GOOGL”: 100,  # Google
“BTCUSDT”: 30000,  # BTC
}

def validate_price(price_symbol, price):
“”“Return False if price looks obviously wrong.”””
if price is None or price <= 0:
return False
floor = _PRICE_FLOORS.get(price_symbol)
if floor and price < floor * 0.3:
log(f”  ⚠️  Price sanity fail: {price_symbol} @ ${price:.2f} (floor ${floor}) — skipping”)
return False
return True

def get_live_price(ticker, asset_type):
if ticker in _price_cache:
return _price_cache[ticker]

```
price = None
try:
    if asset_type == "crypto":
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={ticker}",
            timeout=6
        )
        if r.status_code == 200:
            price = float(r.json()["price"])

    elif asset_type in ("stock", "commodity"):
        if not FINNHUB_API_KEY:
            return None
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=6
        )
        if r.status_code == 200:
            d = r.json()
            price = d.get("c") or d.get("pc")  # current or prev close

except Exception as e:
    log(f"  ⚠️  Price fetch failed {ticker}: {e}")

if price and price > 0:
    _price_cache[ticker] = price
return price
```

# ─────────────────────────────────────────────────────────

# MARKET FETCH

# ─────────────────────────────────────────────────────────

def fetch_markets():
“”“Fetch all active Polymarket markets closing within MAX_DAYS.”””
try:
all_markets = []
for offset in [0, 500, 1000]:
r = requests.get(
“https://gamma-api.polymarket.com/markets”
“?active=true&closed=false&limit=500”
f”&offset={offset}&order=volume&ascending=false”,
timeout=12
)
if r.status_code != 200:
break
batch = r.json()
if not batch:
break
all_markets.extend(batch)
raw = all_markets
if not raw:
log(“⚠️  Polymarket returned no markets”)
return []
except Exception as e:
log(f”⚠️  Polymarket fetch failed: {e}”)
return []

```
now = datetime.now(timezone.utc)
markets = []
for m in raw:
    if not m.get("question"):
        continue
    end_str = m.get("endDate") or m.get("end_date") or ""
    if not end_str:
        continue
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except:
        continue
    days = (end_dt - now).total_seconds() / 86400
    hours = days * 24
    if hours < MIN_DAYS_HOURS or days > MAX_DAYS:
        continue
    if m.get("negRisk", False):
        continue
    markets.append({
        "id":       str(m.get("id", "")),
        "question": m["question"],
        "closes":   end_dt.isoformat(),
        "closes_in_days": round(days, 3),
        "slug":     m.get("slug", ""),
    })

log(f"📋 Fetched {len(markets)} markets in 2h-{MAX_DAYS}d window")
return markets
```

# ─────────────────────────────────────────────────────────

# ASSET MATCHING

# ─────────────────────────────────────────────────────────

def match_asset(question):
“”“Return (price_symbol, asset_type) if question mentions a tracked asset.”””
q = question.lower()
for keyword, price_symbol, asset_type in ASSETS:
if keyword in q:
return price_symbol, asset_type
return None, None

# ─────────────────────────────────────────────────────────

# HAIKU PARSER

# ─────────────────────────────────────────────────────────

def haiku_parse_threshold(client, question, current_price, ticker):
“””
Use Haiku to extract:
- threshold price
- direction: ‘above’ | ‘below’ | ‘range’
- range_low, range_high (if direction == ‘range’)
- resolution_type: ‘close’ | ‘intraday’ | ‘weekly_close’ | ‘weekly_low’ | ‘weekly_high’ | ‘unclear’
- required_move_pct: how far current price needs to move to resolve YES
- position: ‘YES’ or ‘NO’ (which side has the implausible move)
“””
# First do a quick sanity check before calling Haiku
# If we can determine from the question structure whether YES is plausible, skip Haiku
q_lower = question.lower()

```
prompt = (
    f"You are analyzing a Polymarket prediction market question.\n"
    f"Current {ticker} price: ${current_price:,.2f}\n"
    f"Question: \"{question}\"\n\n"
    f"Extract ONLY if this is a price threshold market (stock/crypto/commodity hitting a price level).\n"
    f"Return ONLY valid JSON with these fields:\n"
    f"  threshold: the price level in the question (number)\n"
    f"  direction: \'above\' (YES if price ends above threshold) or \'below\' (YES if price ends below threshold)\n"
    f"  resolution_type: \'close\' (daily close price) or \'intraday\' (any touch) or \'weekly_close\' or \'unclear\'\n"
    f"  yes_requires_move_pct: how many % the current price must MOVE for YES to resolve "
    f"(if current price already past threshold, this is 0 or negative)\n"
    f"  yes_is_implausible: true ONLY if YES resolution requires a move > 10% AND current price "
    f"has NOT already crossed the threshold\n"
    f"  reasoning: one sentence explanation\n\n"
    f"CRITICAL EXAMPLES:\n"
    f"  Current BTC=$94000, question \'above $60000\': YES already true, yes_is_implausible=false\n"
    f"  Current BTC=$94000, question \'above $130000\': needs 38% rise, yes_is_implausible=true\n"
    f"  Current META=$675, question \'above $640\': YES already true (675>640), yes_is_implausible=false\n"
    f"  Current WTI=$93, question \'above $50\': YES already true, yes_is_implausible=false\n"
    f"  Current WTI=$93, question \'above $140\': needs 50% rise, yes_is_implausible=true\n\n"
    f"If not a price threshold market, return {{\"threshold\": null}}"
)

try:
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        return None
    return json.loads(match.group(0))
except Exception as e:
    log(f"  ⚠️  Haiku parse error: {e}")
    return None
```

# ─────────────────────────────────────────────────────────

# MISMATCH CHECK

# ─────────────────────────────────────────────────────────

def required_move_threshold(days_remaining):
“”“min(10% × days, 40%)”””
return min(MOVE_PCT_PER_DAY * days_remaining, MOVE_PCT_CAP)

def check_mismatch(parsed, days_remaining):
“””
Returns True ONLY if YES resolution is genuinely implausible:
- Current price has NOT already crossed the threshold
- Required move exceeds min(10% x days, 30%)
- Resolution type is a closing price (not intraday touch)
“””
if not parsed or parsed.get(“threshold”) is None:
return False

```
# Must explicitly flag as implausible
if not parsed.get("yes_is_implausible", False):
    return False

# Skip resolution types we cant reliably assess
resolution = parsed.get("resolution_type", "unclear")
if resolution in ("intraday", "weekly_low", "weekly_high", "unclear"):
    return False

required_pct = parsed.get("yes_requires_move_pct", 0)
if not required_pct or required_pct <= 0:
    return False

threshold_pct = required_move_threshold(days_remaining) * 100
return required_pct > threshold_pct
```

# ─────────────────────────────────────────────────────────

# PLACE TRADE

# ─────────────────────────────────────────────────────────

def place_trade(market, parsed, current_price, ticker, state):
open_ids = {t[“market_id”] for t in state[“trades”] if t[“status”] == “open”}
if market[“id”] in open_ids:
return state

```
stake = round(state["bankroll"] * STAKE_PCT, 2)
if stake < 0.50:
    log(f"  ⏭  Stake ${stake:.2f} too small")
    return state

# We always bet NO — YES is the implausible side by definition
position = "NO"

# Estimate entry price from market odds — fetch from Gamma
entry_price = 50  # default if we can't fetch
try:
    r = requests.get(
        f"https://gamma-api.polymarket.com/markets/{market['id']}",
        timeout=6
    )
    if r.status_code == 200:
        mkt = r.json()
        prices_raw = mkt.get("outcomePrices")
        if prices_raw:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            prices = [float(p) * 100 for p in prices]
            if len(prices) >= 2:
                entry_price = round(prices[1] if position == "NO" else prices[0])
except:
    pass

if entry_price <= 1:
    log(f"  ⏭  Entry price {entry_price}¢ too low — already priced in")
    return state
if entry_price >= 99:
    log(f"  ⏭  Entry price {entry_price}¢ too high — NO already near certain")
    return state

payout = round(stake * 100 / entry_price, 2)
profit = round(payout - stake, 2)

trade = {
    "id":               f"AB{int(time.time())}",
    "market_id":        market["id"],
    "market_slug":      market.get("slug", ""),
    "market":           market["question"],
    "position":         position,
    "entry_price":      entry_price,
    "stake":            stake,
    "payout_if_wins":   payout,
    "profit_if_wins":   profit,
    "ticker":           ticker,
    "current_price":    current_price,
    "threshold":        parsed.get("threshold"),
    "required_move_pct": parsed.get("yes_requires_move_pct"),
    "resolution_type":  parsed.get("resolution_type"),
    "reasoning":        parsed.get("reasoning", ""),
    "closes":           market["closes"],
    "closes_in_days":   market["closes_in_days"],
    "status":           "open",
    "placed_at":        datetime.now(timezone.utc).isoformat(),
    "paper":            True,
    "model":            "assetbot-mechanical",
}

state["bankroll"] = round(state["bankroll"] - stake, 2)
state["trades"].append(trade)

log(f"  ✅ TRADE — {position} @ {entry_price}¢ | ${stake:.2f} stake | win ${payout:.2f}")
log(f"     {ticker} @ ${current_price:,.2f} | needs {parsed.get('yes_requires_move_pct', 0):.1f}% move | {market['question'][:65]}")
log(f"     Bankroll now ${state['bankroll']:.2f}")

return state
```

# ─────────────────────────────────────────────────────────

# RESOLVER

# ─────────────────────────────────────────────────────────

def resolve_open_trades(state):
open_trades = [t for t in state[“trades”] if t[“status”] == “open”]
if not open_trades:
return state
log(f”🔍 Checking {len(open_trades)} open position(s)…”)
now = datetime.now(timezone.utc)

```
for trade in open_trades:
    market_id = trade.get("market_id", "")
    closes_str = trade.get("closes", "")
    close_dt = datetime.fromisoformat(closes_str.replace("Z", "+00:00")) if closes_str else None

    if close_dt and now < close_dt:
        continue

    hours_past = (now - close_dt).total_seconds() / 3600 if close_dt else 0

    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=12
        )
        if r.status_code != 200:
            continue
        mkt = r.json()
        active = mkt.get("active", True)
        closed_flag = mkt.get("closed", False)
        gamma_lagging = active and not closed_flag and hours_past > 2

        if active and not closed_flag and not gamma_lagging:
            continue

        prices_raw = mkt.get("outcomePrices")
        if not prices_raw:
            continue
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        prices = [float(p) for p in prices]

        if len(prices) >= 2:
            yes_price = prices[0]
            no_price  = prices[1]
            if yes_price >= 0.99 or no_price >= 0.99:
                won = (no_price >= 0.99) if trade["position"] == "NO" else (yes_price >= 0.99)
                trade["status"]      = "closed"
                trade["won"]         = won
                trade["resolved_at"] = now.isoformat()
                if won:
                    payout = round(trade["stake"] * 100 / trade["entry_price"], 2)
                    trade["realized_pnl"] = round(payout - trade["stake"], 2)
                    state["bankroll"] = round(state["bankroll"] + payout, 2)
                    log(f"  ✅ WON +${trade['realized_pnl']:.2f} | {trade['market'][:55]}")
                else:
                    trade["realized_pnl"] = -trade["stake"]
                    log(f"  ❌ LOST -${trade['stake']:.2f} | {trade['market'][:55]}")
                if gamma_lagging:
                    log(f"  ⚡ Gamma lag override used")
            elif hours_past > 24:
                log(f"  ⚠️  {hours_past:.0f}h past close, prices not snapped — manual check needed: {trade['market'][:50]}")
    except Exception as e:
        log(f"  ⚠️  Resolve error {market_id}: {e}")

return state
```

# ─────────────────────────────────────────────────────────

# PORTFOLIO SUMMARY

# ─────────────────────────────────────────────────────────

def print_portfolio(state):
trades   = state[“trades”]
closed   = [t for t in trades if t[“status”] == “closed”]
open_t   = [t for t in trades if t[“status”] == “open”]
won      = [t for t in closed if t.get(“won”)]
pnl      = sum(t.get(“realized_pnl”, 0) for t in closed)
wr       = len(won) / len(closed) * 100 if closed else 0
roi      = (state[“bankroll”] - STARTING_BANKROLL) / STARTING_BANKROLL * 100

```
print("\n" + "═" * 60)
print("  ASSETBOT — Price Mismatch Scanner")
print("═" * 60)
print(f"  Bankroll      ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
print(f"  Realized P&L  ${pnl:+.2f}")
print(f"  Closed        {len(closed)} ({len(won)}W/{len(closed)-len(won)}L — {wr:.0f}% WR)")
print(f"  Open          {len(open_t)}")
print(f"  Scans         {state.get('scan_count', 0)}")
print("═" * 60)
if open_t:
    print("\n  OPEN POSITIONS:")
    for t in open_t:
        print(f"  {t['position']} @ {t['entry_price']}¢ | ${t['stake']:.2f} | "
              f"{t['ticker']} @ ${t['current_price']:,.2f} | "
              f"needs {t.get('required_move_pct', 0):.1f}% | {t['market'][:45]}")
print()
```

# ─────────────────────────────────────────────────────────

# MAIN

# ─────────────────────────────────────────────────────────

def single_scan():
now = datetime.now(timezone.utc)
print(”\n╔══════════════════════════════════════════════════════════╗”)
print(“║  ASSETBOT  ·  Price Mismatch Scanner                     ║”)
print(f”║  {now.strftime(’%Y-%m-%d %H:%M UTC’)}                                  ║”)
print(“╚══════════════════════════════════════════════════════════╝\n”)

```
if not ANTHROPIC_API_KEY:
    print("❌  ANTHROPIC_API_KEY not set")
    sys.exit(1)

state = load_state()
state["scan_count"] = state.get("scan_count", 0) + 1
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Step 0: Closing price snapshot
if should_take_snapshot(state):
    state = take_price_snapshot(state)
else:
    snap_date = state.get("price_snapshot", {}).get("date", "none")
    log(f"── Step 0: Using snapshot from {snap_date} ──────────────────")

# Step 1: Resolve
log("── Step 1: Resolve open trades ──────────────────────────")
state = resolve_open_trades(state)

# Step 2: Fetch markets
log("── Step 2: Fetch markets ────────────────────────────────")
markets = fetch_markets()
if not markets:
    save_state(state)
    return

# Step 3: Match assets and check mispricings
log("── Step 3: Scan for price mismatches ────────────────────")
open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
candidates = [m for m in markets if m["id"] not in open_ids]

trades_placed = 0
for market in candidates:
    ticker, asset_type = match_asset(market["question"])
    if not ticker:
        continue

    # Get reference price (snapshot if available, else live)
    price = get_reference_price(ticker, asset_type, state)
    if not price or not validate_price(ticker, price):
        continue

    # Haiku parse
    parsed = haiku_parse_threshold(client, market["question"], price, ticker)
    if not parsed or not parsed.get("threshold"):
        continue

    # Check mismatch
    if not check_mismatch(parsed, market["closes_in_days"]):
        continue

    log(f"  🎯 MISMATCH: {ticker} @ ${price:,.2f} | needs {parsed.get('yes_requires_move_pct', 0):.1f}% move for YES | {market['question'][:55]}")
    log(f"     Resolution: {parsed.get('resolution_type')} | Reasoning: {parsed.get('reasoning', '')[:80]}")

    state = place_trade(market, parsed, price, ticker, state)
    trades_placed += 1

if trades_placed == 0:
    log("  No mispricings found this scan")

# Step 4: Save
log("── Step 4: Save ─────────────────────────────────────────")
save_state(state)
print_portfolio(state)
```

if **name** == “**main**”:
if “–single-scan” in sys.argv:
single_scan()
else:
print(“Run with –single-scan”)