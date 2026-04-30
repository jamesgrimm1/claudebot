"""
╔══════════════════════════════════════════════════════════╗
║  ALPHA-PRIME — Focused NearCertain Variant               ║
║                                                          ║
║  Five mechanical patterns, no LLM reasoning layer.       ║
║  Pattern A: Weather exact-temp NO (YES 86-95, <6h)       ║
║  Pattern B: Soccer O/U NO (YES 81-95, <6h)               ║
║  Pattern C: Esports CS/Dota/LoL NO (<6h)                 ║
║  Pattern D: Watchlist city boost on Pattern A            ║
║  Pattern E: Bracket markets (micro-stake, data only)     ║
║                                                          ║
║  RUN: python alpha_prime.py --single-scan                ║
╚══════════════════════════════════════════════════════════╝
"""

import os, sys, re, json, time, requests
from datetime import datetime, timezone, timedelta

LOG_FILE          = "alpha_prime_log.json"
AP_CONFIG_FILE    = "alpha_prime_config.json"
PAPER_TRADING     = True
STARTING_BANKROLL = 1000.00


# ─────────────────────────────────────────────────────────
#  SELF-AUDIT CONFIG LOADER
# ─────────────────────────────────────────────────────────
# Opus updates alpha_prime_config.json every 3 days via self_audit.py
# We load it here and override pattern parameters accordingly

def load_audit_config():
    """Load Opus-generated config from self_audit.py, fall back to defaults."""
    if not os.path.exists(AP_CONFIG_FILE):
        return {}
    try:
        with open(AP_CONFIG_FILE) as f:
            cfg = json.load(f)
        print(f"[CONFIG] Loaded audit config from {AP_CONFIG_FILE} "
              f"(updated: {cfg.get('last_updated','?')})")
        print(f"[CONFIG] Notes: {cfg.get('audit_notes','')}")
        return cfg
    except Exception as e:
        print(f"[CONFIG] Failed to load audit config: {e} — using defaults")
        return {}

_AUDIT_CFG = load_audit_config()

# ── Circuit breakers ──────────────────────────────────────
EQUITY_STOP_PCT   = 0.70   # halve stakes if bankroll < 70% of starting
DAILY_LOSS_PCT    = 0.40   # halve stakes if same-day loss > 40% of starting

# ── Pattern stake config ──────────────────────────────────
PATTERNS = {
    "A": {"pct": 0.030, "cap": 50.0, "label": "Weather Exact-Temp"},
    "B": {"pct": 0.030, "cap": 50.0, "label": "Soccer O/U"},
    "C": {"pct": 0.025, "cap": 40.0, "label": "Esports CS/Dota/LoL"},
    "D": {"pct": 0.040, "cap": 50.0, "label": "Watchlist City Boost"},
    "E": {"pct": 0.001, "cap":  2.0, "label": "Bracket Markets (micro)"},
}

MIN_STAKE = 1.00
VOL_CAP   = 0.05   # max 5% of market volume

# ── Watchlist (Pattern D) ─────────────────────────────────
WATCHLIST_CITIES = {"jakarta", "karachi", "guangzhou"}

# ── Hard exclusions ────────────────────────────────────────
DIRECTIONAL_KW = [
    "or above","or below","or higher","or lower",
    "at least","at most","more than","less than","exceed","between"
]
VALORANT_KW    = ["valorant"]
CONFLICT_KW    = ["israel","yemen","russia","ukraine","iran","hormuz","hamas","hezbollah"]

# Override from audit config if available
if _AUDIT_CFG.get("blocked_categories"):
    _AUDIT_BLOCKED = set(_AUDIT_CFG["blocked_categories"])
else:
    _AUDIT_BLOCKED = set()

if _AUDIT_CFG.get("watchlist_cities"):
    WATCHLIST_CITIES = set(_AUDIT_CFG["watchlist_cities"])

if _AUDIT_CFG.get("yes_price_min"):
    _AP_YES_MIN = _AUDIT_CFG["yes_price_min"]
    _AP_YES_MAX = _AUDIT_CFG.get("yes_price_max", 95)
else:
    _AP_YES_MIN = 86
    _AP_YES_MAX = 95

if _AUDIT_CFG.get("max_closes_in_days"):
    _AP_MAX_CID = _AUDIT_CFG["max_closes_in_days"]
else:
    _AP_MAX_CID = 0.25

if _AUDIT_CFG.get("soccer_ou_enabled") is False:
    _AP_SOCCER = False
else:
    _AP_SOCCER = True
    _AP_SOCCER_YES_MIN = _AUDIT_CFG.get("soccer_yes_min", 81)
    _AP_SOCCER_YES_MAX = _AUDIT_CFG.get("soccer_yes_max", 95)

if _AUDIT_CFG.get("esports_enabled") is False:
    _AP_ESPORTS = False
else:
    _AP_ESPORTS = True

if _AUDIT_CFG.get("esports_games"):
    # Rebuild ESPORTS_GAMES from audit config
    _ap_eg = _AUDIT_CFG["esports_games"]
    ESPORTS_GAMES = {
        "cs":   ["counter-strike","cs2","csgo"," cs:","(cs "] if any("counter" in g or "cs" in g for g in _ap_eg) else [],
        "dota": ["dota 2","dota2"] if any("dota" in g for g in _ap_eg) else [],
        "lol":  ["league of legends","lol:","(lol"] if any("league" in g or "lol" in g for g in _ap_eg) else [],
    }
    ESPORTS_GAMES = {k: v for k, v in ESPORTS_GAMES.items() if v}

# ── Soccer league detection ───────────────────────────────
SOCCER_KW      = ["o/u","over/under"]
SOCCER_LEAGUE_KW = [
    "epl","premier league","la liga","serie a","bundesliga","ligue 1",
    "mls","champions league","europa league","fa cup","copa del rey",
    "eredivisie","fc ","united fc"," vs. "," vs "," fc","afc ",
    "atletico","barcelona","real madrid","ajax","psg","milan","juventus",
    "inter ","arsenal","chelsea","liverpool","tottenham","manchester",
    "brighton","burnley","fulham","sunderland","nottingham","aston villa",
    "brentford","wolves","leicester","newcastle","randers","odense",
    "kobenhavn","lyon","marseille","monaco","lens","nantes","rennes",
    "macarthur","wellington phoenix","perth glory","melbourne city",
    "kasimpasa","basaksehir","alverca","arouca","columbus","galaxy",
]

# ── Esports detection ─────────────────────────────────────
ESPORTS_GAMES = {
    "cs": ["counter-strike","cs2","csgo"," cs:","(cs "],
    "dota": ["dota 2","dota2"],
    "lol": ["league of legends","lol:","(lol"],
}

# ── Bracket market detection ──────────────────────────────
BRACKET_NAMES  = ["trump","cruz","zelenskyy","musk","biden","harris","vance","newsom","putin"]
BRACKET_VERBS  = ["post","tweet","publish"]


# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            s = json.load(f)
        closed = [t for t in s["trades"] if t["status"] == "closed"]
        won = [t for t in closed if t.get("won")]
        log(f"📂 Loaded — {len(s['trades'])} trades | bankroll ${s['bankroll']:.2f} | "
            f"{len(won)}W/{len(closed)-len(won)}L")
        return s
    log("📂 Fresh start")
    return {
        "bankroll":    STARTING_BANKROLL,
        "trades":      [],
        "daily_loss":  0.0,
        "daily_reset": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scan_count":  0,
        "started":     datetime.now(timezone.utc).isoformat(),
        "watchlist":   list(WATCHLIST_CITIES),
        "watchlist_log": [],
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
    return state


# ─────────────────────────────────────────────────────────
#  CIRCUIT BREAKERS
# ─────────────────────────────────────────────────────────

def get_stake_multiplier(state):
    """Returns 0.5 if either circuit breaker is active, else 1.0."""
    equity_stop = state["bankroll"] < STARTING_BANKROLL * EQUITY_STOP_PCT
    daily_stop  = state.get("daily_loss", 0) > STARTING_BANKROLL * DAILY_LOSS_PCT
    if equity_stop or daily_stop:
        if equity_stop:
            log(f"  ⚡ Equity stop active (bankroll ${state['bankroll']:.2f} < "
                f"${STARTING_BANKROLL * EQUITY_STOP_PCT:.2f})")
        if daily_stop:
            log(f"  ⚡ Daily loss stop active (${state['daily_loss']:.2f} lost today)")
        return 0.5
    return 1.0


# ─────────────────────────────────────────────────────────
#  STAKE CALCULATION
# ─────────────────────────────────────────────────────────

def calc_stake(pattern, market_volume, state):
    cfg = PATTERNS[pattern]
    base   = state["bankroll"] * cfg["pct"]
    stake  = min(base, cfg["cap"])
    stake  = min(stake, market_volume * VOL_CAP)
    stake  = stake * get_stake_multiplier(state)
    stake  = round(stake, 2)
    if stake < MIN_STAKE:
        return None, "stake too small after caps"
    return stake, None


# ─────────────────────────────────────────────────────────
#  PATTERN MATCHING
# ─────────────────────────────────────────────────────────

def is_directional_weather(market):
    m = market.lower()
    return any(k in m for k in DIRECTIONAL_KW)

def is_soccer_market(market):
    m = market.lower()
    return any(k in m for k in SOCCER_KW) and any(k in m for k in SOCCER_LEAGUE_KW)

def get_esports_game(market):
    m = market.lower()
    for game, keywords in ESPORTS_GAMES.items():
        if any(k in m for k in keywords):
            return game
    return None

def is_bracket_market(market):
    m = market.lower()
    if not any(n in m for n in BRACKET_NAMES):
        return False
    if not re.search(r"\d+-\d+", m):
        return False
    return any(v in m for v in BRACKET_VERBS)

def get_watchlist(state):
    return set(state.get("watchlist", list(WATCHLIST_CITIES)))

def classify_market(market_dict, state):
    """
    Returns (pattern_letter, reason) or (None, reason) if excluded.
    Hard exclusions override everything.
    """
    q          = market_dict["question"]
    m          = q.lower()
    cat        = market_dict.get("category", "").lower()
    no_price   = market_dict.get("no_price", 0)
    yes_price  = market_dict.get("yes_price", 0)
    cid        = market_dict.get("closes_in_days", 99)
    volume     = market_dict.get("volume", 0)

    # ── Hard exclusions (from audit config) ─────────────
    if yes_price >= 95:
        return None, "excl:YES>=95 (0/10 in data)"
    if no_price >= 35:
        return None, "excl:NO>=35"
    if cat == "crypto":
        return None, "excl:crypto"
    if _AUDIT_BLOCKED and cat in _AUDIT_BLOCKED:
        return None, f"excl:audit_blocked:{cat}"
    if any(k in m for k in VALORANT_KW):
        return None, "excl:valorant (0/6 in data)"
    if any(k in m for k in CONFLICT_KW):
        return None, "excl:conflict"
    if cat == "weather" and is_directional_weather(q):
        return None, "excl:directional_weather (-62% EV)"
    if cat == "weather" and "between" in m:
        return None, "excl:range_weather"
    if cid > 1.0 and cat not in []:  # Pattern E allows up to 1d, A/B/C require <0.25
        pass  # check individually per pattern below

    # ── Pattern D/A: Weather exact-temp ─────────────────
    if cat == "weather":
        _yes_max_a = 100 - 5  # NO 5-14 means YES 86-95
        _yes_min_a = 100 - 14
        if 5 <= no_price <= 14 and cid < _AP_MAX_CID:
            if not is_directional_weather(q) and "between" not in m:
                if volume >= 500:
                    watchlist = get_watchlist(state)
                    for city in watchlist:
                        if city in m:
                            return "D", f"watchlist:{city} no={no_price} cid={cid:.3f}"
                    return "A", f"weather_exact no={no_price} cid={cid:.3f}"
                else:
                    return None, f"excl:vol<500 ({volume:.0f})"
        return None, f"no_match:weather no={no_price} cid={cid:.3f}"

    # ── Pattern B: Soccer O/U ────────────────────────────
    if any(k in m for k in SOCCER_KW):
        if 5 <= no_price <= 19 and cid < 0.25:
            if is_soccer_market(q):
                if volume >= 500:
                    return "B", f"soccer_ou no={no_price} cid={cid:.3f}"
                return None, f"excl:vol<500 ({volume:.0f})"
        return None, f"no_match:soccer no={no_price} cid={cid:.3f}"

    # ── Pattern C: Esports ───────────────────────────────
    game = get_esports_game(q)
    if game:
        if 5 <= no_price <= 30 and cid < 0.25:
            if volume >= 500:
                return "C", f"esports:{game} no={no_price} cid={cid:.3f}"
            return None, f"excl:vol<500 ({volume:.0f})"
        return None, f"no_match:esports no={no_price} cid={cid:.3f}"

    # ── Pattern E: Bracket markets ───────────────────────
    if is_bracket_market(q):
        if 5 <= no_price <= 19 and cid < 1.0:
            if volume >= 200:
                return "E", f"bracket no={no_price} cid={cid:.3f}"
            return None, f"excl:vol<200 ({volume:.0f})"

    return None, f"no_match:cat={cat} no={no_price}"


# ─────────────────────────────────────────────────────────
#  RESOLVER
# ─────────────────────────────────────────────────────────

def settle(trade, won, state):
    if trade.get("status") == "closed":
        return
    trade["status"]      = "closed"
    trade["won"]         = won
    trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
    if won:
        payout                = round(trade["stake"] * 100 / trade["entry_no_price"], 2)
        trade["realized_pnl"] = round(payout - trade["stake"], 2)
        state["bankroll"]     = round(state["bankroll"] + payout, 2)
        log(f"  ✅ WON +${trade['realized_pnl']:.2f} [{trade.get('pattern','?')}] {trade['market'][:55]}")
    else:
        trade["realized_pnl"]  = round(-trade["stake"], 2)
        state["daily_loss"]    = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST -${trade['stake']:.2f} [{trade.get('pattern','?')}] {trade['market'][:55]}")

def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state
    log(f"🔍 Checking {len(open_trades)} open position(s)...")
    now = datetime.now(timezone.utc)

    for trade in open_trades:
        market_id = trade.get("market_id", "")
        closes_str = trade.get("closes", "")
        try:
            close_dt = datetime.fromisoformat(closes_str.replace("Z", "+00:00")) if closes_str else None
        except:
            close_dt = None

        if close_dt and now < close_dt:
            continue

        hours_past = (now - close_dt).total_seconds() / 3600 if close_dt else 0

        resolved = False
        for url in [
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            f"https://gamma-api.polymarket.com/markets?id={market_id}",
        ]:
            if resolved:
                break
            try:
                r = requests.get(url, timeout=10)
                if r.status_code != 200:
                    continue
                raw = r.json()
                mkt = raw[0] if isinstance(raw, list) and raw else raw
                active      = mkt.get("active", True)
                closed_flag = mkt.get("closed", False)
                gamma_lag   = active and not closed_flag and hours_past > 2

                if active and not closed_flag and not gamma_lag:
                    continue

                prices_raw = mkt.get("outcomePrices")
                if not prices_raw:
                    continue
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                prices = [float(p) for p in prices]
                if len(prices) >= 2:
                    yes_p = prices[0]
                    no_p  = prices[1]
                    if yes_p >= 0.99 or no_p >= 0.99:
                        won = (no_p >= 0.99)  # we always hold NO
                        if gamma_lag:
                            log(f"  ⚡ Gamma lag override: {trade['market'][:50]}")
                        settle(trade, won, state)
                        resolved = True
                    elif hours_past > 24:
                        log(f"  ⚠️  {hours_past:.0f}h past close, not snapped: {trade['market'][:50]}")
            except Exception as e:
                log(f"  ⚠️  Resolve error {market_id}: {e}")

    return state


# ─────────────────────────────────────────────────────────
#  MARKET FETCH
# ─────────────────────────────────────────────────────────

def fetch_markets():
    all_markets = []
    for offset in [0, 500]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets"
                "?active=true&closed=false&limit=500"
                f"&offset={offset}&order=volume&ascending=false",
                timeout=12
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            all_markets.extend(batch)
        except Exception as e:
            log(f"⚠️  Market fetch error: {e}")
            break

    now = datetime.now(timezone.utc)
    parsed = []
    for m in all_markets:
        if not m.get("question") or not m.get("outcomePrices"):
            continue
        if m.get("negRisk", False):
            continue
        end_str = m.get("endDate") or m.get("end_date") or ""
        if not end_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except:
            continue
        cid   = (end_dt - now).total_seconds() / 86400
        hours = cid * 24
        if hours < 0.5 or cid > 1.0:  # Pattern E allows up to 1 day
            continue
        try:
            prices = json.loads(m["outcomePrices"])
            yes_p  = round(float(prices[0]) * 100)
            no_p   = round(float(prices[1]) * 100)
        except:
            continue
        # Quick pre-filter: we only ever buy NO
        if no_p < 5 or no_p >= 35:
            continue
        # Determine category
        q = m["question"].lower()
        if any(k in q for k in ["temperature","°c","°f","celsius","fahrenheit"]):
            cat = "weather"
        elif any(k in q for k in ["bitcoin","btc","ethereum","eth","solana","xrp","crypto"]):
            cat = "crypto"
        else:
            cat = m.get("category", "other").lower()

        parsed.append({
            "id":             str(m.get("id", "")),
            "slug":           m.get("slug", ""),
            "question":       m["question"],
            "yes_price":      yes_p,
            "no_price":       no_p,
            "volume":         float(m.get("volume", 0)),
            "category":       cat,
            "closes":         end_dt.isoformat(),
            "closes_in_days": round(cid, 3),
        })

    log(f"📋 Fetched {len(parsed)} candidate markets")
    return parsed


# ─────────────────────────────────────────────────────────
#  PLACE TRADE
# ─────────────────────────────────────────────────────────

def place_trade(market, pattern, reason, state):
    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    if market["id"] in open_ids:
        return state

    stake, err = calc_stake(pattern, market["volume"], state)
    if stake is None:
        log(f"  ⏭  {err} — {market['question'][:55]}")
        return state

    entry_no = market["no_price"]
    payout   = round(stake * 100 / entry_no, 2)
    profit   = round(payout - stake, 2)

    trade = {
        "id":                f"AP{int(time.time()*1000)}",
        "market_id":         market["id"],
        "market_slug":       market.get("slug", ""),
        "market":            market["question"],
        "pattern":           pattern,
        "triggered_by":      reason,
        "category":          market["category"],
        "position":          "NO",
        "entry_no_price":    entry_no,
        "entry_yes_price":   market["yes_price"],
        "stake":             stake,
        "payout_if_wins":    payout,
        "profit_if_wins":    profit,
        "volume_at_entry":   market["volume"],
        "closes_in_days":    market["closes_in_days"],
        "closes":            market["closes"],
        "bankroll_at_entry": state["bankroll"],
        "bankroll_fraction": round(stake / state["bankroll"], 4),
        "status":            "open",
        "placed_at":         datetime.now(timezone.utc).isoformat(),
        "paper":             True,
        "model":             "alpha-prime",
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  ✅ [{pattern}] NO @ {entry_no}¢ | ${stake:.2f} stake → win ${payout:.2f} | "
        f"{market['question'][:55]}")
    log(f"     {reason} | vol=${market['volume']:,.0f} | bankroll ${state['bankroll']:.2f}")
    return state


# ─────────────────────────────────────────────────────────
#  WATCHLIST MAINTENANCE
# ─────────────────────────────────────────────────────────

def update_watchlist(state):
    """Weekly watchlist update based on recent Pattern A/D performance."""
    from collections import defaultdict
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    closed = [
        t for t in state["trades"]
        if t["status"] == "closed"
        and t.get("pattern") in ("A", "D")
        and datetime.fromisoformat(t.get("resolved_at", now.isoformat()).replace("Z","+00:00")) > cutoff
    ]

    city_stats = defaultdict(lambda: {"w": 0, "l": 0})
    for t in closed:
        q = t["market"].lower()
        for city in ["jakarta","karachi","guangzhou","singapore","mumbai","delhi","bangkok","manila","seoul","tokyo","london","paris","madrid","berlin","sydney","dubai","istanbul","cairo","nairobi"]:
            if city in q:
                if t.get("won"):
                    city_stats[city]["w"] += 1
                else:
                    city_stats[city]["l"] += 1
                break

    current = set(state.get("watchlist", list(WATCHLIST_CITIES)))
    changes = []

    for city, s in city_stats.items():
        total = s["w"] + s["l"]
        if city not in current and s["w"] >= 3:
            current.add(city)
            changes.append(f"ADD {city} ({s['w']}W/{s['l']}L in 30d)")
        elif city in current and total >= 4 and s["w"] == 0:
            current.discard(city)
            changes.append(f"REMOVE {city} (0W/{total}L in 30d)")

    if changes:
        log(f"  🗺️  Watchlist updated: {', '.join(changes)}")
        state["watchlist"] = list(current)
        state["watchlist_log"] = state.get("watchlist_log", []) + [
            {"date": now.strftime("%Y-%m-%d"), "changes": changes}
        ]
    return state


# ─────────────────────────────────────────────────────────
#  PORTFOLIO SUMMARY
# ─────────────────────────────────────────────────────────

def print_portfolio(state):
    trades  = state["trades"]
    closed  = [t for t in trades if t["status"] == "closed"]
    open_t  = [t for t in trades if t["status"] == "open"]
    won     = [t for t in closed if t.get("won")]
    pnl     = sum(t.get("realized_pnl", 0) for t in closed)
    wr      = len(won) / len(closed) * 100 if closed else 0
    roi     = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100

    print("\n" + "═" * 60)
    print("  ALPHA-PRIME  ·  Focused NearCertain Variant")
    print("═" * 60)
    print(f"  Bankroll      ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L  ${pnl:+.2f}")
    print(f"  Closed        {len(closed)} ({len(won)}W/{len(closed)-len(won)}L — {wr:.0f}% WR)")
    print(f"  Open          {len(open_t)}")
    print(f"  Scans         {state.get('scan_count', 0)}")
    print(f"  Watchlist     {', '.join(state.get('watchlist', []))}")

    from collections import defaultdict
    by_pat = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for t in closed:
        p = t.get("pattern", "?")
        if t.get("won"):
            by_pat[p]["w"] += 1
        else:
            by_pat[p]["l"] += 1
        by_pat[p]["pnl"] += t.get("realized_pnl", 0)
    print()
    for pat in sorted(by_pat.keys()):
        v = by_pat[pat]
        n = v["w"] + v["l"]
        wr_p = v["w"] / n * 100 if n else 0
        label = PATTERNS.get(pat, {}).get("label", pat)
        print(f"  [{pat}] {label:<25} {v['w']}W/{v['l']}L {wr_p:.0f}% WR  ${v['pnl']:+.2f}")
    print("═" * 60)


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def single_scan():
    """
    V2 Architecture: AlphaPrime no longer scans for trades.
    Trades arrive via ap_signal.py (called by nearcertain.py + nearcertain_beta.py).
    This function ONLY resolves open trades and saves state.
    """
    now = datetime.now(timezone.utc)
    print("\n=== ALPHA-PRIME  Resolution-Only Mode ===")
    print(f"    Trades arrive via NearCertain signal injection")
    print(f"    {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    state = load_state()
    state = reset_daily_loss(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    open_before = len([t for t in state["trades"] if t["status"] == "open"])
    log(f"  Open trades: {open_before}")

    # Resolve only — no scan, no fetch, no classify
    log("-- Step 1: Resolve open trades")
    state = resolve_open_trades(state)

    open_after = len([t for t in state["trades"] if t["status"] == "open"])
    resolved = open_before - open_after
    if resolved > 0:
        log(f"  Resolved {resolved} trade(s) this cycle")

    log("-- Step 2: Save")
    save_state(state)
    print_portfolio(state)


if __name__ == "__main__":
    if "--single-scan" in sys.argv:
        single_scan()
    else:
        print("Run with --single-scan")
