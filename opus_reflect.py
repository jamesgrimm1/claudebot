"""
╔══════════════════════════════════════════════════════════╗
║  OPUS REFLECT — ClaudeBot Self-Reflection Engine         ║
║                                                          ║
║  Runs every 48 hours (triggered by claudebot_v2.py)      ║
║  Reads trade_reflections_v2/*.md                         ║
║  Opus analyzes patterns → writes graphify-out/GRAPH_REPORT.md ║
║  That report gets injected into every Opus scan prompt   ║
║                                                          ║
║  RUN: python opus_reflect.py                             ║
╚══════════════════════════════════════════════════════════╝
"""

import os, sys, json, glob, anthropic
from datetime import datetime, timezone

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
REFLECTIONS_DIR    = "trade_reflections_v2"
GRAPH_REPORT_FILE  = "graphify-out/GRAPH_REPORT.md"
CLAUDEBOT_LOG      = "claudebot_log.json"
OPUS_MODEL         = "claude-opus-4-5"
MAX_REFLECTIONS    = 60   # cap to avoid token overload


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def should_reflect(state):
    """Return True if 48+ hours since last reflection."""
    last = state.get("last_reflection_utc", "")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        hours_elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        log(f"  Last reflection: {hours_elapsed:.1f}h ago (threshold: 48h)")
        return hours_elapsed >= 48
    except:
        return True


def load_reflections():
    """Load recent trade reflection markdown files."""
    if not os.path.exists(REFLECTIONS_DIR):
        return []
    files = sorted(glob.glob(f"{REFLECTIONS_DIR}/*.md"), key=os.path.getmtime, reverse=True)
    files = files[:MAX_REFLECTIONS]
    reflections = []
    for f in files:
        try:
            with open(f) as fh:
                reflections.append(fh.read())
        except:
            pass
    log(f"  Loaded {len(reflections)} reflection files")
    return reflections


def load_recent_stats(state):
    """Compute quick stats from the log to give Opus context."""
    trades = state.get("trades", [])
    closed = [t for t in trades if t["status"] == "closed"]
    if not closed:
        return "No closed trades yet."

    won = [t for t in closed if t.get("won")]
    pnl = sum(t.get("realized_pnl", 0) for t in closed)
    wr  = len(won) / len(closed) * 100

    # By category
    cats = {}
    for t in closed:
        c = t.get("category", "other")
        if c not in cats:
            cats[c] = {"w": 0, "l": 0, "pnl": 0}
        if t.get("won"):
            cats[c]["w"] += 1
        else:
            cats[c]["l"] += 1
        cats[c]["pnl"] += t.get("realized_pnl", 0)

    # By confidence band
    bands = {}
    for t in closed:
        conf = t.get("confidence", 0)
        if isinstance(conf, (int, float)):
            band = f"{(int(conf)//10)*10}-{(int(conf)//10)*10+9}%"
            if band not in bands:
                bands[band] = {"w": 0, "l": 0}
            if t.get("won"):
                bands[band]["w"] += 1
            else:
                bands[band]["l"] += 1

    lines = [
        f"OVERALL: {len(closed)} closed | {len(won)}W/{len(closed)-len(won)}L | {wr:.1f}% WR | ${pnl:+.2f} P&L",
        f"Bankroll: ${state.get('bankroll', 0):.2f}",
        "",
        "BY CATEGORY:",
    ]
    for cat, v in sorted(cats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        n = v["w"] + v["l"]
        wr_c = v["w"]/n*100 if n else 0
        lines.append(f"  {cat:<15} {v['w']}W/{v['l']}L {wr_c:.0f}% WR ${v['pnl']:+.2f}")

    lines += ["", "BY CONFIDENCE BAND:"]
    for band, v in sorted(bands.items()):
        n = v["w"] + v["l"]
        wr_b = v["w"]/n*100 if n else 0
        lines.append(f"  {band:<12} {v['w']}W/{v['l']}L {wr_b:.0f}% WR")

    return "\n".join(lines)


def run_opus_reflection(reflections, stats, client):
    """Call Opus to analyze trade history and output a structured report."""
    log("  🧠 Calling Opus for reflection analysis...")

    reflections_text = "\n\n---\n\n".join(reflections[:MAX_REFLECTIONS])

    prompt = f"""You are the meta-learning brain for an autonomous Polymarket trading bot called ClaudeBot.

Your job is to analyze the recent trade history and extract actionable patterns that will improve future trading decisions. Be brutally honest — identify what is working, what is not, and give concrete guidance.

## Current Performance Stats
{stats}

## Recent Trade Reflections
{reflections_text[:15000]}

## Your Task

Write a structured GRAPH_REPORT.md that will be injected into every future Opus scan prompt. This report should make Opus smarter on the next trade. Be specific and concrete — not generic advice.

Output ONLY the report in this exact format:

---
# CLAUDEBOT KNOWLEDGE GRAPH — AUTO-GENERATED {datetime.now().strftime('%Y-%m-%d')}

## Overall Assessment
[2-3 sentences on current performance trajectory and biggest strengths/weaknesses]

## WORKING PATTERNS (reinforce these)
- [specific pattern that is generating edge — category, timing, position type, confidence level]
- [repeat for each confirmed working pattern]

## FAILING PATTERNS (avoid these)
- [specific pattern that is bleeding money — be concrete about why]
- [repeat for each confirmed failing pattern]

## CONFIDENCE CALIBRATION
- [Which confidence bands are well-calibrated vs overconfident?]
- [Specific guidance: e.g. "85-90% claimed conf has 43% actual WR — treat as 60% when sizing"]

## CATEGORY GUIDANCE
- [Category-by-category verdict: STRONG EDGE / MARGINAL / AVOID]

## TIMING INSIGHTS
- [Sub-day trades: performance?]
- [Multi-day trades: performance?]
- [Any timing patterns worth exploiting?]

## NEXT SCAN PRIORITIES
- [Top 3 things Opus should be on the lookout for in the next scan]

## RED FLAGS TO AVOID
- [Specific market types or situations that have repeatedly lost — hard rules]
---"""

    resp = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()


def write_graph_report(report_text):
    """Write the Opus-generated report to graphify-out/GRAPH_REPORT.md."""
    os.makedirs("graphify-out", exist_ok=True)
    with open(GRAPH_REPORT_FILE, "w") as f:
        f.write(report_text)
    log(f"  ✅ Graph report written ({len(report_text)} chars)")


def main():
    now = datetime.now(timezone.utc)
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  OPUS REFLECT  ·  ClaudeBot Self-Reflection Engine       ║")
    print(f"║  {now.strftime('%Y-%m-%d %H:%M UTC')}                                  ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Load state to check timing and get stats
    state = {}
    if os.path.exists(CLAUDEBOT_LOG):
        with open(CLAUDEBOT_LOG) as f:
            state = json.load(f)

    if not should_reflect(state):
        log("⏭  Reflection not due yet (< 48h since last) — skipping")
        return

    log("── Step 1: Load trade reflections ───────────────────────")
    reflections = load_reflections()

    if len(reflections) < 3:
        log(f"⏭  Only {len(reflections)} reflections — need at least 3 to analyze")
        return

    log("── Step 2: Compute performance stats ────────────────────")
    stats = load_recent_stats(state)
    log(f"  Stats:\n{stats}")

    log("── Step 3: Opus reflection analysis ─────────────────────")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    report = run_opus_reflection(reflections, stats, client)

    log("── Step 4: Write graph report ────────────────────────────")
    write_graph_report(report)

    # Update last_reflection timestamp in state
    state["last_reflection_utc"] = now.isoformat()
    if os.path.exists(CLAUDEBOT_LOG):
        with open(CLAUDEBOT_LOG, "w") as f:
            json.dump(state, f, indent=2)
        log("  📅 Updated last_reflection_utc in state")

    log("\n✅ Reflection complete. Graph report will be injected into next scan.")
    print()
    print(report[:500] + "..." if len(report) > 500 else report)


if __name__ == "__main__":
    main()
