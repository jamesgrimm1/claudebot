#!/usr/bin/env python3
"""
investor_sync.py
Reads nobot_v3_log.json, finds newly resolved trades,
and writes per-investor transactions to Supabase.

Run after nobot_v3.py in the GitHub Actions workflow.
Requires: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.
"""

import json
import os
import requests
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")  # service role key — full access
LOG_FILE     = "nobot_v3_log.json"

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def sb_get(path, params=None):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
        },
        params=params,
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def sb_post(path, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
        json=data,
        timeout=10
    )
    r.raise_for_status()

def sb_patch(path, data, match_params):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
        params=match_params,
        json=data,
        timeout=10
    )
    r.raise_for_status()

def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("SUPABASE_URL or SUPABASE_SERVICE_KEY not set — skipping investor sync")
        return

    # Load the V3 log
    if not os.path.exists(LOG_FILE):
        log(f"{LOG_FILE} not found — skipping")
        return

    with open(LOG_FILE) as f:
        v3 = json.load(f)

    closed_trades = [t for t in v3.get("trades", []) if t.get("status") == "closed"]
    if not closed_trades:
        log("No closed trades in V3 log")
        return

    # Get already-processed trade IDs
    processed = sb_get("processed_trades", {"select": "trade_id"})
    processed_ids = {p["trade_id"] for p in processed}

    # Find new trades
    new_trades = [t for t in closed_trades if t.get("id") not in processed_ids]
    if not new_trades:
        log("No new trades to process")
        return

    log(f"Processing {len(new_trades)} new resolved trade(s)")

    # Get all active investors
    investors = sb_get("investors", {"select": "*", "active": "eq.true"})
    if not investors:
        log("No active investors found")
        return

    log(f"Found {len(investors)} active investor(s)")

    for trade in new_trades:
        trade_id    = trade.get("id")
        won         = trade.get("won", False)
        realized    = trade.get("realized_pnl", 0.0)
        market      = trade.get("market", "")[:120]
        resolved_at = trade.get("resolved_at", datetime.now(timezone.utc).isoformat())

        log(f"  Trade: {market[:50]} | {'WON' if won else 'LOST'} | P&L: ${realized:+.2f}")

        for inv in investors:
            inv_id      = inv["id"]
            share_pct   = float(inv["pool_share_pct"])   # e.g. 0.10 for 10%
            comm_pct    = float(inv["commission_pct"])    # e.g. 15.00 for 15%
            bal_before  = float(inv["current_balance"])

            # Their gross share of this trade's P&L
            gross_pnl = round(realized * share_pct, 4)

            # Fee: only on winning trades, only on profit portion
            fee = 0.0
            if won and gross_pnl > 0:
                fee = round(gross_pnl * (comm_pct / 100), 4)

            net_pnl     = round(gross_pnl - fee, 4)
            bal_after   = round(bal_before + net_pnl, 4)

            # Write transaction
            sb_post("transactions", {
                "investor_id":   inv_id,
                "trade_id":      trade_id,
                "market":        market,
                "resolved_at":   resolved_at,
                "won":           won,
                "gross_pnl":     gross_pnl,
                "fee_charged":   fee,
                "net_pnl":       net_pnl,
                "balance_before": bal_before,
                "balance_after":  bal_after,
            })

            # Update investor current_balance and total_fees_paid
            sb_patch("investors", {
                "current_balance": bal_after,
                "total_fees_paid": round(float(inv.get("total_fees_paid", 0)) + fee, 4),
            }, {"id": f"eq.{inv_id}"})

            # Update local copy so next trade in this batch uses updated balance
            inv["current_balance"] = bal_after
            inv["total_fees_paid"] = round(float(inv.get("total_fees_paid", 0)) + fee, 4)

            log(f"    {inv['name']}: gross=${gross_pnl:+.2f} fee=${fee:.2f} net=${net_pnl:+.2f} bal=${bal_after:.2f}")

        # Mark trade as processed
        sb_post("processed_trades", {"trade_id": trade_id})

    log("Investor sync complete")

if __name__ == "__main__":
    main()
