"""
poly_open_positions.py

Simulated paper-trading position opener for the Polymarket bot.

For each event's MOST RECENT forecast in poly_state/forecasts_log.jsonl:

  (a) if the event has no position record yet AND is in
      poly_state/backfill_skip_ids.json (the original 35 forecasts logged
      before this feature existed), record a zero-stake placeholder — not
      backfilled with real stakes; closed out at resolution time as $0 P&L
      so it doesn't linger forever.

  (b) if the event already has a REAL (non-placeholder) position — open,
      resolved_win, resolved_loss — leave it alone entirely. Never touched,
      never duplicated.

  (c) if the event has a $0 PLACEHOLDER position AND has since been
      refreshed (a forecast exists with a timestamp newer than the
      placeholder's opened_at), UPGRADE it to a real position in place —
      this is what lets the legacy-20 refresh plan actually work. Bug fixed
      2026-07-20: the original version deduped on "does this event have ANY
      position at all," which silently froze every backfilled event at its
      $0 placeholder forever, even after a genuine refresh — confirmed in
      production when the Iran market's 2026-07-20 refresh produced a new
      probability (0.18) but the position record stayed untouched at the
      2026-07-16 placeholder. This also explains an earlier discrepancy
      report (11 forecasts in a run, only 10 accounted for across
      opened+skipped counters) — the silently-skipped event was never
      counted anywhere, same root cause.

  (d) otherwise (brand-new event, or a placeholder not yet refreshed):
      opens a real paper position, sized as a % of current balance, if the
      forecast has a reliable probability (probability_extraction_method
      == "explicit") and the edge vs. the market price clears EDGE_THRESHOLD.

Direction: BUY_YES if bot probability > market YES price (positive edge),
BUY_NO if bot probability < market YES price (negative edge). Entry price
is the market price already captured at forecast time — no live re-fetch
needed to open.

Sizing: SIZE_PCT of current paper balance per position (dynamic —
mirrors bybit_sim.py's compounding position sizing). Balance itself is
NOT touched here; it only changes when poly_resolve_positions.py settles
a position. Running this script never moves money, only opens exposure.

Idempotent / safe to re-run: an event's position is only created or
upgraded once per distinct forecast timestamp — re-running without new
forecasts changes nothing.

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

    # Collapse to the most recent forecast per event_id — a refresh
    # supersedes the original for position-opening purposes.
    latest_by_event: dict[str, dict] = {}
    for r in forecasts:
        eid = r.get("event_id")
        if eid is None:
            continue
        existing = latest_by_event.get(eid)
        if existing is None or r.get("timestamp", "") > existing.get("timestamp", ""):
            latest_by_event[eid] = r

    positions_by_event = {p["event_id"]: p for p in positions}
    balance = balance_data["balance"]

    opened = 0
    upgraded = 0
    placeholders = 0
    skipped_low_edge = 0
    skipped_unreliable = 0
    skipped_already_real = 0

    for event_id, r in latest_by_event.items():
        existing_position = positions_by_event.get(event_id)

        # Real position already exists (open or resolved) — never touch it.
        if existing_position is not None and existing_position["status"] != "backfill_no_position":
            skipped_already_real += 1
            continue

        is_stale_placeholder = (
            existing_position is not None
            and existing_position["status"] == "backfill_no_position"
            and r.get("timestamp", "") > existing_position.get("opened_at", "")
        )

        # First time seeing this event, it's a pre-existing (skip-list)
        # forecast, and no position exists yet at all — record the $0
        # placeholder. (If a placeholder already exists and this forecast
        # ISN'T newer than it, there's genuinely nothing new to do — falls
        # through to the market_price/probability checks below, which for
        # an unchanged forecast will just re-derive the same answer as
        # last time and get correctly skipped or re-opened identically.)
        if existing_position is None and event_id in skip_ids:
            positions.append({
                "event_id": event_id,
                "event_slug": r.get("event_slug", "?"),
                "question": r.get("question", ""),
                "condition_id": r.get("condition_id", ""),
                "direction": None,
                "entry_price": _market_price(r),
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

        # A placeholder exists and hasn't been refreshed since it was
        # recorded — nothing to do yet.
        if existing_position is not None and not is_stale_placeholder:
            continue

        # From here: either a brand-new (never-forecast, not skip-listed)
        # event, or a placeholder eligible for upgrade via a genuine refresh.
        market_price = _market_price(r)
        probability = r.get("estimated_probability")

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

        new_record = {
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
        }

        if is_stale_placeholder:
            idx = positions.index(existing_position)
            positions[idx] = new_record
            upgraded += 1
        else:
            positions.append(new_record)
            opened += 1

    _save_json(POSITIONS_FILE, positions)
    _save_json(BALANCE_FILE, balance_data)  # unchanged here — balance only moves on resolution

    print(f"Opened {opened} new position(s).")
    print(f"Upgraded {upgraded} placeholder(s) to real position(s) via refresh.")
    print(f"Recorded {placeholders} new backfill placeholder(s) (pre-existing forecasts, $0 stake).")
    print(f"Left {skipped_already_real} event(s) untouched (already have a real position).")
    print(f"Skipped {skipped_unreliable} forecast(s) with unreliable probability extraction.")
    print(f"Skipped {skipped_low_edge} forecast(s) below the {EDGE_THRESHOLD} edge threshold.")
    print(f"Paper balance: ${balance:.2f} (unchanged — only resolution moves the balance)")


if __name__ == "__main__":
    main()