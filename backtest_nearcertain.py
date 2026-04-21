#!/usr/bin/env python3
"""
backtest_nearcertain.py
Downloads the Jon-Becker Polymarket dataset and backtests the NearCertain strategy.
Strategy: Buy NO when YES is priced 75-95c.
Run: python backtest_nearcertain.py
Output: backtest_nearcertain_<timestamp>.html
"""

import os, sys, json, math, time, statistics, subprocess, urllib.request
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

DATA_DIR    = Path("data/polymarket")
MARKETS_DIR = DATA_DIR / "markets"
TRADES_DIR  = DATA_DIR / "trades"
DATA_URL    = "https://data.jbecker.dev/data.tar.zst"
FLAT_STAKE  = 10.0
STARTING_BR = 1000.0
YES_MIN, YES_MAX = 75, 95
MIN_VOLUME  = 2000
BLOCKED_CATS = {"sports", "crypto"}
ENTRY_WINDOWS = [1, 3, 7]

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def get_category(q):
    q = (q or "").lower()
    if any(k in q for k in ["bitcoin","btc","ethereum","eth","crypto","solana","bnb","xrp"]):
        return "crypto"
    if any(k in q for k in ["temperature","weather","rain","snow","celsius","forecast","°c","°f"]):
        return "weather"
    if any(k in q for k in ["election","vote","president","senate","trump","democrat","republican","parliament"]):
        return "politics"
    if any(k in q for k in ["gdp","inflation","cpi","fed","fomc","rate","earnings","stock","nasdaq","s&p","tesla","apple","nvidia"]):
        return "economics"
    if any(k in q for k in ["goal","win","score","match","game","nba","nfl","fifa","league","tournament","sport"]):
        return "sports"
    if any(k in q for k in ["outbreak","cases","disease","virus","measles","covid","flu"]):
        return "health"
    return "other"

def download_dataset():
    if MARKETS_DIR.exists() and any(MARKETS_DIR.glob("*.parquet")):
        log(f"Dataset already present ({len(list(MARKETS_DIR.glob('*.parquet')))} market files)")
        return True
    log(f"Downloading dataset from {DATA_URL}...")
    archive = Path("data.tar.zst")
    try:
        urllib.request.urlretrieve(DATA_URL, archive)
        log(f"  Downloaded {archive.stat().st_size/1024/1024:.0f} MB")
        Path("data").mkdir(exist_ok=True)
        r = subprocess.run(["tar", "-xf", str(archive), "-C", "data"], capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  tar failed: {r.stderr[:200]}")
            return False
        archive.unlink(missing_ok=True)
        log("  Extracted successfully")
        return True
    except Exception as e:
        log(f"  Failed: {e}")
        return False

def load_data():
    try:
        import duckdb
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "duckdb", "pyarrow", "-q"])
        import duckdb

    con = duckdb.connect()
    mfiles = list(MARKETS_DIR.glob("*.parquet"))
    tfiles = list(TRADES_DIR.glob("*.parquet")) if TRADES_DIR.exists() else []
    log(f"Market parquet files: {len(mfiles)}, Trade parquet files: {len(tfiles)}")

    if not mfiles:
        return None, None

    # Inspect schema
    sample = con.execute(f"SELECT * FROM '{mfiles[0]}' LIMIT 1").df()
    log(f"Market columns: {list(sample.columns)}")

    # Load resolved markets
    mdf = con.execute(f"SELECT * FROM '{MARKETS_DIR}/*.parquet' LIMIT 10000").df()
    log(f"Loaded {len(mdf)} markets")

    tdf = None
    if tfiles:
        tsample = con.execute(f"SELECT * FROM '{tfiles[0]}' LIMIT 1").df()
        log(f"Trade columns: {list(tsample.columns)}")
        tdf = con.execute(f"SELECT * FROM '{TRADES_DIR}/*.parquet' LIMIT 3000000").df()
        log(f"Loaded {len(tdf)} trades")

    return mdf, tdf

def run_backtest(mdf, tdf):
    log("Running backtest...")
    trades = []
    stats  = defaultdict(int)
    cols   = list(mdf.columns)

    def col(*candidates):
        for c in candidates:
            if c in cols: return c
        return None

    q_col  = col("question","title","market_question")
    res_col= col("outcome","resolved_outcome","winner","result")
    p_col  = col("bestAsk","best_ask","outcomePrices","price","lastTradePrice")
    v_col  = col("volume","volume24hr","liquidity")
    e_col  = col("endDate","end_date","endDateIso","closedTime","close_time")
    id_col = col("id","market_id","condition_id","conditionId")
    tk_col = col("clobTokenIds","clob_token_ids","token_id")

    log(f"  Columns mapped: q={q_col} res={res_col} price={p_col} vol={v_col} end={e_col}")

    # Build price lookup from trades
    price_lookup = defaultdict(list)
    if tdf is not None:
        tcols = list(tdf.columns)
        tt    = next((c for c in ["asset_id","token_id","makerAssetId","market"] if c in tcols), None)
        tp    = next((c for c in ["price","yes_price","p"] if c in tcols), None)
        ts    = next((c for c in ["timestamp","time","created_at","t"] if c in tcols), None)
        if tt and tp and ts:
            log(f"  Building price lookup (token={tt}, price={tp}, time={ts})...")
            for _, row in tdf.iterrows():
                tid = str(row.get(tt,""))
                p   = row.get(tp)
                t   = row.get(ts)
                if tid and p is not None and t is not None:
                    price_lookup[tid].append((float(t), float(p)))
            log(f"  Price lookup: {len(price_lookup)} tokens")

    for _, m in mdf.iterrows():
        question = str(m.get(q_col,"") if q_col else "")
        cat      = get_category(question)

        if cat in BLOCKED_CATS:
            stats["skip_cat"] += 1
            continue

        vol = 0
        if v_col:
            try: vol = float(m.get(v_col,0) or 0)
            except: pass
        if vol < MIN_VOLUME:
            stats["skip_vol"] += 1
            continue

        # Resolution
        resolved_yes = None
        if res_col:
            r = str(m.get(res_col,"")).lower()
            if r in ("yes","1","true","win"): resolved_yes = True
            elif r in ("no","0","false","lose"): resolved_yes = False
        if resolved_yes is None:
            op = m.get("outcomePrices")
            if op:
                try:
                    p = json.loads(op) if isinstance(op,str) else op
                    if float(p[0])>=0.99: resolved_yes = True
                    elif float(p[1])>=0.99: resolved_yes = False
                except: pass
        if resolved_yes is None:
            stats["skip_no_res"] += 1
            continue

        # End timestamp
        end_ts = None
        if e_col:
            ev = m.get(e_col)
            if ev:
                try:
                    if isinstance(ev,(int,float)): end_ts = float(ev)
                    else:
                        dt = datetime.fromisoformat(str(ev).replace("Z","+00:00"))
                        end_ts = dt.timestamp()
                except: pass

        # Token ID
        token_id = None
        if tk_col:
            t = m.get(tk_col)
            if t:
                try:
                    if isinstance(t,str) and t.startswith("["):
                        ids = json.loads(t)
                        token_id = ids[0] if ids else None
                    elif isinstance(t,(list,tuple)):
                        token_id = t[0] if t else None
                    else:
                        token_id = str(t)
                except: token_id = str(t)

        market_id = str(m.get(id_col,"") if id_col else "")

        for days_before in ENTRY_WINDOWS:
            yes_price = None

            # Try trade history
            if token_id and end_ts and price_lookup.get(str(token_id)):
                target = end_ts - days_before * 86400
                pts    = price_lookup[str(token_id)]
                best   = min(pts, key=lambda x: abs(x[0]-target))
                if abs(best[0]-target) < 4*86400:
                    p = best[1]
                    yes_price = p if p<=1 else p/100

            # Fallback to market price
            if yes_price is None and p_col:
                raw = m.get(p_col)
                if raw:
                    try:
                        if isinstance(raw,str) and raw.startswith("["):
                            prices = json.loads(raw)
                            yes_price = float(prices[0])
                        else:
                            yes_price = float(raw)
                        if yes_price > 1: yes_price /= 100
                    except: pass

            if yes_price is None:
                if days_before == ENTRY_WINDOWS[0]:
                    stats["no_price"] += 1
                continue

            yp = round(yes_price*100, 1)
            np_ = round((1-yes_price)*100, 1)

            if not (YES_MIN <= yp <= YES_MAX):
                if days_before == ENTRY_WINDOWS[0]:
                    stats["wrong_range"] += 1
                continue

            if np_ <= 0: continue

            won = not resolved_yes
            pnl = round(FLAT_STAKE*100/np_ - FLAT_STAKE, 2) if won else -FLAT_STAKE

            try:
                from datetime import datetime as dt
                end_date = dt.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m") if end_ts else "unknown"
            except: end_date = "unknown"

            trades.append({
                "market_id":  market_id,
                "market":     question[:80],
                "category":   cat,
                "entry_days": days_before,
                "yes_entry":  yp,
                "no_entry":   np_,
                "won":        won,
                "pnl":        pnl,
                "volume":     vol,
                "end_dt":     end_date,
            })

    log(f"  Generated {len(trades)} trades")
    for k,v in stats.items():
        if v: log(f"  {k}: {v}")
    return trades

def calc_stats(trades):
    if not trades: return {}
    won  = [t for t in trades if t["won"]]
    lost = [t for t in trades if not t["won"]]
    n    = len(trades)
    wr   = len(won)/n*100
    total= sum(t["pnl"] for t in trades)
    gw   = sum(t["pnl"] for t in won) if won else 0
    gl   = abs(sum(t["pnl"] for t in lost)) if lost else 0.001
    pf   = round(gw/gl, 2)
    avg  = statistics.mean(t["no_entry"] for t in trades)
    edge = round(wr-avg, 1)
    daily= defaultdict(float)
    for t in trades: daily[t["end_dt"]] += t["pnl"]
    dv   = list(daily.values())
    sharpe = round(statistics.mean(dv)/statistics.stdev(dv)*math.sqrt(252),2) if len(dv)>1 and statistics.stdev(dv) else 0
    equity = STARTING_BR; peak = STARTING_BR; max_dd = 0
    for t in sorted(trades, key=lambda x: x["end_dt"]):
        equity += t["pnl"]
        if equity>peak: peak=equity
        dd=(peak-equity)/peak*100 if peak>0 else 0
        if dd>max_dd: max_dd=dd
    p=wr/100; b=(100/avg)-1 if avg>0 else 0
    kelly_roi=0
    if b>0 and p>0:
        hk=max(0,min(((b*p-(1-p))/b)*0.5,0.15))
        br=STARTING_BR
        for t in sorted(trades,key=lambda x:x["end_dt"]):
            s=min(br*hk,br*0.15)
            br=br+s*b if t["won"] else br-s
        kelly_roi=round((br-STARTING_BR)/STARTING_BR*100,1)
    return {"n":n,"won":len(won),"lost":len(lost),"wr":round(wr,1),"implied":round(avg,1),
            "edge":edge,"pf":pf,"flat_pnl":round(total,2),"max_dd":round(max_dd,1),"sharpe":sharpe,"kelly_roi":kelly_roi}

def stats_by_entry(trades):
    return {f"T-{d}d": calc_stats([t for t in trades if t["entry_days"]==d])
            for d in ENTRY_WINDOWS if [t for t in trades if t["entry_days"]==d]}

def stats_by_category(trades, ed=1):
    sub=[ t for t in trades if t["entry_days"]==ed]
    return {c: calc_stats([t for t in sub if t["category"]==c])
            for c in set(t["category"] for t in sub)
            if len([t for t in sub if t["category"]==c])>=3}

def stats_by_band(trades, ed=1):
    sub=[t for t in trades if t["entry_days"]==ed]
    return {l: calc_stats([t for t in sub if lo<=t["yes_entry"]<hi])
            for l,lo,hi in [("75-80¢",75,80),("80-85¢",80,85),("85-90¢",85,90),("90-95¢",90,95)]
            if len([t for t in sub if lo<=t["yes_entry"]<hi])>=3}

def equity_curve(trades, ed=1):
    sub=sorted([t for t in trades if t["entry_days"]==ed],key=lambda x:x["end_dt"])
    br=STARTING_BR; pts=[br]
    for t in sub: br+=t["pnl"]; pts.append(round(br,2))
    return pts

def gen_html(trades, overall, by_entry, by_cat, by_band, eq, ts):
    nm=len(set(t["market_id"] for t in trades))
    be=max(by_entry.items(),key=lambda x:x[1].get("edge",0)) if by_entry else ("—",{})
    def badge(wr):
        if wr>=70: return f'<span style="color:#4ade80;font-weight:bold">{wr:.0f}%</span>'
        if wr>=60: return f'<span style="color:#a3e635">{wr:.0f}%</span>'
        if wr>=50: return f'<span style="color:#facc15">{wr:.0f}%</span>'
        return f'<span style="color:#f87171">{wr:.0f}%</span>'
    def rc(wr): return "#1a3a1a" if wr>=70 else "#2a3a1a" if wr>=60 else "#3a3a1a" if wr>=50 else "#3a1a1a"
    eq_min=min(eq); eq_max=max(eq); w,h=700,200
    pts=" ".join(f"{int(i/max(len(eq)-1,1)*w)},{int(h-(v-eq_min)/max(eq_max-eq_min,1)*h)}" for i,v in enumerate(eq))
    svg=f'<svg viewBox="0 0 {w} {h}" style="width:100%;background:#0d1117;border-radius:8px;margin:12px 0"><polyline points="{pts}" fill="none" stroke="#3b82f6" stroke-width="2"/><text x="4" y="14" fill="#6b7280" font-size="11">+${eq_max-STARTING_BR:.0f}</text><text x="4" y="{h-4}" fill="#6b7280" font-size="11">${eq_min:.0f}</text></svg>'
    er="".join(f'<tr style="background:{rc(s["wr"])}"><td>{l}</td><td>{s["n"]}</td><td>{badge(s["wr"])}</td><td style="color:#9ca3af">{s["implied"]}%</td><td style="color:#4ade80;font-weight:bold">+{s["edge"]}%</td><td>{s["pf"]}</td><td style="color:#4ade80">${s["flat_pnl"]:+.0f}</td><td style="color:#60a5fa">{s["kelly_roi"]:+.1f}%</td><td style="color:#f87171">{s["max_dd"]:.1f}%</td><td>{s["sharpe"]}</td></tr>' for l,s in sorted(by_entry.items()))
    cr="".join(f'<tr><td>{c}</td><td>{badge(s["wr"])}</td><td style="color:#9ca3af">n={s["n"]}</td><td style="color:#4ade80">+{s["edge"]}%</td><td>${s["flat_pnl"]:+.0f}</td></tr>' for c,s in sorted(by_cat.items(),key=lambda x:-x[1].get("n",0)))
    br_rows="".join(f'<tr><td>{b}</td><td>{badge(s["wr"])}</td><td style="color:#9ca3af">n={s["n"]}</td><td style="color:#4ade80">+{s["edge"]}%</td><td>${s["flat_pnl"]:+.0f}</td></tr>' for b,s in sorted(by_band.items()))
    t1=sorted([t for t in trades if t["entry_days"]==1],key=lambda x:-x["pnl"])
    def tr_(t): c="#4ade80" if t["won"] else "#f87171"; return f'<tr><td style="color:#cbd5e1;font-size:11px">{t["market"][:65]}</td><td style="font-size:11px">{t["category"]}</td><td style="color:#94a3b8">{t["yes_entry"]}¢</td><td>{"✅" if t["won"] else "❌"}</td><td style="color:{c};font-weight:bold">${t["pnl"]:+.2f}</td></tr>'
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>NearCertain Backtest</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{background:#0d1117;color:#e2e8f0;font-family:system-ui,sans-serif;padding:24px}}h1{{font-size:22px;color:#f1f5f9;margin-bottom:4px}}h2{{font-size:13px;color:#94a3b8;font-weight:400;margin-bottom:20px}}.meta{{color:#6b7280;font-size:12px;margin-bottom:24px}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:28px}}.card{{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px}}.cl{{font-size:11px;color:#6b7280;text-transform:uppercase;margin-bottom:6px}}.cv{{font-size:22px;font-weight:700}}.cs{{font-size:11px;color:#4b5563;margin-top:4px}}table{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px}}th{{background:#161b22;color:#6b7280;padding:8px 10px;text-align:left;border-bottom:1px solid #21262d;font-weight:500}}td{{padding:7px 10px;border-bottom:1px solid #1e293b}}.sec{{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:20px}}h3{{font-size:14px;color:#cbd5e1;margin:0 0 10px;border-bottom:1px solid #1e293b;padding-bottom:6px}}.note{{background:#1c2333;border-left:3px solid #3b82f6;padding:10px 14px;border-radius:0 6px 6px 0;font-size:12px;color:#94a3b8;margin-top:12px}}</style></head><body>
<h1>🔵 NearCertain Backtest Report</h1>
<h2>Buy NO when YES 75-95¢ — prediction markets overprice near-certain events</h2>
<div class="meta">Generated {ts} · {nm} markets · {len(trades)} trades · jon-becker/prediction-market-analysis dataset</div>
<div class="grid">
<div class="card"><div class="cl">Markets</div><div class="cv">{nm}</div></div>
<div class="card"><div class="cl">Best Entry</div><div class="cv" style="color:#3b82f6">{be[0]}</div></div>
<div class="card"><div class="cl">Best Edge</div><div class="cv" style="color:#4ade80">+{be[1].get("edge",0)}%</div></div>
<div class="card"><div class="cl">Best PF</div><div class="cv" style="color:#a78bfa">{be[1].get("pf",0)}</div></div>
<div class="card"><div class="cl">Overall WR</div><div class="cv">{overall.get("wr",0)}%</div><div class="cs">{overall.get("won",0)}W/{overall.get("lost",0)}L</div></div>
<div class="card"><div class="cl">Overall Edge</div><div class="cv" style="color:#4ade80">+{overall.get("edge",0)}%</div></div>
<div class="card"><div class="cl">Flat P&L (best)</div><div class="cv" style="color:#4ade80">${be[1].get("flat_pnl",0):+.0f}</div></div>
<div class="card"><div class="cl">Max DD</div><div class="cv" style="color:#fb923c">{be[1].get("max_dd",0):.1f}%</div></div>
</div>
<div class="sec"><h3>Results by Entry Timing</h3>
<table><tr><th>Entry</th><th>Trades</th><th>Win Rate</th><th>Implied</th><th>Edge ★</th><th>PF</th><th>Flat P&L</th><th>Kelly ROI</th><th>Max DD</th><th>Sharpe</th></tr>{er}</table>
<div class="note">★ Edge = actual WR − implied probability from NO entry price. Positive = strategy beats market pricing.</div></div>
<div class="sec"><h3>Equity Curve (flat $10/trade, T-1d entry)</h3>{svg}</div>
<div class="sec"><h3>Win Rate by YES Entry Price Band (T-1d)</h3>
<table><tr><th>YES Band</th><th>Win Rate</th><th>Trades</th><th>Edge</th><th>Flat P&L</th></tr>{br_rows}</table></div>
<div class="sec"><h3>Win Rate by Category (T-1d)</h3>
<table><tr><th>Category</th><th>Win Rate</th><th>Trades</th><th>Edge</th><th>P&L</th></tr>{cr}</table>
<div class="note">Crypto + Sports blocked in live bot based on backtest data.</div></div>
<div class="sec"><h3>Top 8 Wins + Top 8 Losses (T-1d)</h3>
<table><tr><th>Market</th><th>Category</th><th>YES Entry</th><th>Result</th><th>P&L</th></tr>
{"".join(tr_(t) for t in t1[:8])}
<tr><td colspan="5" style="color:#374151;font-size:11px;padding:8px">— Top losses —</td></tr>
{"".join(tr_(t) for t in t1[-8:])}
</table></div>
<div class="sec"><h3>Methodology</h3>
<p style="font-size:12px;color:#94a3b8;line-height:1.7"><b>Data:</b> Jon-Becker prediction-market-analysis dataset (Polymarket on-chain data from Cloudflare R2).<br><br>
<b>Strategy:</b> NearCertain buys NO when YES is priced 75-95¢. Entry at T-7d, T-3d, T-1d using trade price history.<br><br>
<b>Blocked:</b> Crypto and Sports categories.<br><b>Min volume:</b> ${MIN_VOLUME}.<br><br>
<b>Limitations:</b> Price history from trade snapshots. 0.1-0.2% taker fee not included.</p></div>
</body></html>"""

def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log("="*60)
    log("NearCertain Backtest — jon-becker dataset")
    log(f"Strategy: Buy NO when YES {YES_MIN}-{YES_MAX}¢")
    log("="*60)

    if not download_dataset():
        sys.exit(1)

    mdf, tdf = load_data()
    if mdf is None or len(mdf)==0:
        log("❌ No data loaded"); sys.exit(1)

    trades = run_backtest(mdf, tdf)
    if not trades:
        log("❌ No trades generated"); sys.exit(1)

    log(f"\nRESULTS: {len(trades)} trades across {len(set(t['market_id'] for t in trades))} markets")
    overall  = calc_stats(trades)
    by_entry = stats_by_entry(trades)
    by_cat   = stats_by_category(trades)
    by_band  = stats_by_band(trades)
    eq       = equity_curve(trades)

    log(f"Overall: {overall['wr']}% WR | edge +{overall['edge']}% | PF {overall['pf']} | ${overall['flat_pnl']:+.2f}")
    for l,s in sorted(by_entry.items()):
        log(f"  {l}: {s['wr']}% WR | edge +{s['edge']}% | ${s['flat_pnl']:+.0f}")

    html  = gen_html(trades, overall, by_entry, by_cat, by_band, eq, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    fname = f"backtest_nearcertain_{ts}.html"
    with open(fname,"w") as f: f.write(html)
    log(f"\n✅ Report: {fname}")

if __name__=="__main__":
    main()
