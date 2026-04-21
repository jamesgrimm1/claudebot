#!/usr/bin/env python3
"""
build_graph.py — Trade Knowledge Graph Builder
Reads trade_reflections/*.md and generates graphify-out/GRAPH_REPORT.md
No LLM needed. Pure pattern extraction from resolved trade history.
Run after claudebot.py to update the knowledge base Opus reads.
"""

import os
import re
import json
from collections import defaultdict
from datetime import datetime

REFLECTIONS_DIR = "trade_reflections"
OUTPUT_DIR      = "graphify-out"
REPORT_FILE     = os.path.join(OUTPUT_DIR, "GRAPH_REPORT.md")
GRAPH_JSON      = os.path.join(OUTPUT_DIR, "graph.json")

def parse_reflection(path):
    """Extract structured data from a reflection markdown file."""
    with open(path) as f:
        text = f.read()

    def get(pattern, default=None):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    won      = "**Result:** WON" in text
    category = get(r"\*\*Category:\*\* (.+)")
    conf_str = get(r"\*\*Confidence:\*\* (.+?)%")
    conf     = int(conf_str) if conf_str and conf_str.isdigit() else None
    hold_str = get(r"\*\*Hold duration:\*\* (.+)")
    hold_h   = None
    if hold_str:
        m = re.search(r"([\d.]+)\s*hours?", hold_str)
        if m:
            hold_h = float(m.group(1))
    tier     = get(r"\*\*Tier:\*\* T(\d)")
    position = get(r"\*\*Position:\*\* (YES|NO)")
    pnl_str  = get(r"\*\*Result:\*\* (?:WON|LOST) \$([+-][\d.]+)")
    pnl      = float(pnl_str) if pnl_str else 0
    market   = get(r"\*\*Market:\*\* (.+)")
    bear     = get(r"## Bear Case\n(.+)", "")
    research = get(r"## Research\n(.+?)(?=\n##|\Z)", "")
    news     = "True" in (get(r"\*\*News triggered:\*\* (.+)") or "")

    return {
        "won":      won,
        "category": category or "other",
        "conf":     conf,
        "hold_h":   hold_h,
        "tier":     tier or "1",
        "position": position or "?",
        "pnl":      pnl,
        "market":   market or "",
        "bear":     bear,
        "news":     news,
        "path":     path,
    }

def build_report(trades):
    if not trades:
        return "# Trade Knowledge Graph\n\nNo resolved trades yet.\n"

    total  = len(trades)
    won    = [t for t in trades if t["won"]]
    lost   = [t for t in trades if not t["won"]]
    wr     = len(won) / total * 100

    lines = [
        "# Trade Knowledge Graph — Prescient",
        f"*Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · {total} resolved trades*",
        "",
        "---",
        "",
        "## Overall Performance",
        f"- **Win rate:** {wr:.1f}% ({len(won)}W / {len(lost)}L)",
        f"- **Total P&L:** ${sum(t['pnl'] for t in trades):+.2f}",
        f"- **Avg win:** ${sum(t['pnl'] for t in won)/len(won):.2f}" if won else "",
        f"- **Avg loss:** ${sum(t['pnl'] for t in lost)/len(lost):.2f}" if lost else "",
        "",
    ]

    # ── By category ──────────────────────────────────────────
    lines += ["## Win Rate by Category", ""]
    cats = defaultdict(list)
    for t in trades:
        cats[t["category"]].append(t)

    for cat, group in sorted(cats.items(), key=lambda x: -len(x[1])):
        w = [t for t in group if t["won"]]
        wr_c = len(w)/len(group)*100
        pnl_c = sum(t["pnl"] for t in group)
        flag = " ⭐" if wr_c >= 80 else (" ⚠️" if wr_c < 50 else "")
        lines.append(f"- **{cat}**: {wr_c:.0f}% WR ({len(w)}W/{len(group)-len(w)}L) · P&L ${pnl_c:+.2f}{flag}")
    lines.append("")

    # ── By confidence band ────────────────────────────────────
    conf_trades = [t for t in trades if t["conf"] is not None]
    if conf_trades:
        lines += ["## Calibration — Confidence vs Actual Win Rate", ""]
        bands = [(75,79),(80,84),(85,89),(90,94),(95,100)]
        for lo, hi in bands:
            g = [t for t in conf_trades if lo <= t["conf"] <= hi]
            if not g: continue
            w = [t for t in g if t["won"]]
            wr_b = len(w)/len(g)*100
            gap = wr_b - (lo+hi)/2
            flag = " ✅ well-calibrated" if abs(gap) < 5 else (" ⚠️ OVERCONFIDENT" if gap < -10 else " 🎯 underconfident")
            lines.append(f"- **{lo}-{hi}% conf**: {wr_b:.0f}% actual WR ({len(g)} trades) — {gap:+.0f}pt gap{flag}")
        lines.append("")

    # ── By hold duration ─────────────────────────────────────
    hold_trades = [t for t in trades if t["hold_h"] is not None]
    if hold_trades:
        lines += ["## Win Rate by Hold Duration", ""]
        dur_buckets = [
            ("< 2 hours",   0,    2),
            ("2–6 hours",   2,    6),
            ("6–12 hours",  6,   12),
            ("12–24 hours", 12,  24),
            ("1–2 days",    24,  48),
            ("2–7 days",    48, 168),
        ]
        for label, lo, hi in dur_buckets:
            g = [t for t in hold_trades if lo <= t["hold_h"] < hi]
            if not g: continue
            w = [t for t in g if t["won"]]
            wr_d = len(w)/len(g)*100
            flag = " ⭐ SWEET SPOT" if wr_d >= 80 else (" ⚠️ AVOID" if wr_d < 50 else "")
            lines.append(f"- **{label}**: {wr_d:.0f}% WR ({len(g)} trades){flag}")
        lines.append("")

    # ── By position ───────────────────────────────────────────
    lines += ["## YES vs NO Performance", ""]
    for pos in ["YES", "NO"]:
        g = [t for t in trades if t["position"] == pos]
        if not g: continue
        w = [t for t in g if t["won"]]
        wr_p = len(w)/len(g)*100
        lines.append(f"- **{pos}**: {wr_p:.0f}% WR ({len(g)} trades) · P&L ${sum(t['pnl'] for t in g):+.2f}")
    lines.append("")

    # ── News triggered ────────────────────────────────────────
    news_trades = [t for t in trades if t["news"]]
    if news_trades:
        w = [t for t in news_trades if t["won"]]
        wr_n = len(w)/len(news_trades)*100
        lines += [
            "## News-Triggered Trades",
            f"- **Win rate:** {wr_n:.0f}% ({len(news_trades)} trades)",
            ""
        ]

    # ── Key patterns for Opus ─────────────────────────────────
    lines += ["## Key Patterns (for trade evaluation)", ""]

    # Best category
    best_cat = max(cats.items(), key=lambda x: len([t for t in x[1] if t["won"]])/len(x[1]) if len(x[1])>=3 else 0)
    worst_cat = min(cats.items(), key=lambda x: len([t for t in x[1] if t["won"]])/len(x[1]) if len(x[1])>=3 else 1)
    if len(best_cat[1]) >= 3:
        lines.append(f"- Best category: **{best_cat[0]}** — prioritise these markets")
    if len(worst_cat[1]) >= 3 and len([t for t in worst_cat[1] if t["won"]])/len(worst_cat[1]) < 0.5:
        lines.append(f"- Worst category: **{worst_cat[0]}** — apply extra scrutiny or avoid")

    # Calibration warning
    high_conf = [t for t in conf_trades if t["conf"] and t["conf"] >= 85]
    if len(high_conf) >= 5:
        hc_wr = len([t for t in high_conf if t["won"]])/len(high_conf)*100
        if hc_wr < 70:
            lines.append(f"- ⚠️ CALIBRATION WARNING: 85%+ confidence trades hitting only {hc_wr:.0f}% WR — Opus is overconfident at high confidence levels. Apply skepticism and reduce Kelly sizing on high-confidence calls.")
        else:
            lines.append(f"- ✅ High confidence trades ({hc_wr:.0f}% WR) — calibration looks reasonable")

    # Duration pattern
    fast = [t for t in hold_trades if t["hold_h"] < 12 if t["won"] is not None]
    slow = [t for t in hold_trades if t["hold_h"] >= 24 if t["won"] is not None]
    if len(fast) >= 3 and len(slow) >= 3:
        fast_wr = len([t for t in fast if t["won"]])/len(fast)*100
        slow_wr = len([t for t in slow if t["won"]])/len(slow)*100
        if fast_wr > slow_wr + 15:
            lines.append(f"- ⭐ SHORT HOLD EDGE: <12h trades at {fast_wr:.0f}% WR vs {slow_wr:.0f}% for 24h+. Prefer near-resolution markets.")

    # Bear case patterns from losses
    bear_themes = defaultdict(int)
    for t in lost:
        b = (t["bear"] or "").lower()
        for theme in ["stale data", "resolution", "ambiguous", "sharp", "informed", "already priced", "forecast", "moved"]:
            if theme in b:
                bear_themes[theme] += 1
    if bear_themes:
        top_bears = sorted(bear_themes.items(), key=lambda x: -x[1])[:3]
        lines.append(f"- Recurring loss themes: {', '.join(f'{k} ({v}x)' for k,v in top_bears)}")

    lines += ["", "---", "*This report updates automatically after every trade resolution.*"]

    return "\n".join(lines)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not os.path.exists(REFLECTIONS_DIR):
        print(f"No {REFLECTIONS_DIR}/ directory found — nothing to process")
        return

    files = [f for f in os.listdir(REFLECTIONS_DIR) if f.endswith(".md")]
    if not files:
        print("No reflection files found")
        return

    print(f"Processing {len(files)} reflection files...")
    trades = []
    for fname in files:
        path = os.path.join(REFLECTIONS_DIR, fname)
        try:
            trades.append(parse_reflection(path))
        except Exception as e:
            print(f"  ⚠️  Could not parse {fname}: {e}")

    print(f"  Parsed {len(trades)} trades ({len([t for t in trades if t['won']])}W / {len([t for t in trades if not t['won']])}L)")

    report = build_report(trades)

    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"  ✅ Report written to {REPORT_FILE}")

    # Also save graph.json with raw trade data for future querying
    graph_data = {
        "generated": datetime.utcnow().isoformat(),
        "total_trades": len(trades),
        "trades": trades
    }
    with open(GRAPH_JSON, "w") as f:
        json.dump(graph_data, f, indent=2)
    print(f"  ✅ Graph data written to {GRAPH_JSON}")

    # Print summary
    if trades:
        wr = len([t for t in trades if t["won"]])/len(trades)*100
        print(f"\n  Overall WR: {wr:.1f}% | P&L: ${sum(t['pnl'] for t in trades):+.2f}")

if __name__ == "__main__":
    main()
