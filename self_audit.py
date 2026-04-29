# SELF AUDIT - NearCertain -> AlphaPrime Auto-Updater
# Runs every 3 days via cron
# Reads nearcertain_log.json + nearcertain_beta_log.json
# Opus analyzes performance -> updates alpha_prime_config.json
# AlphaPrime reads that config at startup
# RUN: python self_audit.py

import os, sys, json, re, anthropic
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
NC_LOG               = "nearcertain_log.json"
NC_BETA_LOG          = "nearcertain_beta_log.json"
AP_CONFIG            = "alpha_prime_config.json"
AUDIT_STATE_FILE     = "self_audit_state.json"
OPUS_MODEL           = "claude-opus-4-5"
AUDIT_INTERVAL_DAYS  = 3
LOOKBACK_DAYS        = 30


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------
#  TIMING
# ---------------------------------------------------------

def should_audit():
    if not os.path.exists(AUDIT_STATE_FILE):
        return True
    try:
        with open(AUDIT_STATE_FILE) as f:
            s = json.load(f)
        last = datetime.fromisoformat(s.get("last_audit_utc", "").replace("Z", "+00:00"))
        days_elapsed = (datetime.now(timezone.utc) - last).days
        log(f"  Last audit: {days_elapsed} days ago (threshold: {AUDIT_INTERVAL_DAYS}d)")
        return days_elapsed >= AUDIT_INTERVAL_DAYS
    except:
        return True


def save_audit_timestamp():
    with open(AUDIT_STATE_FILE, "w") as f:
        json.dump({"last_audit_utc": datetime.now(timezone.utc).isoformat()}, f)


# ---------------------------------------------------------
#  DATA LOADING & ANALYSIS
# ---------------------------------------------------------

def load_log(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("trades", [])


def analyse_trades(trades, label, lookback_days=LOOKBACK_DAYS):
    """Compute detailed performance stats for Opus."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    closed = [
        t for t in trades
        if t.get("status") == "closed"
        and t.get("resolved_at", "")
        and datetime.fromisoformat(t["resolved_at"].replace("Z", "+00:00")) > cutoff
    ]

    if not closed:
        return f"## {label}\nNo resolved trades in last {lookback_days} days.\n"

    won   = [t for t in closed if t.get("won")]
    pnl   = sum(t.get("realized_pnl", 0) for t in closed)
    wr    = len(won) / len(closed) * 100

    lines = [
        f"## {label}",
        f"Total (last {lookback_days}d): {len(closed)} | {len(won)}W/{len(closed)-len(won)}L | {wr:.1f}% WR | ${pnl:+.2f} P&L",
        "",
    ]

    # By category
    cats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0, "stakes": []})
    for t in closed:
        c = t.get("category", "other")
        cats[c]["w" if t.get("won") else "l"] += 1
        cats[c]["pnl"] += t.get("realized_pnl", 0)
        cats[c]["stakes"].append(t.get("stake", 0))

    lines.append("### By Category")
    for cat, v in sorted(cats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        n = v["w"] + v["l"]
        wr_c = v["w"]/n*100 if n else 0
        avg_stake = sum(v["stakes"])/len(v["stakes"]) if v["stakes"] else 0
        lines.append(f"  {cat:<18} {n:>3} trades | {v['w']}W/{v['l']}L | {wr_c:.0f}% WR | ${v['pnl']:+.2f} | avg stake ${avg_stake:.2f}")

    # By YES price band
    bands = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for t in closed:
        yes = t.get("entry_yes_price", t.get("yes_price", 0))
        if yes:
            band = f"{(int(yes)//5)*5}-{(int(yes)//5)*5+4}"
            bands[band]["w" if t.get("won") else "l"] += 1
            bands[band]["pnl"] += t.get("realized_pnl", 0)

    lines.append("\n### By YES Price Band (NO position)")
    for band, v in sorted(bands.items(), key=lambda x: int(x[0].split("-")[0])):
        n = v["w"] + v["l"]
        wr_b = v["w"]/n*100 if n else 0
        lines.append(f"  YES {band}c  {n:>3} trades | {v['w']}W/{v['l']}L | {wr_b:.0f}% WR | ${v['pnl']:+.2f}")

    # By time to close
    time_buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for t in closed:
        cid = t.get("closes_in_days", 0)
        if cid < 0.25:
            bucket = "<6h"
        elif cid < 1:
            bucket = "6h-24h"
        elif cid < 3:
            bucket = "1-3d"
        else:
            bucket = "3d+"
        time_buckets[bucket]["w" if t.get("won") else "l"] += 1
        time_buckets[bucket]["pnl"] += t.get("realized_pnl", 0)

    lines.append("\n### By Time to Close at Entry")
    order = ["<6h", "6h-24h", "1-3d", "3d+"]
    for bucket in order:
        if bucket in time_buckets:
            v = time_buckets[bucket]
            n = v["w"] + v["l"]
            wr_t = v["w"]/n*100 if n else 0
            lines.append(f"  {bucket:<8}  {n:>3} trades | {v['w']}W/{v['l']}L | {wr_t:.0f}% WR | ${v['pnl']:+.2f}")

    # Weather: exact-temp vs directional
    DIRECTIONAL = ["or above","or below","or higher","or lower","at least","exceed","between"]
    w_exact = [t for t in closed if t.get("category") == "weather" and not any(k in t.get("market","").lower() for k in DIRECTIONAL)]
    w_dir   = [t for t in closed if t.get("category") == "weather" and any(k in t.get("market","").lower() for k in DIRECTIONAL)]
    if w_exact or w_dir:
        lines.append("\n### Weather Breakdown")
        for wlist, wlabel in [(w_exact, "Exact temp"), (w_dir, "Directional")]:
            if wlist:
                ww = sum(1 for t in wlist if t.get("won"))
                wp = sum(t.get("realized_pnl",0) for t in wlist)
                lines.append(f"  {wlabel:<15} {len(wlist)} trades | {ww}W/{len(wlist)-ww}L | {ww/len(wlist)*100:.0f}% WR | ${wp:+.2f}")

    return "\n".join(lines)


# ---------------------------------------------------------
#  OPUS AUDIT CALL
# ---------------------------------------------------------

DEFAULT_CONFIG = {
    "blocked_categories": ["politics", "economics", "crypto", "conflict", "geopolitics"],
    "weather_exact_only": True,
    "yes_price_min": 86,
    "yes_price_max": 95,
    "max_closes_in_days": 0.25,
    "min_volume": 500,
    "watchlist_cities": ["jakarta", "karachi", "guangzhou"],
    "soccer_ou_enabled": True,
    "soccer_yes_min": 81,
    "soccer_yes_max": 95,
    "esports_enabled": True,
    "esports_games": ["counter-strike", "dota 2", "league of legends"],
    "esports_yes_min": 70,
    "esports_yes_max": 95,
    "pattern_e_enabled": True,
    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "audit_notes": "Default config -- no audit data yet"
}


def run_opus_audit(stats_text, client):
    """Call Opus to analyze NearCertain performance and output updated AlphaPrime config."""
    log("   Calling Opus for NearCertain -> AlphaPrime audit...")

    current_config = DEFAULT_CONFIG
    if os.path.exists(AP_CONFIG):
        with open(AP_CONFIG) as f:
            current_config = json.load(f)

    prompt = f"""You are the self-audit brain for an autonomous Polymarket trading system.

NearCertain is a broad NO-selling bot across many categories and price ranges.
AlphaPrime is the focused variant -- it only trades the patterns that NearCertain data has confirmed are profitable.

Your job: analyze NearCertain's recent performance data, identify which patterns are genuinely profitable, and output an updated AlphaPrime configuration JSON.

## NearCertain Performance Data (Last 30 Days)
{stats_text}

## Current AlphaPrime Config
{json.dumps(current_config, indent=2)}

## AlphaPrime Pattern Rules
AlphaPrime trades these patterns:
- Pattern A: Weather EXACT-TEMP NO (not directional "or above/below") -- <6h to close
- Pattern B: Soccer O/U NO -- <6h to close  
- Pattern C: Esports (CS/Dota/LoL) binary NO -- <6h to close
- Pattern D: Watchlist city boost on Pattern A (cities with historically high WR)
- Pattern E: Bracket markets (micro-stake data collection)

## Your Task

Analyze the data carefully. Then output ONLY a valid JSON config block with NO other text -- not even a preamble. The JSON must be parseable by Python's json.loads().

Rules for your analysis:
1. If a category has < 10 trades, don't draw strong conclusions -- keep it enabled
2. If a category has WR < 40% on 15+ trades, add it to blocked_categories
3. If a YES price band has WR < 35% on 10+ trades, tighten the min/max
4. Only remove esports games if they have 6+ trades with 0 wins
5. Watchlist cities: add cities with 3+ wins in 30d; remove cities with 0 wins on 4+ attempts
6. Weather: if directional weather is losing but exact-temp is profitable, keep weather_exact_only=true
7. If a time window (e.g. 6h-24h) is consistently losing, tighten max_closes_in_days

Output this exact JSON structure (update values based on your analysis):
{{
  "blocked_categories": ["list", "of", "blocked", "categories"],
  "weather_exact_only": true,
  "yes_price_min": 86,
  "yes_price_max": 95,
  "max_closes_in_days": 0.25,
  "min_volume": 500,
  "watchlist_cities": ["list", "of", "cities"],
  "soccer_ou_enabled": true,
  "soccer_yes_min": 81,
  "soccer_yes_max": 95,
  "esports_enabled": true,
  "esports_games": ["counter-strike", "dota 2", "league of legends"],
  "esports_yes_min": 70,
  "esports_yes_max": 95,
  "pattern_e_enabled": true,
  "last_updated": "{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
  "audit_notes": "Brief explanation of key changes made and why"
}}"""

    resp = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()


def parse_and_save_config(raw_text):
    """Parse Opus JSON output and save to alpha_prime_config.json."""
    # Extract JSON from response
    match = re.search(r'\{[\s\S]*\}', raw_text)
    if not match:
        log("WARNING:  Could not find JSON in Opus response -- keeping existing config")
        return False

    try:
        config = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log(f"WARNING:  JSON parse error: {e} -- keeping existing config")
        return False

    # Validate required keys
    required = ["blocked_categories", "yes_price_min", "yes_price_max", "watchlist_cities"]
    for key in required:
        if key not in config:
            log(f"WARNING:  Missing required key '{key}' -- keeping existing config")
            return False

    # Sanity checks
    if config["yes_price_min"] < 65 or config["yes_price_max"] > 99:
        log("WARNING:  YES price range out of bounds -- keeping existing config")
        return False

    with open(AP_CONFIG, "w") as f:
        json.dump(config, f, indent=2)

    log(f"OK AlphaPrime config updated -> {AP_CONFIG}")
    log(f"   Blocked: {config.get('blocked_categories', [])}")
    log(f"   YES range: {config.get('yes_price_min')}-{config.get('yes_price_max')}c")
    log(f"   Watchlist: {config.get('watchlist_cities', [])}")
    log(f"   Notes: {config.get('audit_notes', '')}")
    return True


# ---------------------------------------------------------
#  MAIN
# ---------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    print(f"\n=== SELF AUDIT - NearCertain -> AlphaPrime Auto-Updater ===")
    print(f"    {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    if not ANTHROPIC_API_KEY:
        print("FAIL  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    if not should_audit():
        log("?  Audit not due yet -- skipping")
        return

    log("-- Step 1: Load NearCertain logs ------------------------")
    nc_trades   = load_log(NC_LOG)
    ncb_trades  = load_log(NC_BETA_LOG)
    total = len(nc_trades) + len(ncb_trades)
    log(f"  NearCertain: {len(nc_trades)} trades | NearCertain Beta: {len(ncb_trades)} trades | Total: {total}")

    if total < 20:
        log(f"?  Only {total} trades -- need at least 20 for meaningful audit")
        return

    log("-- Step 2: Analyse performance --------------------------")
    nc_stats  = analyse_trades(nc_trades, "NearCertain (Main)")
    ncb_stats = analyse_trades(ncb_trades, "NearCertain Beta")
    stats_text = nc_stats + "\n\n" + ncb_stats
    print("\n" + stats_text + "\n")

    log("-- Step 3: Opus audit analysis --------------------------")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    raw_output = run_opus_audit(stats_text, client)
    log(f"  Opus response ({len(raw_output)} chars):")
    print(raw_output[:600] + "..." if len(raw_output) > 600 else raw_output)

    log("-- Step 4: Parse and save config ------------------------")
    success = parse_and_save_config(raw_output)

    if success:
        save_audit_timestamp()
        log("\nOK Self-audit complete. AlphaPrime will use updated config on next scan.")
    else:
        log("\nWARNING:  Audit completed but config not updated -- check logs above")


if __name__ == "__main__":
    main()
