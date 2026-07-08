"""
poly_discovery.py

Discovery / coverage / ID-mapping layer for the Polymarket paper-trading bot.

Responsibilities (kept deliberately narrow — this module does NOT forecast,
trade, or price anything; it just answers "what markets exist and which of
them do we track"):

  1. Page through Gamma API /events (active, not closed) and flatten to markets.
  2. Build a canonical ID record per market, keyed on conditionId (stable
     across the market's on-chain lifecycle), carrying every other ID
     (event id, market id, questionID, slug, clobTokenIds) as attributes.
     Reasoning: Metaculus post_id/question_id confusion cost real backfill
     work; here there are FIVE identifiers instead of two, so the mapping
     is captured up front rather than discovered under pressure later.
  3. Classify each market: single-market (binary) event vs. multi-market
     (categorical/negRisk) event — the Polymarket analogue of Metaculus
     binary vs. MultipleChoiceQuestion/group questions.
  4. Apply a coverage decision: broad floor on liquidity/volume, OR flagged
     as priority via tag (crypto / politics / economy by default).
  5. Run basic data-quality diagnostics (missing clobTokenIds, negRisk
     groups whose prices don't sum to ~1) — the Polymarket analogue of
     meta_coverage_check.py's gated-vs-real-gap classification.
  6. Persist a canonical coverage.json + a coverage_report.json diagnostic
     summary. No trading, no forecasting — that's the next layer.

No API key / auth required for any of this (Gamma API is fully public read).
Rate limits (per Polymarket docs, July 2026): /events 500 req/10s, /markets
300 req/10s — this script pages at 500/request and sleeps briefly between
pages, nowhere close to the limit for a coverage run.

NOT YET LIVE-TESTED — sandbox network access doesn't reach gamma-api.polymarket.com.
Run this on your machine first and sanity-check the output before wiring it
into anything else (matches the "live production testing over simulation"
pattern from the Metaculus bot).
"""

from __future__ import annotations

import json
import time
import argparse
import datetime as dt
from pathlib import Path
from dataclasses import dataclass, field, asdict

import requests

# ---------------------------------------------------------------------------
# Config — tweak these as coverage needs change. Nothing else in the script
# should need editing to adjust scope.
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Hybrid coverage rule: include a market if it's above the floor OR belongs
# to an event carrying one of these priority tag slugs (checked regardless
# of floor). Adjust freely once you see what's actually flowing through.
PRIORITY_TAG_SLUGS = {"crypto", "politics", "economy"}
COVERAGE_LIQUIDITY_MIN = 1000.0
COVERAGE_VOLUME_MIN = 1000.0

PAGE_LIMIT = 500  # matches Gamma's practical page size; rate limit is 500 req/10s on /events
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
SLEEP_BETWEEN_PAGES = 0.25

STATE_DIR = Path("poly_state")
COVERAGE_FILE = STATE_DIR / "coverage.json"
REPORT_FILE = STATE_DIR / "coverage_report.json"

NEG_RISK_SUM_TOLERANCE = 0.05  # flag if group's lead-outcome prices sum outside 1 +/- this


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(session: requests.Session, path: str, params: dict) -> list[dict]:
    url = f"{GAMMA_BASE}{path}"
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"  [rate limited] sleeping {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  [request error] {exc} — retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    raise RuntimeError(f"Failed GET {url} after {MAX_RETRIES} attempts") from last_exc


def fetch_all_events(session: requests.Session, active: bool = True, closed: bool = False) -> list[dict]:
    """Page through /events until a short page signals we've hit the end."""
    events: list[dict] = []
    offset = 0
    while True:
        params = {
            "limit": PAGE_LIMIT,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        page = _get(session, "/events", params)
        if not page:
            break
        events.extend(page)
        print(f"  fetched {len(page)} events (offset={offset}, total so far={len(events)})")
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(SLEEP_BETWEEN_PAGES)
    return events


# ---------------------------------------------------------------------------
# Parsing helpers — several Gamma fields are JSON-encoded as strings
# (outcomes, outcomePrices, clobTokenIds) rather than native arrays.
# ---------------------------------------------------------------------------

def _parse_json_field(raw, default):
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _to_float(raw, default=0.0):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Canonical record
# ---------------------------------------------------------------------------

@dataclass
class MarketRecord:
    # canonical key
    condition_id: str

    # every other identifier, kept as attributes rather than discarded
    market_id: str
    question_id: str | None
    market_slug: str | None
    event_id: str
    event_slug: str | None
    clob_token_ids: list[str]

    # content
    question: str | None
    event_title: str | None
    tags: list[str]

    # typing — Polymarket analogue of Metaculus binary vs. group/MC questions
    is_group_member: bool          # event has >1 market (categorical/negRisk-style)
    neg_risk: bool
    outcomes: list[str]

    # market state
    active: bool
    closed: bool
    accepting_orders: bool | None
    enable_order_book: bool | None
    end_date: str | None
    uma_resolution_status: str | None

    # sizing signals used for coverage decision
    liquidity: float
    volume: float

    # coverage
    priority: bool
    covered: bool
    coverage_reason: str

    # live implied probability snapshot (Metaculus CP analogue — not time-gated here)
    outcome_prices: list[str]

    fetched_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


def normalize_market(event: dict, market: dict, event_market_count: int) -> MarketRecord | None:
    condition_id = market.get("conditionId")
    if not condition_id:
        return None  # can't build a canonical record without the canonical key

    tags = [t.get("slug") for t in (event.get("tags") or []) if t.get("slug")]
    priority = bool(set(tags) & PRIORITY_TAG_SLUGS)

    liquidity = _to_float(market.get("liquidity"))
    volume = _to_float(market.get("volume"))

    meets_floor = liquidity >= COVERAGE_LIQUIDITY_MIN or volume >= COVERAGE_VOLUME_MIN
    covered = meets_floor or priority
    if priority and not meets_floor:
        reason = "priority_tag_below_floor"
    elif priority and meets_floor:
        reason = "priority_tag_and_floor"
    elif meets_floor:
        reason = "floor"
    else:
        reason = "excluded"

    return MarketRecord(
        condition_id=condition_id,
        market_id=str(market.get("id")),
        question_id=market.get("questionID"),
        market_slug=market.get("slug"),
        event_id=str(event.get("id")),
        event_slug=event.get("slug"),
        clob_token_ids=_parse_json_field(market.get("clobTokenIds"), []),
        question=market.get("question"),
        event_title=event.get("title"),
        tags=tags,
        is_group_member=event_market_count > 1,
        neg_risk=bool(event.get("negRisk")),
        outcomes=_parse_json_field(market.get("outcomes"), []),
        active=bool(market.get("active")),
        closed=bool(market.get("closed")),
        accepting_orders=market.get("acceptingOrders"),
        enable_order_book=market.get("enableOrderBook"),
        end_date=market.get("endDate"),
        uma_resolution_status=market.get("umaResolutionStatus"),
        liquidity=liquidity,
        volume=volume,
        priority=priority,
        covered=covered,
        coverage_reason=reason,
        outcome_prices=_parse_json_field(market.get("outcomePrices"), []),
    )


# ---------------------------------------------------------------------------
# Diagnostics — mirrors meta_coverage_check.py's gap classification
# ---------------------------------------------------------------------------

def run_diagnostics(records: list[MarketRecord], events: list[dict]) -> dict:
    missing_clob_tokens = [r.condition_id for r in records if not r.clob_token_ids]
    missing_outcomes = [r.condition_id for r in records if not r.outcomes]

    neg_risk_flags = []
    events_by_id = {str(e.get("id")): e for e in events}
    grouped: dict[str, list[MarketRecord]] = {}
    for r in records:
        if r.neg_risk and r.is_group_member:
            grouped.setdefault(r.event_id, []).append(r)
    for event_id, group in grouped.items():
        total = 0.0
        ok = True
        for r in group:
            if not r.outcome_prices:
                ok = False
                break
            total += _to_float(r.outcome_prices[0], 0.0)
        if ok and abs(total - 1.0) > NEG_RISK_SUM_TOLERANCE:
            neg_risk_flags.append({
                "event_id": event_id,
                "event_slug": events_by_id.get(event_id, {}).get("slug"),
                "market_count": len(group),
                "lead_outcome_price_sum": round(total, 4),
            })

    covered = [r for r in records if r.covered]
    priority_covered = [r for r in covered if r.priority]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total_events_fetched": len(events),
        "total_markets_seen": len(records),
        "markets_covered": len(covered),
        "markets_covered_via_priority_tag": len(priority_covered),
        "markets_excluded": len(records) - len(covered),
        "markets_missing_clob_token_ids": len(missing_clob_tokens),
        "markets_missing_outcomes": len(missing_outcomes),
        "missing_clob_token_id_sample": missing_clob_tokens[:20],
        "neg_risk_group_price_sum_flags": neg_risk_flags,
        "coverage_liquidity_min": COVERAGE_LIQUIDITY_MIN,
        "coverage_volume_min": COVERAGE_VOLUME_MIN,
        "priority_tag_slugs": sorted(PRIORITY_TAG_SLUGS),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "mike-poly-bot-discovery/0.1"})

    print("Fetching active, open events from Gamma API...")
    events = fetch_all_events(session, active=True, closed=False)
    print(f"Total events fetched: {len(events)}")

    records: list[MarketRecord] = []
    for event in events:
        markets = event.get("markets") or []
        for market in markets:
            rec = normalize_market(event, market, event_market_count=len(markets))
            if rec is not None:
                records.append(rec)
            else:
                print(f"  [skip] market with no conditionId in event {event.get('id')} ({event.get('slug')})")

    report = run_diagnostics(records, events)

    print("\n--- Coverage report ---")
    for k, v in report.items():
        if isinstance(v, list):
            print(f"{k}: {len(v)} item(s)")
        else:
            print(f"{k}: {v}")

    if dry_run:
        print("\n[dry-run] Not writing state files.")
        return

    coverage_out = {
        r.condition_id: asdict(r)
        for r in records
        if r.covered
    }
    COVERAGE_FILE.write_text(json.dumps(coverage_out, indent=2))
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    print(f"\nWrote {len(coverage_out)} covered markets to {COVERAGE_FILE}")
    print(f"Wrote diagnostics to {REPORT_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket discovery/coverage layer")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report but don't write state files")
    args = parser.parse_args()
    run(dry_run=args.dry_run)