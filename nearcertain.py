#!/usr/bin/env python3
"""
NearCertain Backtest — SII-WANGZJ / jon-becker Polymarket dataset
==================================================================
Strategy : Buy NO when YES is priced 75-95c before resolution.
Thesis   : Prediction markets systematically overprice near-certain events.

Usage (run in the same folder as markets.parquet):
    python3 backtest_nearcertain.py

Data source : SII-WANGZJ/Polymarket_data markets.parquet (85 MB)
              Download: hf download SII-WANGZJ/Polymarket_data markets.parquet --repo-type dataset --local-dir .

Output : backtest_nearcertain_<timestamp>.html
"""

import ast, json, math, os, statistics, sys, time
import urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────

MARKETS_FILE  = "markets.parquet"   # path to markets.parquet
FLAT_STAKE    = 10.0                # $10 flat stake for P&L calcs
STARTING_BR   = 1000.0
YES_MIN       = 75                  # entry window: YES must be at least this
YES_MAX       = 95                  # entry window: YES must be at most this
MIN_VOLUME    = 5000                # skip thin markets
MARKET_SAMPLE = 600                 # how many resolved markets to analyse
ENTRY_WINDOWS = [1, 3, 7]          # days before close to simulate entry
CLOB_URL      = "https://clob.polymarket.com/prices-history"

# Categories blocked in the live bot
BLOCKED_CATS  = {"sports", "crypto"}

# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def get_category(q):
    q = (q or "").lower()
    if any(k in q for k in ["bitcoin","btc","ethereum","eth","crypto","solana","bnb","xrp","doge","pepe"]):
        return "crypto"
    if any(k in q for k in ["temperature","weather","rain","snow","celsius","°c","°f","forecast","hurricane","typhoon"]):
        return "weather"
    if any(k in q for k in ["election","vote","president","senate","congress","trump","democrat","republican","parliament","prime minister","governor"]):
        return "politics"
    if any(k in q for k in ["gdp","inflation","cpi","fed","fomc","interest rate","earnings","revenue","stock","nasdaq","s&p","tesla","apple","nvidia","amazon","ipo"]):
        return "economics"
    if any(k in q for k in ["goal","win","score","match","game","nba","nfl","nhl","nba","fifa","premier league","championship","tournament","sport","cup","series","playoff","league","bowl"]):
        return "sports"
    if any(k in q for k in ["outbreak","cases","disease","virus","measles","covid","flu","who","pandemic","mpox"]):
        return "health"
    if any(k in q for k in ["war","military","ceasefire","troops","invasion","conflict","strike","missile","attack","nato"]):
        return "geopolitics"
    return "other"

def parse_outcome_prices(op):
    """Parse outcome_prices string like \"['0.8', '0.2']\" → (0.8, 0.2)"""
    try:
        prices = ast.literal_eval(op)
        return float(prices[0]), float(prices[1])
    except Exception:
        return None, None

def is_resolved(yes_p, no_p):
    """Return 'YES', 'NO', or None if unresolved."""
    if yes_p is not None and yes_p >= 0.9:
        return "YES"
    if no_p is not None and no_p >= 0.9:
        return "NO"
    return None

def clob_price_at(token_id, target_dt, tolerance_days=2):
    """
    Fetch YES token price from CLOB at approximately target_dt.
    Returns float (0-1) or None.
    """
    target_ts = int(target_dt.timestamp())
    url = f"{CLOB_URL}?market={token_id}&interval=max&fidelity=720"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        history = data.get("history", [])
        if not history:
            return None
        tol = tolerance_days * 86400
        best_p, best_d = None, float("inf")
        for pt in history:
            t = pt.get("t", 0)
            p = pt.get("p")
            if p is None:
                continue
            d = abs(t - target_ts)
            if d < best_d and d < tol:
                best_d = d
                best_p = float(p)
        return best_p
    except Exception:
        return None

# ─────────────────────────────────────────────────────────
# LOAD MARKETS
# ─────────────────────────────────────────────────────────

def load_markets():
    try:
        import duckdb
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "duckdb", "pyarrow", "pytz", "-q"])
        import duckdb

    if not Path(MARKETS_FILE).exists():
        log(f"ERROR: {MARKETS_FILE} not found in current directory: {Path.cwd()}")
        log("Download it with:")
        log("  hf download SII-WANGZJ/Polymarket_data markets.parquet --repo-type dataset --local-dir .")
        sys.exit(1)

    log(f"Loading {MARKETS_FILE}...")
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT id, question, outcome_prices, volume, token1, end_date
        FROM '{MARKETS_FILE}'
        WHERE closed = 1
          AND volume > {MIN_VOLUME}
        ORDER BY volume DESC
        LIMIT {MARKET_SAMPLE * 3}
    """).df()
    log(f"  Loaded {len(df)} candidate markets (vol>{MIN_VOLUME}, closed=1)")
    return df

# ─────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────────────────

def run_backtest(df):
    log("Running backtest...")
    log(f"  Entry windows: T-{ENTRY_WINDOWS}d  |  YES range: {YES_MIN}-{YES_MAX}c  |  Flat stake: ${FLAT_STAKE}")

    trades         = []
    markets_tried  = 0
    skip_cat       = 0
    skip_resolved  = 0
    no_history     = 0
    wrong_range    = 0
    clob_calls     = 0
    markets_used   = set()

    for _, row in df.iterrows():
        if markets_tried >= MARKET_SAMPLE:
            break

        question    = str(row.get("question") or "")
        op          = str(row.get("outcome_prices") or "")
        volume      = float(row.get("volume") or 0)
        token1      = str(row.get("token1") or "")
        end_date    = row.get("end_date")
        market_id   = str(row.get("id") or "")

        # Category filter
        cat = get_category(question)
        if cat in BLOCKED_CATS:
            skip_cat += 1
            continue

        # Parse resolution
        yes_p, no_p = parse_outcome_prices(op)
        resolution  = is_resolved(yes_p, no_p)
        if resolution is None:
            skip_resolved += 1
            continue

        # Parse end_date
        if end_date is None:
            skip_resolved += 1
            continue
        try:
            if hasattr(end_date, "timestamp"):
                end_ts = end_date.timestamp()
                end_dt = end_date
                if end_dt.tzinfo is None:
                    from datetime import timezone as tz
                    end_dt = end_dt.replace(tzinfo=tz.utc)
            else:
                end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                end_ts = end_dt.timestamp()
        except Exception:
            skip_resolved += 1
            continue

        markets_tried += 1
        if markets_tried % 50 == 0:
            log(f"  [{markets_tried}/{MARKET_SAMPLE}] trades={len(trades)} clob_calls={clob_calls}")

        # Try each entry window
        for days_before in ENTRY_WINDOWS:
            target_dt = end_dt - timedelta(days=days_before)

            # Fetch YES price from CLOB at entry time
            clob_calls += 1
            yes_price = clob_price_at(token1, target_dt)
            time.sleep(0.05)   # gentle rate limiting

            if yes_price is None:
                if days_before == ENTRY_WINDOWS[0]:
                    no_history += 1
                continue

            yes_pct = round(yes_price * 100, 1)
            no_pct  = round((1 - yes_price) * 100, 1)

            if not (YES_MIN <= yes_pct <= YES_MAX):
                if days_before == ENTRY_WINDOWS[0]:
                    wrong_range += 1
                continue

            if no_pct <= 0:
                continue

            # NearCertain trade: we bet NO
            won = (resolution == "NO")
            pnl = round(FLAT_STAKE * 100 / no_pct - FLAT_STAKE, 2) if won else -FLAT_STAKE

            # Date label for equity curve
            try:
                date_str = end_dt.strftime("%Y-%m")
            except Exception:
                date_str = "unknown"

            trades.append({
                "market_id":   market_id,
                "market":      question[:80],
                "category":    cat,
                "entry_days":  days_before,
                "yes_entry":   yes_pct,
                "no_entry":    no_pct,
                "resolution":  resolution,
                "won":         won,
                "pnl":         pnl,
                "volume":      volume,
                "date":        date_str,
            })

    log(f"\nBacktest complete:")
    log(f"  Markets tried:       {markets_tried}")
    log(f"  Trades generated:    {len(trades)}")
    log(f"  CLOB calls made:     {clob_calls}")
    log(f"  Skip category:       {skip_cat}")
    log(f"  Skip unresolved:     {skip_resolved}")
    log(f"  No CLOB history:     {no_history}")
    log(f"  Wrong price range:   {wrong_range}")
    return trades

# ─────────────────────────────────────────────────────────
# STATISTICS
# ─────────────────────────────────────────────────────────

def calc_stats(trades):
    if not trades:
        return {}
    won   = [t for t in trades if t["won"]]
    lost  = [t for t in trades if not t["won"]]
    n     = len(trades)
    wr    = len(won) / n * 100
    total = sum(t["pnl"] for t in trades)
    gw    = sum(t["pnl"] for t in won)   if won  else 0
    gl    = abs(sum(t["pnl"] for t in lost)) if lost else 0.001
    pf    = round(gw / gl, 2)
    avg   = statistics.mean(t["no_entry"] for t in trades)
    edge  = round(wr - avg, 1)

    # Sharpe
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]
    dv = list(daily.values())
    sharpe = 0
    if len(dv) > 1:
        sd = statistics.stdev(dv)
        sharpe = round(statistics.mean(dv) / sd * math.sqrt(252), 2) if sd else 0

    # Max drawdown
    equity = STARTING_BR
    peak   = STARTING_BR
    max_dd = 0
    for t in sorted(trades, key=lambda x: x["date"]):
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Kelly ROI
    p = wr / 100
    b = (100 / avg) - 1 if avg > 0 else 0
    kelly_roi = 0
    if b > 0 and p > 0:
        hk = max(0, min(((b * p - (1 - p)) / b) * 0.5, 0.15))
        br = STARTING_BR
        for t in sorted(trades, key=lambda x: x["date"]):
            s  = min(br * hk, br * 0.15)
            br = br + s * b if t["won"] else br - s
        kelly_roi = round((br - STARTING_BR) / STARTING_BR * 100, 1)

    return {
        "n": n, "won": len(won), "lost": len(lost),
        "wr": round(wr, 1), "implied": round(avg, 1),
        "edge": edge, "pf": pf, "flat_pnl": round(total, 2),
        "max_dd": round(max_dd, 1), "sharpe": sharpe, "kelly_roi": kelly_roi,
    }

def stats_by_window(trades):
    return {
        f"T-{d}d": calc_stats([t for t in trades if t["entry_days"] == d])
        for d in ENTRY_WINDOWS
        if any(t["entry_days"] == d for t in trades)
    }

def stats_by_category(trades, ed=1):
    sub = [t for t in trades if t["entry_days"] == ed]
    cats = set(t["category"] for t in sub)
    return {
        c: calc_stats([t for t in sub if t["category"] == c])
        for c in cats
        if sum(1 for t in sub if t["category"] == c) >= 3
    }

def stats_by_price_band(trades, ed=1):
    sub   = [t for t in trades if t["entry_days"] == ed]
    bands = [("75-80¢", 75, 80), ("80-85¢", 80, 85), ("85-90¢", 85, 90), ("90-95¢", 90, 95)]
    return {
        label: calc_stats([t for t in sub if lo <= t["yes_entry"] < hi])
        for label, lo, hi in bands
        if sum(1 for t in sub if lo <= t["yes_entry"] < hi) >= 3
    }

# ── NEW: Cross-tab breakdowns ─────────────────────────────

def stats_category_by_window(trades):
    """For each category, show T-1d / T-3d / T-7d WR side by side."""
    cats = sorted(set(t["category"] for t in trades))
    result = {}
    for cat in cats:
        row = {}
        for d in ENTRY_WINDOWS:
            sub = [t for t in trades if t["category"] == cat and t["entry_days"] == d]
            if len(sub) >= 3:
                row[f"T-{d}d"] = calc_stats(sub)
        if row:
            result[cat] = row
    return result

def stats_band_by_category(trades, ed=1):
    """For each price band, show WR by category."""
    bands  = [("75-80¢", 75, 80), ("80-85¢", 80, 85), ("85-90¢", 85, 90), ("90-95¢", 90, 95)]
    cats   = sorted(set(t["category"] for t in trades if t["entry_days"] == ed))
    result = {}
    for label, lo, hi in bands:
        row = {}
        for cat in cats:
            sub = [t for t in trades if t["entry_days"] == ed
                   and lo <= t["yes_entry"] < hi and t["category"] == cat]
            if len(sub) >= 3:
                row[cat] = calc_stats(sub)
        if row:
            result[label] = row
    return result

def stats_category_by_band(trades, ed=1):
    """For each category, show WR by price band."""
    bands = [("75-80¢", 75, 80), ("80-85¢", 80, 85), ("85-90¢", 85, 90), ("90-95¢", 90, 95)]
    cats  = sorted(set(t["category"] for t in trades if t["entry_days"] == ed))
    result = {}
    for cat in cats:
        row = {}
        for label, lo, hi in bands:
            sub = [t for t in trades if t["entry_days"] == ed
                   and t["category"] == cat and lo <= t["yes_entry"] < hi]
            if len(sub) >= 3:
                row[label] = calc_stats(sub)
        if row:
            result[cat] = row
    return result

def equity_curves_by_category(trades, ed=1):
    """Equity curve per category at T-1d."""
    cats = sorted(set(t["category"] for t in trades if t["entry_days"] == ed))
    result = {}
    for cat in cats:
        sub = sorted([t for t in trades if t["entry_days"] == ed and t["category"] == cat],
                     key=lambda x: x["date"])
        if len(sub) >= 5:
            br  = STARTING_BR
            pts = [br]
            for t in sub:
                br += t["pnl"]
                pts.append(round(br, 2))
            result[cat] = pts
    return result


    sub = sorted([t for t in trades if t["entry_days"] == ed], key=lambda x: x["date"])
    br  = STARTING_BR
    pts = [br]
    for t in sub:
        br += t["pnl"]
        pts.append(round(br, 2))
    return pts

# ─────────────────────────────────────────────────────────
# HTML REPORT
# ─────────────────────────────────────────────────────────

def build_html(trades, overall, by_window, by_cat, by_band, eq_pts,
               cat_by_window, band_by_cat, cat_by_band, eq_by_cat, ts):
    n_markets = len(set(t["market_id"] for t in trades))
    best      = max(by_window.items(), key=lambda x: x[1].get("edge", -999)) if by_window else ("—", {})

    def badge(wr):
        if wr >= 70: return f'<span style="color:#4ade80;font-weight:700">{wr:.1f}%</span>'
        if wr >= 60: return f'<span style="color:#a3e635">{wr:.1f}%</span>'
        if wr >= 50: return f'<span style="color:#facc15">{wr:.1f}%</span>'
        return f'<span style="color:#f87171">{wr:.1f}%</span>'

    def rc(wr):
        if wr >= 70: return "#162216"
        if wr >= 60: return "#1e2e16"
        if wr >= 50: return "#2e2e16"
        return "#2e1616"

    # Equity SVG
    if len(eq_pts) > 1:
        eq_min = min(eq_pts); eq_max = max(eq_pts); w, h = 800, 220
        rng    = max(eq_max - eq_min, 1)
        pts    = " ".join(
            f"{int(i / max(len(eq_pts)-1,1) * w)},{int(h - (v - eq_min) / rng * (h - 10) + 5)}"
            for i, v in enumerate(eq_pts)
        )
        eq_color = "#4ade80" if eq_pts[-1] >= STARTING_BR else "#f87171"
        svg = (f'<svg viewBox="0 0 {w} {h}" style="width:100%;background:#0d1117;border-radius:8px;margin:10px 0">'
               f'<line x1="0" y1="{int(h-(STARTING_BR-eq_min)/rng*(h-10)+5)}" x2="{w}" y2="{int(h-(STARTING_BR-eq_min)/rng*(h-10)+5)}" stroke="#374151" stroke-width="1" stroke-dasharray="4"/>'
               f'<polyline points="{pts}" fill="none" stroke="{eq_color}" stroke-width="2.5"/>'
               f'<text x="6" y="16" fill="#6b7280" font-size="11" font-family="monospace">${eq_max:.0f}</text>'
               f'<text x="6" y="{h-4}" fill="#6b7280" font-size="11" font-family="monospace">${eq_min:.0f}</text>'
               f'</svg>')
    else:
        svg = '<p style="color:#6b7280">No equity curve — insufficient T-1d trades</p>'

    # Window table rows
    win_rows = "".join(
        f'<tr style="background:{rc(s["wr"])}">'
        f'<td style="font-weight:600">{label}</td>'
        f'<td style="text-align:right">{s["n"]}</td>'
        f'<td style="text-align:center">{badge(s["wr"])}</td>'
        f'<td style="text-align:right;color:#9ca3af">{s["implied"]:.1f}%</td>'
        f'<td style="text-align:right;color:#4ade80;font-weight:700">+{s["edge"]}%</td>'
        f'<td style="text-align:right">{s["pf"]}</td>'
        f'<td style="text-align:right;color:#4ade80">${s["flat_pnl"]:+.0f}</td>'
        f'<td style="text-align:right;color:#60a5fa">{s["kelly_roi"]:+.1f}%</td>'
        f'<td style="text-align:right;color:#fb923c">{s["max_dd"]:.1f}%</td>'
        f'<td style="text-align:right">{s["sharpe"]}</td>'
        f'</tr>'
        for label, s in sorted(by_window.items())
    )

    # Category rows (T-1d)
    cat_rows = "".join(
        f'<tr><td>{c}</td><td style="text-align:center">{badge(s["wr"])}</td>'
        f'<td style="text-align:right;color:#9ca3af">n={s["n"]}</td>'
        f'<td style="text-align:right;color:#4ade80">+{s["edge"]}%</td>'
        f'<td style="text-align:right;color:#a78bfa">PF {s["pf"]}</td>'
        f'<td style="text-align:right;color:#60a5fa">${s["flat_pnl"]:+.0f}</td></tr>'
        for c, s in sorted(by_cat.items(), key=lambda x: -x[1].get("n", 0))
    )

    # Price band rows (T-1d)
    band_rows = "".join(
        f'<tr><td>{b}</td><td style="text-align:center">{badge(s["wr"])}</td>'
        f'<td style="text-align:right;color:#9ca3af">n={s["n"]}</td>'
        f'<td style="text-align:right;color:#4ade80">+{s["edge"]}%</td>'
        f'<td style="text-align:right;color:#60a5fa">${s["flat_pnl"]:+.0f}</td></tr>'
        for b, s in sorted(by_band.items())
    )

    # Top/bottom trades
    t1 = sorted([t for t in trades if t["entry_days"] == 1], key=lambda x: -x["pnl"])
    def trow(t):
        c = "#4ade80" if t["won"] else "#f87171"
        return (f'<tr><td style="font-size:11px;color:#cbd5e1">{t["market"][:70]}</td>'
                f'<td><span style="background:#1e293b;padding:2px 6px;border-radius:4px;font-size:10px">{t["category"]}</span></td>'
                f'<td style="text-align:right;color:#94a3b8">{t["yes_entry"]}¢</td>'
                f'<td style="text-align:right;color:#94a3b8">{t["no_entry"]}¢</td>'
                f'<td style="text-align:center">{"✅" if t["won"] else "❌"}</td>'
                f'<td style="text-align:right;color:{c};font-weight:700">${t["pnl"]:+.2f}</td></tr>')

    css = """
* { box-sizing: border-box; margin: 0; padding: 0 }
body { background: #0d1117; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif; padding: 28px; }
h1 { font-size: 24px; color: #f1f5f9; margin-bottom: 4px }
h2 { font-size: 13px; color: #94a3b8; font-weight: 400; margin-bottom: 20px }
.meta { color: #6b7280; font-size: 12px; margin-bottom: 28px }
.grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; margin-bottom: 32px }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 18px }
.cl { font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: .8px; margin-bottom: 8px }
.cv { font-size: 26px; font-weight: 700; color: #f1f5f9; line-height: 1 }
.cs { font-size: 11px; color: #4b5563; margin-top: 5px }
.sec { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 22px; margin-bottom: 22px }
h3 { font-size: 14px; color: #cbd5e1; margin: 0 0 14px; border-bottom: 1px solid #1e293b; padding-bottom: 8px }
table { width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 8px }
th { background: #0d1117; color: #6b7280; padding: 9px 12px; text-align: left; border-bottom: 1px solid #21262d; font-weight: 500; font-size: 11px }
td { padding: 8px 12px; border-bottom: 1px solid #1a2030 }
tr:hover td { background: #1a2030 }
.note { background: #131d2e; border-left: 3px solid #3b82f6; padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 12px; color: #94a3b8; margin-top: 14px; line-height: 1.6 }
.divider { text-align: center; color: #374151; font-size: 11px; padding: 6px; }
"""

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><title>NearCertain Backtest</title>
<style>{css}</style></head><body>

<h1>🔵 NearCertain Backtest Report</h1>
<h2>Strategy: Buy NO when YES is priced 75-95¢ — markets systematically overprice near-certain outcomes</h2>
<div class="meta">
  Generated {ts} &nbsp;·&nbsp;
  {n_markets} resolved markets analysed &nbsp;·&nbsp;
  {len(trades)} simulated trades &nbsp;·&nbsp;
  {len(ENTRY_WINDOWS)} entry windows (T-{min(ENTRY_WINDOWS)}d to T-{max(ENTRY_WINDOWS)}d) &nbsp;·&nbsp;
  Source: SII-WANGZJ/Polymarket_data &amp; Polymarket CLOB API
</div>

<div class="grid">
  <div class="card">
    <div class="cl">Markets Analysed</div>
    <div class="cv">{n_markets}</div>
    <div class="cs">Vol &gt; ${MIN_VOLUME:,}, resolved binary</div>
  </div>
  <div class="card">
    <div class="cl">Best Entry Timing</div>
    <div class="cv" style="color:#3b82f6">{best[0]}</div>
    <div class="cs">by actual edge over implied</div>
  </div>
  <div class="card">
    <div class="cl">Best Edge (WR − implied)</div>
    <div class="cv" style="color:#4ade80">+{best[1].get("edge",0)}%</div>
    <div class="cs">genuine alpha over market pricing</div>
  </div>
  <div class="card">
    <div class="cl">Best Profit Factor</div>
    <div class="cv" style="color:#a78bfa">{best[1].get("pf",0)}</div>
    <div class="cs">gross wins / gross losses</div>
  </div>
  <div class="card">
    <div class="cl">Overall Win Rate</div>
    <div class="cv">{overall.get("wr",0):.1f}%</div>
    <div class="cs">{overall.get("won",0)}W / {overall.get("lost",0)}L across all windows</div>
  </div>
  <div class="card">
    <div class="cl">Overall Edge</div>
    <div class="cv" style="color:#4ade80">+{overall.get("edge",0)}%</div>
    <div class="cs">vs {overall.get("implied",0):.1f}% market-implied</div>
  </div>
  <div class="card">
    <div class="cl">Flat P&amp;L (best window)</div>
    <div class="cv" style="color:#4ade80">${best[1].get("flat_pnl",0):+.0f}</div>
    <div class="cs">$10/trade flat sizing</div>
  </div>
  <div class="card">
    <div class="cl">Max Drawdown (best)</div>
    <div class="cv" style="color:#fb923c">{best[1].get("max_dd",0):.1f}%</div>
    <div class="cs">flat $10 sizing</div>
  </div>
</div>

<div class="sec">
  <h3>Results by Entry Timing</h3>
  <table>
    <tr><th>Entry</th><th style="text-align:right">Trades</th><th style="text-align:center">Win Rate</th>
        <th style="text-align:right">Implied</th><th style="text-align:right">Edge ★</th>
        <th style="text-align:right">PF</th><th style="text-align:right">Flat P&L</th>
        <th style="text-align:right">Kelly ROI</th><th style="text-align:right">Max DD</th>
        <th style="text-align:right">Sharpe</th></tr>
    {win_rows}
  </table>
  <div class="note">
    ★ <b>Edge</b> = actual win rate − average NO entry price (market-implied win probability for NO).
    Positive edge means the strategy systematically beats market pricing.
    <b>Profit Factor &gt; 2.0</b> = strong. <b>Sharpe &gt; 1.0</b> = strong risk-adjusted returns.
    <br>Timing note: T-7d = entered 7 days before close, T-1d = 1 day before close.
  </div>
</div>

<div class="sec">
  <h3>Equity Curve (flat $10/trade, T-1d entry, chronological)</h3>
  {svg}
  <div style="font-size:11px;color:#6b7280;margin-top:4px">Dashed line = $1,000 starting bankroll. Each point = one resolved trade.</div>
</div>

<div class="sec">
  <h3>Win Rate by YES Entry Price Band (T-1d entry)</h3>
  <p style="font-size:12px;color:#6b7280;margin-bottom:12px">
    Higher YES price = market more aggressively overbidding = stronger NearCertain signal.
    90-95¢ YES means NO is available at 5-10¢ — extraordinary payoff if the strategy holds.
  </p>
  <table>
    <tr><th>YES Entry Band</th><th style="text-align:center">Win Rate</th><th style="text-align:right">Trades</th>
        <th style="text-align:right">Edge</th><th style="text-align:right">Flat P&L</th></tr>
    {band_rows if band_rows else '<tr><td colspan="5" style="color:#6b7280">Insufficient data per band — widen YES range or increase sample</td></tr>'}
  </table>
</div>

<div class="sec">
  <h3>Win Rate by Category (T-1d entry)</h3>
  <table>
    <tr><th>Category</th><th style="text-align:center">Win Rate</th><th style="text-align:right">Trades</th>
        <th style="text-align:right">Edge</th><th style="text-align:right">PF</th><th style="text-align:right">Flat P&L</th></tr>
    {cat_rows if cat_rows else '<tr><td colspan="6" style="color:#6b7280">Insufficient trades per category</td></tr>'}
  </table>
  <div class="note">
    Crypto and Sports are blocked in the live NearCertain bot based on prior backtest analysis showing near-50/50 WR in crypto and in-game certainty bias in sports.
  </div>
</div>

<div class="sec">
  <h3>Top 10 Wins &amp; Top 10 Losses (T-1d entry, flat $10)</h3>
  <table>
    <tr><th>Market</th><th>Category</th><th style="text-align:right">YES Entry</th>
        <th style="text-align:right">NO Entry</th><th style="text-align:center">Result</th>
        <th style="text-align:right">P&amp;L</th></tr>
    {"".join(trow(t) for t in t1[:10])}
    <tr><td colspan="6" class="divider">— Top Losses —</td></tr>
    {"".join(trow(t) for t in t1[-10:])}
  </table>
</div>

<div class="sec">
  <h3>Category × Entry Timing (WR across all windows)</h3>
  <p style="font-size:12px;color:#6b7280;margin-bottom:12px">How each category performs at T-1d, T-3d, T-7d entry — shows whether edge is timing-dependent.</p>
  <table>
    <tr><th>Category</th><th style="text-align:right">T-1d WR</th><th style="text-align:right">T-1d n</th>
        <th style="text-align:right">T-3d WR</th><th style="text-align:right">T-3d n</th>
        <th style="text-align:right">T-7d WR</th><th style="text-align:right">T-7d n</th></tr>
    {"".join(
        f'<tr><td style="font-weight:500">{cat}</td>'
        + "".join(
            f'<td style="text-align:right">{badge(row[w]["wr"])} </td><td style="text-align:right;color:#6b7280">{row[w]["n"]}</td>'
            if w in row else '<td style="text-align:right;color:#374151">—</td><td></td>'
            for w in ["T-1d","T-3d","T-7d"]
        )
        + "</tr>"
        for cat, row in sorted(cat_by_window.items())
    )}
  </table>
</div>

<div class="sec">
  <h3>Price Band × Category (T-1d entry)</h3>
  <p style="font-size:12px;color:#6b7280;margin-bottom:12px">Which categories drive edge within each price band — shows where to concentrate.</p>
  <table>
    <tr><th>YES Band</th>{"".join(f'<th style="text-align:right">{c}</th>' for c in sorted(set(cat for row in band_by_cat.values() for cat in row)))}</tr>
    {"".join(
        f'<tr><td style="font-weight:500">{band}</td>'
        + "".join(
            f'<td style="text-align:right">{badge(row[c]["wr"])} <span style=\"color:#6b7280;font-size:10px\">n={row[c]["n"]}</span></td>'
            if c in row else '<td style="text-align:right;color:#374151">—</td>'
            for c in sorted(set(cat for r in band_by_cat.values() for cat in r))
        )
        + "</tr>"
        for band, row in sorted(band_by_cat.items())
    )}
  </table>
</div>

<div class="sec">
  <h3>Category × Price Band (T-1d entry)</h3>
  <p style="font-size:12px;color:#6b7280;margin-bottom:12px">Within each category, which price band has the strongest edge.</p>
  <table>
    <tr><th>Category</th><th style="text-align:right">75-80¢</th><th style="text-align:right">80-85¢</th>
        <th style="text-align:right">85-90¢</th><th style="text-align:right">90-95¢</th></tr>
    {"".join(
        f'<tr><td style="font-weight:500">{cat}</td>'
        + "".join(
            f'<td style="text-align:right">{badge(row[b]["wr"])} <span style=\"color:#6b7280;font-size:10px\">n={row[b]["n"]}</span></td>'
            if b in row else '<td style="text-align:right;color:#374151">—</td>'
            for b in ["75-80¢","80-85¢","85-90¢","90-95¢"]
        )
        + "</tr>"
        for cat, row in sorted(cat_by_band.items())
    )}
  </table>
</div>

<div class="sec">
  <h3>Equity Curves by Category (T-1d entry)</h3>
  <p style="font-size:12px;color:#6b7280;margin-bottom:12px">Individual P&L progression per category — shows which categories compound vs bleed.</p>
  {"".join(
      (lambda pts, cat: (
          "<div style='margin-bottom:16px'>"
          f"<div style='font-size:12px;color:#94a3b8;margin-bottom:4px'>{cat} "
          f"<span style='color:#6b7280'>({len(pts)-1} trades · final: ${pts[-1]:.0f})</span></div>"
          + (lambda eq_min, eq_max, rng, w, h: (
              f'<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:700px;background:#0d1117;border-radius:6px">'
              f'<line x1="0" y1="{int(h-(STARTING_BR-eq_min)/rng*(h-10)+5)}" x2="{w}" y2="{int(h-(STARTING_BR-eq_min)/rng*(h-10)+5)}" stroke="#374151" stroke-width="1" stroke-dasharray="3"/>'
              f'<polyline points="{" ".join(f"{int(i/max(len(pts)-1,1)*w)},{int(h-(v-eq_min)/rng*(h-10)+5)}" for i,v in enumerate(pts))}" '
              f'fill="none" stroke="{"#4ade80" if pts[-1]>=STARTING_BR else "#f87171"}" stroke-width="2"/>'
              f'</svg>'
          ))(min(pts), max(pts), max(max(pts)-min(pts),1), 600, 120)
          + "</div>"
      ))(pts, cat)
      for cat, pts in sorted(eq_by_cat.items())
  ) or "<p style='color:#6b7280'>Insufficient data per category.</p>"}
</div>

<div class="sec">
  <h3>Methodology</h3>
  <p style="font-size:12px;color:#94a3b8;line-height:1.8">
    <b>Data:</b> SII-WANGZJ/Polymarket_data — markets.parquet (538K+ markets, complete Polymarket history).
    Markets filtered to: closed=1, volume &gt; ${MIN_VOLUME:,}, binary resolution (YES or NO final price ≥ 90%).<br><br>
    <b>Entry simulation:</b> For each entry window (T-{min(ENTRY_WINDOWS)}d, T-{ENTRY_WINDOWS[1]}d, T-{max(ENTRY_WINDOWS)}d before market close),
    the YES token price is fetched from the Polymarket CLOB prices-history API
    (<code>interval=max, fidelity=720</code>).
    Only markets where YES was priced {YES_MIN}-{YES_MAX}¢ at entry are included in the NearCertain strategy results.<br><br>
    <b>Trade logic:</b> Enter NO at the prevailing NO price. Hold to resolution. Win if market resolves NO (YES price at resolution &lt; 10%).<br><br>
    <b>Sizing:</b> Flat $10 per trade for fair comparison. Kelly = half-Kelly from $1,000 starting bankroll, hard-capped at 15% per trade.<br><br>
    <b>Blocked categories:</b> Crypto (structural near-50/50 WR) and Sports (live game markets are legitimately near-certain near close).<br><br>
    <b>Limitations:</b> CLOB price history is daily granularity (12h fidelity) — intraday slippage not modelled.
    Polymarket 0.1-0.2% taker fee excluded. Markets without CLOB price history at the entry window are excluded from trade count.
  </p>
</div>

</body></html>"""

# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    log("=" * 65)
    log("NearCertain Backtest — SII-WANGZJ Polymarket Dataset")
    log(f"Strategy : Buy NO when YES priced {YES_MIN}-{YES_MAX}¢")
    log(f"Sample   : {MARKET_SAMPLE} resolved markets, vol > ${MIN_VOLUME:,}")
    log(f"Windows  : T-{ENTRY_WINDOWS}d before close")
    log("=" * 65)

    df     = load_markets()
    trades = run_backtest(df)

    if not trades:
        log("ERROR: No trades generated. Check CLOB API connectivity and market sample.")
        log("  Make sure you can reach clob.polymarket.com from this machine.")
        sys.exit(1)

    log("\n" + "=" * 65)
    log(f"RESULTS ({len(trades)} trades across {len(set(t['market_id'] for t in trades))} markets)")

    overall       = calc_stats(trades)
    by_window     = stats_by_window(trades)
    by_cat        = stats_by_category(trades)
    by_band       = stats_by_price_band(trades)
    eq_pts        = equity_curve_points(trades)
    cat_by_window = stats_category_by_window(trades)
    band_by_cat   = stats_band_by_category(trades)
    cat_by_band   = stats_category_by_band(trades)
    eq_by_cat     = equity_curves_by_category(trades)

    log(f"  Overall : {overall['wr']}% WR | edge +{overall['edge']}% | PF {overall['pf']} | ${overall['flat_pnl']:+.2f}")
    for label, s in sorted(by_window.items()):
        log(f"  {label}   : {s['wr']}% WR | edge +{s['edge']}% | PF {s['pf']} | ${s['flat_pnl']:+.0f}")

    html  = build_html(
        trades, overall, by_window, by_cat, by_band, eq_pts,
        cat_by_window, band_by_cat, cat_by_band, eq_by_cat,
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )
    fname = f"backtest_nearcertain_{ts}.html"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(html)

    log(f"\n✅ Report saved: {fname}")
    log(f"   Open with: open {fname}")

if __name__ == "__main__":
    main()
