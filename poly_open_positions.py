"""
poly_open_positions.py

Simulated paper-trading position opener for the Polymarket bot.

For each forecast in poly_state/forecasts_log.jsonl that doesn't already
have a position record, this either:

  (a) records a zero-stake placeholder, if the forecast's event_id is in
      poly_state/backfill_skip_ids.json (the 35 forecasts logged before
      this feature existed — deliberately not backfilled with real
      stakes; they still get closed out at resolution time as $0 P&L
      so they don't linger forever), or

  (b) opens a real paper position, sized as a % of current balance,
      if the forecast has a reliable probability (probability_extraction_
      method == "explicit") and the edge vs. the market price clears
      EDGE_THRESHOLD.

Direction: BUY_YES if bot probability > market YES price (positive edge),
BUY_NO if bot probability < market YES price (negative edge). Entry price
is the market price already captured at forecast time — no live re-fetch
needed to open.

Sizing: SIZE_PCT of current paper balance per position (dynamic —
mirrors bybit_sim.py's compounding position sizing). Balance itself is
NOT touched here; it only changes when poly_resolve_positions.py settles
a position. Running this script never moves money, only opens exposure.

Idempotent / safe to re-run: skips any event_id that already has a
position record, so re-running after a new batch only opens positions
for the newly-added forecasts.

Run: python poly_open_positions.py
"""

import json
from pathlib import Path

STATE_DIR = Path("poly_state")
FORECASTS_LOG_FILE = STATE_DIR / "forecasts_log.jsonl"
POSITIONS_FILE = STATE_DIR / "paper_positions.json"
BALANCE_FILE = STATE_DIR / "paper_balance.json"
SKIP_IDS_FILE = STATE_DIR / "backfill_skip_ids.json"

EDGE_THRESHOLD = 0.05   # minimum |bot probability - market price| to open a position
SIZE_PCT = 0.01         # 1% of current paper balance per position


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")


def _market_price(record: dict) -> float | None:
    prices = record.get("market_price_at_forecast")
    if not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError, IndexError):
        return None


def main():
    forecasts = _load_jsonl(FORECASTS_LOG_FILE)
    positions = _load_json(POSITIONS_FILE, [])
    balance_data = _load_json(BALANCE_FILE, {
        "balance": 1000.0, "starting_balance": 1000.0,
        "realized_pnl": 0.0, "last_updated": None,
    })
    skip_ids = set(_load_json(SKIP_IDS_FILE, []))

    existing_event_ids = {p["event_id"] for p in positions}
    balance = balance_data["balance"]

    opened = 0
    placeholders = 0
    skipped_low_edge = 0
    skipped_unreliable = 0

    for r in forecasts:
        event_id = r.get("event_id")
        if event_id is None or event_id in existing_event_ids:
            continue

        market_price = _market_price(r)
        probability = r.get("estimated_probability")

        if event_id in skip_ids:
            positions.append({
                "event_id": event_id,
                "event_slug": r.get("event_slug", "?"),
                "question": r.get("question", ""),
                "condition_id": r.get("condition_id", ""),
                "direction": None,
                "entry_price": market_price,
                "size_usd": 0.0,
                "edge_at_entry": None,
                "opened_at": r.get("timestamp", ""),
                "end_date": r.get("end_date"),
                "status": "backfill_no_position",
                "resolved_at": None,
                "outcome": None,
                "pnl_usd": 0.0,
            })
            placeholders += 1
            continue

        if r.get("probability_extraction_method") != "explicit":
            skipped_unreliable += 1
            continue

        if market_price is None or probability is None:
            continue

        edge = round(probability - market_price, 4)
        if abs(edge) < EDGE_THRESHOLD:
            skipped_low_edge += 1
            continue

        direction = "YES" if edge > 0 else "NO"
        entry_price = market_price if direction == "YES" else round(1 - market_price, 4)
        size_usd = round(balance * SIZE_PCT, 2)

        positions.append({
            "event_id": event_id,
            "event_slug": r.get("event_slug", "?"),
            "question": r.get("question", ""),
            "condition_id": r.get("condition_id", ""),
            "direction": direction,
            "entry_price": entry_price,
            "size_usd": size_usd,
            "edge_at_entry": edge,
            "opened_at": r.get("timestamp", ""),
            "end_date": r.get("end_date"),
            "status": "open",
            "resolved_at": None,
            "outcome": None,
            "pnl_usd": None,
        })
        opened += 1

    _save_json(POSITIONS_FILE, positions)
    _save_json(BALANCE_FILE, balance_data)  # unchanged here — balance only moves on resolution

    print(f"Opened {opened} new position(s).")
    print(f"Recorded {placeholders} backfill placeholder(s) (pre-existing forecasts, $0 stake).")
    print(f"Skipped {skipped_unreliable} forecast(s) with unreliable probability extraction.")
    print(f"Skipped {skipped_low_edge} forecast(s) below the {EDGE_THRESHOLD} edge threshold.")
    print(f"Paper balance: ${balance:.2f} (unchanged — only resolution moves the balance)")


if __name__ == "__main__":
    main()
