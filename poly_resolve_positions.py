"""
poly_resolve_positions.py

Checks Polymarket's Gamma API for resolution on any open/placeholder
paper position and settles it.

*** UNTESTED AGAINST LIVE POLYMARKET API — needs a live run on Mike's
    machine before it's trusted. Built from Gamma API docs/field names,
    not verified against a real response. ***

Resolution check: GET https://gamma-api.polymarket.com/markets?condition_ids={condition_id}
Looks at outcomePrices (e.g. ["1", "0"] once settled) rather than trusting
the `closed` flag alone — Gamma is known to lag reality on `closed` for a
while after actual resolution (see Polymarket/rs-clob-client#199). A
market is treated as resolved once outcomePrices contains a value that
rounds to 0 or 1 (i.e. no longer trading near 0.5-ish uncertainty).

Settlement math (simplified prediction-market payout model):
  - backfill_no_position: always settles to $0 P&L, balance untouched.
  - real position, direction correct (YES resolved & direction=="YES",
    or NO resolved & direction=="NO"): payout = size_usd / entry_price,
    pnl = payout - size_usd.
  - real position, direction wrong: pnl = -size_usd (lose full stake).
  Balance and realized_pnl in paper_balance.json are updated by the sum
  of pnl_usd across everything settled in this run.

Only checks positions whose end_date has passed (no point hammering the
API before a market could plausibly have resolved).

Run: python poly_resolve_positions.py
"""

import json
import datetime as dt
from pathlib import Path

import requests

STATE_DIR = Path("poly_state")
POSITIONS_FILE = STATE_DIR / "paper_positions.json"
BALANCE_FILE = STATE_DIR / "paper_balance.json"

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")


def _is_past_end_date(end_date_raw, now) -> bool:
    if not end_date_raw:
        return False
    try:
        end = dt.datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return now >= end


def check_resolution(condition_id: str) -> str | None:
    """Returns 'YES', 'NO', or None (not resolved / lookup failed)."""
    if not condition_id:
        return None
    try:
        resp = requests.get(
            GAMMA_MARKETS_URL,
            params={"condition_ids": condition_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  lookup failed for {condition_id}: {e}")
        return None

    if not data:
        return None
    market = data[0] if isinstance(data, list) else data

    outcome_prices = market.get("outcomePrices")
    if not outcome_prices:
        return None
    try:
        prices = [float(p) for p in outcome_prices]
    except (TypeError, ValueError):
        return None

    # Resolved markets settle to (1, 0) or (0, 1). Give a little tolerance
    # for dust rather than requiring an exact 1.0/0.0.
    if prices[0] >= 0.99:
        return "YES"
    if len(prices) > 1 and prices[1] >= 0.99:
        return "NO"
    return None  # still trading, not resolved yet


def main():
    positions = _load_json(POSITIONS_FILE, [])
    balance_data = _load_json(BALANCE_FILE, {
        "balance": 1000.0, "starting_balance": 1000.0,
        "realized_pnl": 0.0, "last_updated": None,
    })

    now = dt.datetime.now(dt.timezone.utc)
    settled_count = 0
    total_pnl_this_run = 0.0

    for p in positions:
        if p["status"] not in ("open", "backfill_no_position"):
            continue
        if not _is_past_end_date(p.get("end_date"), now):
            continue

        if p["status"] == "backfill_no_position":
            # Don't even need an API call — always $0, just close it out.
            p["status"] = "resolved_no_position"
            p["resolved_at"] = now.isoformat()
            p["pnl_usd"] = 0.0
            settled_count += 1
            continue

        print(f"Checking resolution: {p['event_slug']}")
        outcome = check_resolution(p.get("condition_id"))
        if outcome is None:
            print("  not resolved yet, skipping")
            continue

        won = (outcome == p["direction"])
        if won:
            payout = p["size_usd"] / p["entry_price"]
            pnl = round(payout - p["size_usd"], 2)
        else:
            pnl = -p["size_usd"]

        p["status"] = "resolved_win" if won else "resolved_loss"
        p["resolved_at"] = now.isoformat()
        p["outcome"] = outcome
        p["pnl_usd"] = pnl

        total_pnl_this_run += pnl
        settled_count += 1
        print(f"  resolved {outcome}, position was {p['direction']} -> {'WIN' if won else 'LOSS'} (${pnl:+.2f})")

    balance_data["balance"] = round(balance_data["balance"] + total_pnl_this_run, 2)
    balance_data["realized_pnl"] = round(balance_data["realized_pnl"] + total_pnl_this_run, 2)
    balance_data["last_updated"] = now.isoformat()

    _save_json(POSITIONS_FILE, positions)
    _save_json(BALANCE_FILE, balance_data)

    print(f"\nSettled {settled_count} position(s) this run. Net P&L this run: ${total_pnl_this_run:+.2f}")
    print(f"Paper balance: ${balance_data['balance']:.2f} (started at ${balance_data['starting_balance']:.2f})")


if __name__ == "__main__":
    main()
