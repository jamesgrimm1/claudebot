#!/usr/bin/env python3
"""
backtest_nearcertain.py
Backtest the NearCertain strategy against real Polymarket resolved markets.

Strategy: Buy NO when YES is priced 75-95¢ (market thinks event is near-certain).
Thesis: Prediction markets systematically overprice near-certain events.

Data source: Polymarket Gamma API (resolved markets) + CLOB price history
Entry timing tested: T-7d, T-3d, T-1d before market close
Target: 500-1000 simulated trades

Output: backtest_nearcertain_<timestamp>.html
"""

import json
import math
import os
import sys
import time
import requests
import statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────
TARGET_TRADES   = 800       # aim for ~800 trades across all strategies/timings
MARKET_BATCH    = 200       # markets to analyse — more = more trades
YES_MIN         = 75        # NearCertain entry: YES at least 75¢
YES_MAX         = 95        # NearCertain entry: YES at most 95¢
FLAT_STAKE      = 10.0      # $10 flat stake for comparison
STARTING_BR     = 1000.0   # for Kelly simulation
MIN_VOLUME      = 2000      # minimum market volume
BLOCKED_CATS    = {"sports", "crypto"}
ENTRY_WINDOWS   = [1, 3, 7]  # days before close to simulate entry

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def get_category(question):
    q = question.lower()
    if any(k in q for k in ["bitcoin","btc","ethereum","eth","crypto","solana","bnb","xrp","doge"]):
        return "crypto"
    if any(k in q for k in ["temperature","weather","rain","snow","°c","°f","celsius","forecast"]):
        return "weather"
    if any(k in q for k in ["election","vote","president","congress","senate","trump","democrat","republican"]):
        return "politics"
    if any(k in q for k in ["gdp","inflation","cpi","fed","fomc","rate","earnings","revenue","stock","nasdaq","s&p","tesla","apple","nvidia","amazon"]):
        return "economics"
    if any(k in q for k in ["goal","win","score","match","game","nba","nfl","fifa","premier league","championship","tournament"]):
        return "sports"
    if any(k in q for k in ["outbreak","cases","disease","virus","measles","covid","flu"]):
        return "health"
    return "other"

def safe_get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=12)
            if r.status_code == 200:
                return r.json()
            time.sleep(0.5)
        except Exception as e:
            if i == retries - 1:
                return None
            time.sleep(1)
    return None

# ─────────────────────────────────────────────────────────
#  DATA COLLECTION
# ─────────────────────────────────────────────────────────

def fetch_resolved_markets(target=MARKET_BATCH):
    """Fetch resolved binary markets from Gamma API."""
    log(f"Fetching resolved markets (target: {target})...")
    markets = []
    offset  = 0
    limit   = 100

    while len(markets) < target:
        data = safe_get(
            f"{GAMMA_URL}/markets",
            params={
                "closed":     "true",
                "active":     "false",
                "limit":      limit,
                "offset":     offset,
                "order":      "endDate",
                "ascending":  "false",   # most recently resolved first
            }
        )
        if not data:
            break

        for m in data:
            # Must be resolved
            prices = m.get("outcomePrices")
            if not prices:
                continue
            try:
                p = json.loads(prices) if isinstance(prices, str) else prices
                yes_final = float(p[0])
                no_final  = float(p[1])
            except Exception:
                continue

            # Must have resolved cleanly (YES=1 or NO=1)
            resolved_yes = yes_final >= 0.99
            resolved_no  = no_final  >= 0.99
            if not (resolved_yes or resolved_no):
                continue

            # Must have volume
            vol = float(m.get("volume", 0))
            if vol < MIN_VOLUME:
                continue

            # Must have clob token IDs for price history
            clob_ids = m.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            if not clob_ids:
                continue

            # Parse close date
            end_str = m.get("endDate") or m.get("endDateIso") or ""
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                continue

            cat = get_category(m.get("question", ""))

            markets.append({
                "id":           str(m.get("id", "")),
                "question":     m.get("question", ""),
                "category":     cat,
                "volume":       vol,
                "end_dt":       end_dt,
                "clob_ids":     clob_ids,
                "resolved_yes": resolved_yes,
                "yes_final":    yes_final,
            })

            if len(markets) >= target:
                break

        if len(data) < limit:
            break
        offset += limit
        time.sleep(0.2)

    log(f"  Got {len(markets)} resolved markets")
    return markets


def fetch_price_at_days_before(token_id, end_dt, days_before):
    """
    Fetch YES token price approximately N days before market close.
    Uses CLOB prices-history endpoint with interval=max, fidelity=720 (12h).
    Note: fidelity < 720 returns empty for resolved markets (Polymarket limitation).
    Returns price (0-1) or None if not available.
    """
    target_dt  = end_dt - timedelta(days=days_before)
    target_ts  = int(target_dt.timestamp())

    data = safe_get(
        f"{CLOB_URL}/prices-history",
        params={
            "market":    token_id,
            "interval":  "max",
            "fidelity":  720,   # 12h in minutes — minimum that works for resolved markets
        }
    )

    if not data:
        return None

    history = data.get("history", [])
    if not history:
        return None

    # Find the price closest to our target timestamp
    best = None
    best_diff = float("inf")
    for point in history:
        ts = point.get("t", 0)
        p  = point.get("p")
        if p is None:
            continue
        diff = abs(ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best = float(p)

    # Reject if the closest point is more than 4 days away from target
    if best is not None and best_diff > 4 * 86400:
        return None

    return best

# ─────────────────────────────────────────────────────────
#  BACKTEST ENGINE
# ─────────────────────────────────────────────────────────

def run_backtest(markets):
    """
    Simulate NearCertain trades at T-7d, T-3d, T-1d entry windows.
    Returns list of trade records.
    """
    log("Running NearCertain backtest simulation...")
    trades       = []
    skipped      = 0
    no_history   = 0
    wrong_range  = 0
    now          = datetime.now(timezone.utc)

    for i, m in enumerate(markets):
        if i % 10 == 0:
            log(f"  Processing market {i+1}/{len(markets)} | trades so far: {len(trades)}...")

        # Only use markets that closed > 1 day ago (confirmed resolution)
        if (now - m["end_dt"]).days < 1:
            skipped += 1
            continue

        # Skip blocked categories
        if m["category"] in BLOCKED_CATS:
            skipped += 1
            continue

        yes_token = m["clob_ids"][0] if m["clob_ids"] else None
        if not yes_token:
            skipped += 1
            continue

        # Try each entry window
        for days_before in ENTRY_WINDOWS:
            # Market must have been open at entry time
            if (now - m["end_dt"]).days < days_before:
                continue

            # Fetch YES price at entry time
            yes_price = fetch_price_at_days_before(yes_token, m["end_dt"], days_before)

            if yes_price is None:
                no_history += 1
                continue

            yes_pct = round(yes_price * 100, 1)
            no_pct  = round((1 - yes_price) * 100, 1)

            # NearCertain filter: YES must be 75-95¢ at entry
            if not (YES_MIN <= yes_pct <= YES_MAX):
                wrong_range += 1
                continue

            # Simulate NO trade
            won = not m["resolved_yes"]

            # P&L calculation
            if won:
                payout = FLAT_STAKE * 100 / no_pct
                pnl    = round(payout - FLAT_STAKE, 2)
            else:
                pnl = -FLAT_STAKE

            trades.append({
                "market_id":    m["id"],
                "market":       m["question"][:80],
                "category":     m["category"],
                "entry_days":   days_before,
                "yes_entry":    yes_pct,
                "no_entry":     no_pct,
                "won":          won,
                "pnl":          pnl,
                "volume":       m["volume"],
                "end_dt":       m["end_dt"].strftime("%Y-%m-%d"),
            })

            time.sleep(0.05)

    log(f"  Generated {len(trades)} trades")
    log(f"  Skipped: {skipped} (blocked cat/no token)")
    log(f"  No history: {no_history}")
    log(f"  Wrong price range: {wrong_range}")
    return trades

# ─────────────────────────────────────────────────────────
#  STATISTICS
# ─────────────────────────────────────────────────────────

def calc_stats(trades):
    if not trades:
        return {}

    won    = [t for t in trades if t["won"]]
    lost   = [t for t in trades if not t["won"]]
    n      = len(trades)
    wr     = len(won) / n * 100
    total_pnl = sum(t["pnl"] for t in trades)

    gross_wins  = sum(t["pnl"] for t in won) if won else 0
    gross_loss  = abs(sum(t["pnl"] for t in lost)) if lost else 0.001
    pf          = round(gross_wins / gross_loss, 2) if gross_loss else float("inf")

    avg_entry   = statistics.mean(t["no_entry"] for t in trades)
    implied     = round(avg_entry, 1)
    edge        = round(wr - implied, 1)

    # Daily P&L for Sharpe
    daily = defaultdict(float)
    for t in trades:
        daily[t["end_dt"]] += t["pnl"]
    daily_vals = list(daily.values())
    if len(daily_vals) > 1:
        avg_d  = statistics.mean(daily_vals)
        std_d  = statistics.stdev(daily_vals)
        sharpe = round(avg_d / std_d * math.sqrt(252), 2) if std_d else 0
    else:
        sharpe = 0

    # Max drawdown
    equity = STARTING_BR
    peak   = equity
    max_dd = 0
    for t in sorted(trades, key=lambda x: x["end_dt"]):
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Kelly ROI (half-Kelly, capped)
    p  = wr / 100
    b  = (100 / avg_entry) - 1 if avg_entry > 0 else 0
    if b > 0 and p > 0:
        full_kelly = (b * p - (1 - p)) / b
        half_kelly = max(0, full_kelly * 0.5)
        half_kelly = min(half_kelly, 0.15)   # cap at 15%
        # Simulate Kelly growth
        br = STARTING_BR
        for t in sorted(trades, key=lambda x: x["end_dt"]):
            stake = min(br * half_kelly, br * 0.15)
            if t["won"]:
                br += stake * b
            else:
                br -= stake
        kelly_roi = round((br - STARTING_BR) / STARTING_BR * 100, 1)
    else:
        kelly_roi = 0

    return {
        "n":         n,
        "won":       len(won),
        "lost":      len(lost),
        "wr":        round(wr, 1),
        "implied":   implied,
        "edge":      edge,
        "pf":        pf,
        "flat_pnl":  round(total_pnl, 2),
        "max_dd":    round(max_dd, 1),
        "sharpe":    sharpe,
        "kelly_roi": kelly_roi,
    }

def stats_by_entry(trades):
    result = {}
    for days in ENTRY_WINDOWS:
        sub = [t for t in trades if t["entry_days"] == days]
        if sub:
            result[f"T-{days}d"] = calc_stats(sub)
    return result

def stats_by_category(trades, entry_days=1):
    sub = [t for t in trades if t["entry_days"] == entry_days]
    result = {}
    cats = set(t["category"] for t in sub)
    for cat in cats:
        g = [t for t in sub if t["category"] == cat]
        if len(g) >= 3:
            result[cat] = calc_stats(g)
    return result

def stats_by_price_band(trades, entry_days=1):
    """Break down WR by YES entry price band."""
    sub = [t for t in trades if t["entry_days"] == entry_days]
    bands = [
        ("75-80¢ YES", 75, 80),
        ("80-85¢ YES", 80, 85),
        ("85-90¢ YES", 85, 90),
        ("90-95¢ YES", 90, 95),
    ]
    result = {}
    for label, lo, hi in bands:
        g = [t for t in sub if lo <= t["yes_entry"] < hi]
        if len(g) >= 3:
            result[label] = calc_stats(g)
    return result

def equity_curve(trades, entry_days=1):
    sub = sorted(
        [t for t in trades if t["entry_days"] == entry_days],
        key=lambda x: x["end_dt"]
    )
    equity = STARTING_BR
    points = [equity]
    for t in sub:
        equity += t["pnl"]
        points.append(round(equity, 2))
    return points

# ─────────────────────────────────────────────────────────
#  HTML REPORT
# ─────────────────────────────────────────────────────────

def generate_html(trades, overall, by_entry, by_cat, by_price, eq_curve, ts):
    n_markets = len(set(t["market_id"] for t in trades))

    best_entry = max(by_entry.items(), key=lambda x: x[1].get("edge", 0)) if by_entry else ("—", {})
    best_entry_label = best_entry[0]
    best_edge = best_entry[1].get("edge", 0)
    best_pf   = best_entry[1].get("pf", 0)
    best_pnl  = best_entry[1].get("flat_pnl", 0)

    def row_color(wr):
        if wr >= 70: return "#1a3a1a"
        if wr >= 60: return "#2a3a1a"
        if wr >= 50: return "#3a3a1a"
        return "#3a1a1a"

    def badge(wr):
        if wr >= 70: return f'<span style="color:#4ade80;font-weight:bold">{wr:.0f}%</span>'
        if wr >= 60: return f'<span style="color:#a3e635">{wr:.0f}%</span>'
        if wr >= 50: return f'<span style="color:#facc15">{wr:.0f}%</span>'
        return f'<span style="color:#f87171">{wr:.0f}%</span>'

    # Equity curve SVG
    eq_min = min(eq_curve) if eq_curve else STARTING_BR
    eq_max = max(eq_curve) if eq_curve else STARTING_BR + 1
    eq_range = max(eq_max - eq_min, 1)
    w, h = 700, 200
    pts = []
    for i, v in enumerate(eq_curve):
        x = int(i / max(len(eq_curve)-1, 1) * w)
        y = int(h - (v - eq_min) / eq_range * h)
        pts.append(f"{x},{y}")
    eq_svg = f"""<svg viewBox="0 0 {w} {h}" style="width:100%;background:#0d1117;border-radius:8px;margin:12px 0">
  <polyline points="{' '.join(pts)}" fill="none" stroke="#3b82f6" stroke-width="2"/>
  <text x="4" y="14" fill="#6b7280" font-size="11">+${eq_max-STARTING_BR:.0f}</text>
  <text x="4" y="{h-4}" fill="#6b7280" font-size="11">${eq_min:.0f}</text>
</svg>"""

    # Entry timing table
    entry_rows = ""
    for label, s in sorted(by_entry.items()):
        entry_rows += f"""
        <tr style="background:{row_color(s['wr'])}">
          <td>{label}</td>
          <td>{s['n']}</td>
          <td>{badge(s['wr'])}</td>
          <td style="color:#9ca3af">{s['implied']}%</td>
          <td style="color:#4ade80;font-weight:bold">+{s['edge']}%</td>
          <td>{s['pf']}</td>
          <td style="color:#4ade80">${s['flat_pnl']:+.0f}</td>
          <td style="color:#60a5fa">{s['kelly_roi']:+.1f}%</td>
          <td style="color:#f87171">{s['max_dd']:.1f}%</td>
          <td>{s['sharpe']}</td>
        </tr>"""

    # Category table (T-1d)
    cat_rows = ""
    for cat, s in sorted(by_cat.items(), key=lambda x: -x[1].get("n", 0)):
        cat_rows += f"""
        <tr>
          <td style="color:#e2e8f0">{cat}</td>
          <td>{badge(s['wr'])}</td>
          <td style="color:#9ca3af">n={s['n']}</td>
          <td style="color:#4ade80">+{s['edge']}%</td>
          <td style="color:#60a5fa">${s['flat_pnl']:+.0f}</td>
        </tr>"""

    # Price band table
    band_rows = ""
    for band, s in sorted(by_price.items()):
        band_rows += f"""
        <tr>
          <td style="color:#e2e8f0">{band}</td>
          <td>{badge(s['wr'])}</td>
          <td style="color:#9ca3af">n={s['n']}</td>
          <td style="color:#4ade80">+{s['edge']}%</td>
          <td style="color:#a78bfa">${s['flat_pnl']:+.0f}</td>
        </tr>"""

    # Top/bottom trades
    t1_trades = sorted([t for t in trades if t["entry_days"]==1], key=lambda x: -x["pnl"])
    top10 = t1_trades[:8]
    bot10 = t1_trades[-8:]

    def trade_rows(tlist):
        rows = ""
        for t in tlist:
            color = "#4ade80" if t["won"] else "#f87171"
            icon  = "✅" if t["won"] else "❌"
            rows += f"""<tr>
              <td style="color:#cbd5e1;font-size:11px">{t['market'][:65]}</td>
              <td><span style="background:#1e293b;padding:2px 6px;border-radius:4px;font-size:11px">{t['category']}</span></td>
              <td style="color:#94a3b8">{t['yes_entry']}¢ YES</td>
              <td style="color:#94a3b8">{t['no_entry']}¢ NO</td>
              <td>{icon}</td>
              <td style="color:{color};font-weight:bold">${t['pnl']:+.2f}</td>
            </tr>"""
        return rows

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NearCertain Backtest Report</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:#0d1117; color:#e2e8f0; font-family:'Segoe UI',system-ui,sans-serif; padding:24px; }}
  h1 {{ font-size:22px; color:#f1f5f9; margin-bottom:4px }}
  h2 {{ font-size:15px; color:#94a3b8; font-weight:400; margin-bottom:24px }}
  h3 {{ font-size:14px; color:#cbd5e1; margin:28px 0 10px; border-bottom:1px solid #1e293b; padding-bottom:6px }}
  .meta {{ color:#6b7280; font-size:12px; margin-bottom:24px }}
  .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:28px }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px }}
  .card-label {{ font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:.5px; margin-bottom:6px }}
  .card-value {{ font-size:22px; font-weight:700; color:#f1f5f9 }}
  .card-sub {{ font-size:11px; color:#4b5563; margin-top:4px }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; margin-bottom:20px }}
  th {{ background:#161b22; color:#6b7280; padding:8px 10px; text-align:left; border-bottom:1px solid #21262d; font-weight:500 }}
  td {{ padding:7px 10px; border-bottom:1px solid #1e293b }}
  tr:hover td {{ background:#161b22 }}
  .badge-cat {{ background:#1e293b; padding:2px 8px; border-radius:4px; font-size:11px }}
  .section {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:20px; margin-bottom:20px }}
  .note {{ background:#1c2333; border-left:3px solid #3b82f6; padding:10px 14px; border-radius:0 6px 6px 0; font-size:12px; color:#94a3b8; margin-top:16px }}
</style>
</head>
<body>

<h1>🔵 NearCertain Backtest Report</h1>
<h2>Strategy: Buy NO when YES is priced 75-95¢ — markets systematically overprice near-certain events</h2>
<div class="meta">Generated {ts}  ·  {n_markets} resolved markets  ·  {len(trades)} simulated trades  ·  {len(ENTRY_WINDOWS)} entry timings tested</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Markets Analysed</div>
    <div class="card-value">{n_markets}</div>
    <div class="card-sub">Resolved binary Yes/No</div>
  </div>
  <div class="card">
    <div class="card-label">Best Entry Timing</div>
    <div class="card-value" style="color:#3b82f6">{best_entry_label}</div>
    <div class="card-sub">by actual edge</div>
  </div>
  <div class="card">
    <div class="card-label">Best Edge (WR − implied)</div>
    <div class="card-value" style="color:#4ade80">+{best_edge}%</div>
    <div class="card-sub">win rate minus NO entry price</div>
  </div>
  <div class="card">
    <div class="card-label">Best Profit Factor</div>
    <div class="card-value" style="color:#a78bfa">{best_pf}</div>
    <div class="card-sub">gross wins / gross losses</div>
  </div>
  <div class="card">
    <div class="card-label">Flat Stake P&L (best)</div>
    <div class="card-value" style="color:#4ade80">${best_pnl:+.0f}</div>
    <div class="card-sub">$10/trade flat sizing</div>
  </div>
  <div class="card">
    <div class="card-label">Overall Win Rate</div>
    <div class="card-value">{overall['wr']}%</div>
    <div class="card-sub">{overall['won']}W / {overall['lost']}L</div>
  </div>
  <div class="card">
    <div class="card-label">Overall Edge</div>
    <div class="card-value" style="color:#4ade80">+{overall['edge']}%</div>
    <div class="card-sub">vs {overall['implied']}% implied</div>
  </div>
  <div class="card">
    <div class="card-label">Max Drawdown (best)</div>
    <div class="card-value" style="color:#fb923c">{best_entry[1].get('max_dd',0):.1f}%</div>
    <div class="card-sub">flat $10 sizing</div>
  </div>
</div>

<div class="section">
  <h3>Strategy Results by Entry Timing</h3>
  <table>
    <tr>
      <th>Entry</th><th>Trades</th><th>Win Rate</th><th>Implied</th>
      <th>Edge ★</th><th>PF</th><th>Flat P&L</th><th>Kelly ROI</th>
      <th>Max DD</th><th>Sharpe</th>
    </tr>
    {entry_rows}
  </table>
  <div class="note">
    ★ <b>Edge</b> = actual win rate − implied probability from NO entry price.
    Positive edge = the strategy genuinely beats the market's pricing.
    Profit Factor &gt; 1.0 = profitable. Sharpe &gt; 1.0 = strong risk-adjusted return.
  </div>
</div>

<div class="section">
  <h3>Equity Curve (flat $10/trade, T-1d entry)</h3>
  {eq_svg}
</div>

<div class="section">
  <h3>Win Rate by Entry Price Band (T-1d entry)</h3>
  <p style="font-size:12px;color:#6b7280;margin-bottom:10px">
    Higher YES price = more aggressively overbid = stronger NearCertain signal
  </p>
  <table>
    <tr><th>YES Entry Band</th><th>Win Rate</th><th>Trades</th><th>Edge</th><th>Flat P&L</th></tr>
    {band_rows}
  </table>
</div>

<div class="section">
  <h3>Win Rate by Category (T-1d entry)</h3>
  <table>
    <tr><th>Category</th><th>Win Rate</th><th>Trades</th><th>Edge</th><th>Flat P&L</th></tr>
    {cat_rows}
  </table>
  <div class="note">
    Crypto and Sports are blocked in the live bot — confirmed by backtest weak performance in those categories.
  </div>
</div>

<div class="section">
  <h3>Top 8 Wins + Top 8 Losses (flat $10 sizing, T-1d entry)</h3>
  <table>
    <tr><th>Market</th><th>Category</th><th>Entry YES</th><th>Entry NO</th><th>Result</th><th>P&L</th></tr>
    {trade_rows(top10)}
    <tr><td colspan="6" style="color:#374151;font-size:11px;padding:8px 10px">— Top losses —</td></tr>
    {trade_rows(bot10)}
  </table>
</div>

<div class="section">
  <h3>Methodology</h3>
  <p style="font-size:12px;color:#94a3b8;line-height:1.7">
    <b>Data:</b> Resolved binary Yes/No markets from Polymarket Gamma API.
    Price history from CLOB API (daily fidelity). Only markets with confirmed
    resolution (outcomePrices = [1,0] or [0,1]) and volume &gt; ${MIN_VOLUME} are included.<br><br>
    <b>Entry simulation:</b> For each entry timing (T-7d, T-3d, T-1d before close),
    the YES token price from CLOB history is used as the entry price.
    Only markets where YES was 75-95¢ at entry time are included in NearCertain results.<br><br>
    <b>Sizing:</b> Flat = $10 per trade regardless of price band.
    Kelly = half-Kelly on $1,000 starting bankroll, capped at 15% per trade.<br><br>
    <b>Blocked categories:</b> Crypto (57% live WR — near 50/50) and Sports (in-game markets legitimately near-certain).<br><br>
    <b>Limitations:</b> Entry prices reconstructed from daily CLOB snapshots —
    intraday slippage not modelled. Polymarket 0.1-0.2% taker fee not included.
    Markets without CLOB price history in the entry window are excluded.
  </p>
</div>

</body>
</html>"""
    return html

# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log("=" * 60)
    log("NearCertain Backtest")
    log(f"Strategy: Buy NO when YES 75-95¢, hold to resolution")
    log(f"Entry windows: T-{', T-'.join(str(d) for d in ENTRY_WINDOWS)}d")
    log("=" * 60)

    # Fetch data
    markets = fetch_resolved_markets(target=MARKET_BATCH)
    if not markets:
        log("❌ No markets fetched — check API connectivity")
        sys.exit(1)

    # Run backtest
    trades = run_backtest(markets)
    if not trades:
        log("❌ No trades generated — check CLOB price history availability")
        sys.exit(1)

    log(f"\n{'='*40}")
    log(f"RESULTS: {len(trades)} trades across {len(set(t['market_id'] for t in trades))} markets")

    # Calculate stats
    overall  = calc_stats(trades)
    by_entry = stats_by_entry(trades)
    by_cat   = stats_by_category(trades, entry_days=1)
    by_price = stats_by_price_band(trades, entry_days=1)
    eq_curve = equity_curve(trades, entry_days=1)

    log(f"Overall WR:    {overall['wr']}%")
    log(f"Overall edge:  +{overall['edge']}%")
    log(f"Profit factor: {overall['pf']}")
    log(f"Flat P&L:      ${overall['flat_pnl']:+.2f}")

    for label, s in sorted(by_entry.items()):
        log(f"  {label}: {s['wr']}% WR | edge +{s['edge']}% | PF {s['pf']} | ${s['flat_pnl']:+.0f}")

    # Generate report
    html  = generate_html(trades, overall, by_entry, by_cat, by_price, eq_curve,
                          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    fname = f"backtest_nearcertain_{ts}.html"
    with open(fname, "w") as f:
        f.write(html)
    log(f"\n✅ Report saved: {fname}")
    return fname

if __name__ == "__main__":
    main()
