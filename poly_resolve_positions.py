"""
poly_resolve_positions.py

Checks Polymarket's Gamma API for resolution on any open/placeholder
paper position and settles it.

*** BUG FOUND AND FIXED 2026-07-25 — see check_resolution(). Gamma
    double-encodes outcomePrices as a JSON STRING, not an actual array.
    The original parsing code iterated over that string's CHARACTERS,
    which raised ValueError almost immediately and got silently caught
    — meaning EVERY resolution check silently returned "not resolved
    yet" regardless of the real outcome, for as long as this script has
    existed. A prior claim that this had been "confirmed working live"
    was WRONG — the 2 positions that appeared to settle successfully in
    that run were backfill placeholders, which settle via a separate
    $0-no-API-call path (see main()) and never actually exercised the
    buggy code. The genuine Gamma-API-dependent resolution path had
    never worked correctly until this fix, despite running nightly for
    over a week. Confirmed via: 35 positions stuck open days past their
    end_date with zero resolutions logged. ***

Resolution check: GET https://gamma-api.polymarket.com/markets?condition_ids={condition_id}
Looks at outcomePrices (a JSON-encoded string like '["1", "0"]' once
settled — NOT a real array, see check_resolution()) rather than trusting
the `closed` flag alone — Gamma is known to lag reality on `closed` for a
while after actual resolution (see Polymarket/rs-clob-client#199). A
market is treated as resolved once outcomePrices contains a value that
rounds to 0 or 1 (i.e. no longer trading near 0.5-ish uncertainty).

Settlement math (simplified prediction-market payout model):
  - backfill_no_position / backfill_no_edge: always settles to $0 P&L,
    balance untouched (the latter is a placeholder that WAS re-evaluated
    on a refresh but still didn't clear the edge threshold — same $0
    settlement either way).
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


def check_resolution(condition_id: str, debug: bool = True) -> str | None:
    """Returns 'YES', 'NO', or None (not resolved / lookup failed).

    debug=True (2026-07-25, per Mike): after the outcomePrices double-
    encoding fix still produced zero resolutions on the very next run —
    including markets 5+ days past end_date that would almost certainly
    have resolved in reality — this prints enough raw detail to tell,
    definitively, whether the failure is (a) Gamma returning no matching
    market for this condition_id at all, (b) a response shape this code
    still doesn't handle, or (c) something else. No more guessing blind.
    Safe to flip back to False once diagnosed — this is deliberately
    temporary, verbose instrumentation, not a permanent feature.
    """
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
        print(f"  lookup failed for {condition_id}: {e}", flush=True)
        return None

    if debug:
        print(f"  [debug] condition_id={condition_id}", flush=True)
        print(f"  [debug] response type={type(data).__name__}, "
              f"length={len(data) if hasattr(data, '__len__') else 'n/a'}", flush=True)
        if data:
            preview = data[0] if isinstance(data, list) else data
            print(f"  [debug] first market keys: {sorted(preview.keys()) if isinstance(preview, dict) else preview}", flush=True)
            raw_op = preview.get("outcomePrices") if isinstance(preview, dict) else None
            print(f"  [debug] outcomePrices raw value={raw_op!r}, type={type(raw_op).__name__}", flush=True)
            print(f"  [debug] closed={preview.get('closed')}, active={preview.get('active')}, "
                  f"umaResolutionStatus={preview.get('umaResolutionStatus')}", flush=True)

    if not data:
        return None
    market = data[0] if isinstance(data, list) else data

    outcome_prices = market.get("outcomePrices")
    if not outcome_prices:
        return None

    # Gamma double-encodes outcomePrices as a JSON STRING (e.g. '["0.02", "0.98"]'),
    # not an actual array — confirmed 2026-07-25 after this exact bug was found in
    # production: iterating over a raw string with `for p in outcome_prices` walks
    # its CHARACTERS ('[', '"', '0', ...), and float() on each one raises ValueError
    # almost immediately, which the except block below silently swallows. The net
    # effect: EVERY resolution check silently returned "not resolved yet" regardless
    # of the real outcome — confirmed as the root cause of 35 positions stuck past
    # their end_date with zero resolutions across nearly a week of nightly runs.
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except json.JSONDecodeError:
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
        if p["status"] not in ("open", "backfill_no_position", "backfill_no_edge"):
            continue
        if not _is_past_end_date(p.get("end_date"), now):
            continue

        if p["status"] in ("backfill_no_position", "backfill_no_edge"):
            # Don't even need an API call — always $0, just close it out.
            p["status"] = "resolved_no_position"
            p["resolved_at"] = now.isoformat()
            p["pnl_usd"] = 0.0
            settled_count += 1
            continue

        print(f"Checking resolution: {p['event_slug']}", flush=True)
        outcome = check_resolution(p.get("condition_id"))
        if outcome is None:
            print("  not resolved yet, skipping", flush=True)
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