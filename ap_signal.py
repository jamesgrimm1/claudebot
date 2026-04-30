# ap_signal.py - AlphaPrime Signal Injection
# Called by nearcertain.py and nearcertain_beta.py after every trade placed.
# If the trade meets alpha_prime_config.json criteria, it is also written to
# alpha_prime_log.json at AlphaPrime's own stake sizing.
# AlphaPrime no longer runs its own scan - it only receives signals here.

import os, json, re
from datetime import datetime, timezone

AP_CONFIG_FILE = "alpha_prime_config.json"
AP_LOG_FILE    = "alpha_prime_log.json"

# Pattern stake sizing (mirrors alpha_prime.py PATTERNS)
PATTERNS = {
    "A": {"pct": 0.030, "cap": 50.0, "label": "Weather Exact-Temp"},
    "B": {"pct": 0.030, "cap": 50.0, "label": "Soccer O/U"},
    "C": {"pct": 0.025, "cap": 40.0, "label": "Esports CS/Dota/LoL"},
    "D": {"pct": 0.040, "cap": 50.0, "label": "Watchlist City Boost"},
    "E": {"pct": 0.001, "cap":  2.0,  "label": "Bracket Markets (micro)"},
}

DIRECTIONAL_KW = [
    "or above", "or below", "or higher", "or lower",
    "at least", "at most", "more than", "less than", "exceed", "between"
]
SOCCER_KW      = ["soccer", "football", "premier league", "la liga", "serie a",
                   "bundesliga", "ligue 1", "champions league", "mls", "eredivisie"]
SOCCER_OU_KW   = ["over", "under", "o/u", "total goals", "btts", "both teams"]
ESPORTS_GAMES  = {
    "cs":   ["counter-strike", "cs2", "csgo", " cs:", "(cs "],
    "dota": ["dota 2", "dota2"],
    "lol":  ["league of legends", "lol:", "(lol"],
}
BRACKET_NAMES  = ["trump", "tariff", "s&p", "nasdaq", "bitcoin", "btc", "gold"]
CONFLICT_KW    = ["israel", "yemen", "russia", "ukraine", "iran", "hormuz",
                  "hamas", "hezbollah"]
VALORANT_KW    = ["valorant"]


def _load_config():
    if os.path.exists(AP_CONFIG_FILE):
        try:
            with open(AP_CONFIG_FILE) as f:
                return json.load(f)
        except:
            pass
    # Defaults matching alpha_prime_config.json
    return {
        "blocked_categories": ["politics", "economics", "conflict", "geopolitics"],
        "weather_exact_only": True,
        "yes_price_min": 90,
        "yes_price_max": 94,
        "max_closes_in_days": 0.25,
        "min_volume": 500,
        "watchlist_cities": ["jakarta", "karachi", "guangzhou"],
        "soccer_ou_enabled": True,
        "soccer_yes_min": 90,
        "soccer_yes_max": 94,
        "esports_enabled": True,
        "esports_games": ["counter-strike", "dota 2", "league of legends"],
        "esports_yes_min": 70,
        "esports_yes_max": 95,
        "pattern_e_enabled": True,
    }


def _load_ap_log():
    if os.path.exists(AP_LOG_FILE):
        try:
            with open(AP_LOG_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"bankroll": 1000.0, "trades": [], "scan_count": 0}


def _save_ap_log(state):
    with open(AP_LOG_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _classify(trade, cfg):
    """
    Classify a NearCertain trade against AP criteria.
    Returns (pattern_letter, reason) or (None, reason).

    trade keys expected: market, category, entry_yes_price, entry_no_price,
                         closes_in_days, volume
    """
    question  = trade.get("market", "")
    m         = question.lower()
    cat       = trade.get("category", "").lower()
    yes_price = trade.get("entry_yes_price", 0)
    no_price  = trade.get("entry_no_price", 0)
    cid       = trade.get("closes_in_days", 99)
    volume    = trade.get("volume", 0)

    blocked   = set(cfg.get("blocked_categories", []))
    yes_min   = cfg.get("yes_price_min", 90)
    yes_max   = cfg.get("yes_price_max", 94)
    max_cid   = cfg.get("max_closes_in_days", 0.25)
    cities    = set(cfg.get("watchlist_cities", []))

    # Hard exclusions
    if yes_price >= 95:
        return None, "excl:YES>=95"
    if no_price >= 35:
        return None, "excl:NO>=35"
    if cat in blocked:
        return None, f"excl:blocked:{cat}"
    if any(k in m for k in VALORANT_KW):
        return None, "excl:valorant"
    if any(k in m for k in CONFLICT_KW):
        return None, "excl:conflict"

    # YES price range gate (audit-driven)
    if not (yes_min <= yes_price <= yes_max):
        return None, f"excl:YES {yes_price} outside {yes_min}-{yes_max}"

    # Time gate
    if cid > max_cid:
        return None, f"excl:cid {cid:.3f} > {max_cid}"

    # Volume gate
    if volume < cfg.get("min_volume", 500):
        return None, f"excl:volume {volume:.0f} < {cfg.get('min_volume', 500)}"

    # Pattern D — Watchlist city weather (higher stake)
    if cat == "weather":
        is_directional = any(k in m for k in DIRECTIONAL_KW)
        if cfg.get("weather_exact_only", True) and is_directional:
            return None, "excl:directional_weather"
        city_hit = any(city in m for city in cities)
        if city_hit:
            return "D", f"watchlist_city"
        return "A", "weather_exact"

    # Pattern B — Soccer O/U
    if cfg.get("soccer_ou_enabled", True):
        s_yes_min = cfg.get("soccer_yes_min", 90)
        s_yes_max = cfg.get("soccer_yes_max", 94)
        if any(k in m for k in SOCCER_KW) and any(k in m for k in SOCCER_OU_KW):
            if s_yes_min <= yes_price <= s_yes_max:
                return "B", "soccer_ou"

    # Pattern C — Esports
    if cfg.get("esports_enabled", True):
        e_yes_min = cfg.get("esports_yes_min", 70)
        e_yes_max = cfg.get("esports_yes_max", 95)
        enabled_games = cfg.get("esports_games", ["counter-strike", "dota 2", "league of legends"])
        for game, keywords in ESPORTS_GAMES.items():
            if any(eg in " ".join(keywords) for eg in enabled_games):
                if any(k in m for k in keywords):
                    if e_yes_min <= yes_price <= e_yes_max:
                        return "C", f"esports_{game}"

    # Pattern E — Bracket micro-stake
    if cfg.get("pattern_e_enabled", True):
        if any(n in m for n in BRACKET_NAMES) and re.search(r"\d+-\d+", m):
            return "E", "bracket"

    return None, "no_pattern_match"


def _calc_ap_stake(pattern, ap_bankroll, volume):
    cfg  = PATTERNS[pattern]
    base = ap_bankroll * cfg["pct"]
    stake = min(base, cfg["cap"])
    stake = min(stake, volume * 0.05)  # VOL_CAP 5%
    stake = round(stake, 2)
    return stake if stake >= 1.0 else None


def signal_to_alpha_prime(trade, source_bot="nearcertain"):
    """
    Call this immediately after placing a trade in NearCertain or NearCertain Beta.
    If the trade matches AP criteria, it will be written to alpha_prime_log.json.

    trade: the trade dict just appended to NC state (must have: market, category,
           entry_yes_price, entry_no_price, closes_in_days, volume, market_id,
           closes, placed_at)
    source_bot: "nearcertain" or "nearcertain_beta"
    """
    try:
        cfg = _load_config()
        pattern, reason = _classify(trade, cfg)

        if pattern is None:
            return  # doesn't qualify for AP — silent

        ap_state = _load_ap_log()
        ap_bankroll = ap_state.get("bankroll", 1000.0)

        # Dedup — already have this market in AP?
        open_ids = {t["market_id"] for t in ap_state["trades"] if t["status"] == "open"}
        if trade.get("market_id") in open_ids:
            return

        # Stake at AP sizing
        volume = trade.get("volume", 0)
        stake  = _calc_ap_stake(pattern, ap_bankroll, volume)
        if not stake:
            return

        # Hard cap: stake must not exceed remaining AP bankroll
        if stake > ap_bankroll:
            stake = round(ap_bankroll * 0.05, 2)
        if stake < 1.0:
            return

        payout = round(stake * 100 / trade["entry_no_price"], 2)
        profit = round(payout - stake, 2)

        ap_trade = {
            "id":               f"AP{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "market_id":        trade["market_id"],
            "market":           trade["market"],
            "category":         trade.get("category", "other"),
            "pattern":          pattern,
            "pattern_label":    PATTERNS[pattern]["label"],
            "pattern_reason":   reason,
            "position":         "NO",
            "entry_no_price":   trade["entry_no_price"],
            "entry_yes_price":  trade["entry_yes_price"],
            "stake":            stake,
            "potential_profit": profit,
            "potential_payout": payout,
            "closes":           trade.get("closes", ""),
            "closes_in_days":   trade.get("closes_in_days", 0),
            "status":           "open",
            "placed_at":        datetime.now(timezone.utc).isoformat(),
            "source_bot":       source_bot,
            "paper":            True,
            "model":            "alpha-prime-signal",
        }

        ap_state["bankroll"] = round(ap_bankroll - stake, 2)
        ap_state["trades"].append(ap_trade)
        _save_ap_log(ap_state)

        print(f"  [AP] Signal [{pattern}:{reason}] NO @ {trade['entry_no_price']}c "
              f"| ${stake:.2f} stake | {trade['market'][:50]}")

    except Exception as e:
        # Never crash NearCertain because AP signal failed
        print(f"  [AP] Signal error (non-fatal): {e}")
