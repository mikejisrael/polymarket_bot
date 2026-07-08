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

Pagination reality, confirmed empirically (2026-07-08), not from docs alone:
  - /events honors `limit` up to 100 per page regardless of what you request
    (asking for 500 silently returns 100).
  - Offset-based paging on /events hits a hard ceiling around offset=2100
    ("offset too large, use /events/keyset for deeper pagination" — a real
    422 from the API, not a guess).
  - /events/keyset is the documented replacement beyond that ceiling, but a
    reported bug (Polymarket/agents#227, 2026-04-27) has its cursor not
    advancing under some conditions. This script detects that symptom
    (duplicate page / static cursor) and stops cleanly rather than looping
    or double-counting — see fetch_events_keyset_continuation().

Bottom line: full coverage beyond ~2,100 open events is not guaranteed by
this script as of first-write. Check pagination.keyset_continuation in the
report each run — if cursor_bug_suspected is True, coverage is capped at
whatever offset pagination reached, and that's currently a known, logged
limitation rather than a silent gap.

NOT YET LIVE-TESTED — sandbox network access doesn't reach gamma-api.polymarket.com.
Run this on your machine first and sanity-check the output before wiring it
into anything else (matches the "live production testing over simulation"
pattern from the Metaculus bot).
"""

from __future__ import annotations

import json
import re
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

# Sports gets priority treatment too, but NOT via flat tag membership like the
# three above — that would priority-flag all ~52k sports-tagged markets,
# including the ~39k game-tied single-match props we specifically excluded
# from the negRisk check as non-research-suitable noise. Confirmed via real
# data (2026-07-08): futures-like sports (no match date in slug) skews long
# horizon (58% are 14+ days out, e.g. World Cup winner markets) and IS a good
# fit for LLM research; game-tied sports skews same-day and isn't. So sports
# priority is gated on slug_looks_game_tied being False — see normalize_market.
SPORTS_TAG_SLUGS = {"sports", "soccer", "esports", "cricket", "nba", "nfl", "nhl", "mlb", "mma"}
COVERAGE_LIQUIDITY_MIN = 1000.0
COVERAGE_VOLUME_MIN = 1000.0

# Hard-excluded regardless of floor/priority — these tags identify markets
# structurally unsuited to LLM-research-based forecasting, not just "low volume."
# up-or-down/5M/15M/30M/1H/1D: recurring ultra-short-interval crypto coin-flip
# markets. There's no research edge on "will BTC be up in 5 minutes" — these
# would just flood coverage with noise. hide-from-new: Polymarket's OWN signal
# that a market is excluded from their New listing — trusted as a general
# noise flag regardless of category, not just crypto.
NOISE_TAG_SLUGS = {"up-or-down", "5M", "15M", "30M", "1H", "1D", "hide-from-new"}

# Sports horizon buckets (days until endDate) — NOT used to exclude anything.
# Mike's hypothesis is that some sports markets (season-long futures) suit
# research-based forecasting while single-game props don't. Rather than guess
# a cutoff, we bucket and report so the real distribution can inform the call.
SPORTS_HORIZON_BUCKETS = [
    ("same_day_or_past", 1),
    ("1_to_3_days", 3),
    ("3_to_14_days", 14),
    ("14_plus_days", None),
]

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

class PaginationBoundaryError(Exception):
    """Raised when the API rejects a request with a 4xx that isn't a rate limit —
    most often a deep-offset pagination ceiling. Not worth retrying: the answer
    will be identical on attempt 2 and 3."""
    def __init__(self, status_code: int, body_text: str, url: str):
        self.status_code = status_code
        self.body_text = body_text
        self.url = url
        super().__init__(f"HTTP {status_code} for {url}: {body_text[:500]}")


def _get(session: requests.Session, path: str, params: dict) -> list[dict] | dict:
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
            if 400 <= resp.status_code < 500:
                # Deterministic client error — retrying won't change the response.
                # Surface the real body instead of a generic "HTTPError" message.
                raise PaginationBoundaryError(resp.status_code, resp.text, resp.url)
            resp.raise_for_status()
            return resp.json()
        except PaginationBoundaryError:
            raise  # not retryable, propagate immediately
        except requests.RequestException as exc:
            last_exc = exc
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  [request error] {exc} — retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    raise RuntimeError(f"Failed GET {url} after {MAX_RETRIES} attempts") from last_exc


def fetch_events_keyset_continuation(
    session: requests.Session,
    already_seen_event_ids: set[str],
    active: bool = True,
    closed: bool = False,
    max_pages: int = 100,
) -> tuple[list[dict], dict]:
    """Extend coverage past the offset ceiling using /events/keyset.

    Deduplicates against already_seen_event_ids (from the offset-based fetch)
    rather than trying to align keyset's cursor position with a specific
    offset — the two endpoints aren't guaranteed to share sort order, so
    "resume from offset 2100" isn't a coherent request against keyset.

    Defends against a known bug (Polymarket/agents#227, reported 2026-04-27):
    /events/keyset's cursor was observed to be silently ignored server-side,
    with page 2 returning identical data to page 1 instead of advancing. If
    that symptom appears (page N's event IDs == page N-1's event IDs, or
    next_cursor stops changing), we log it clearly and stop rather than loop
    forever or silently treat duplicate data as new coverage. Not assumed to
    still be broken as of this run — just checked for, defensively.
    """
    KEYSET_PAGE_LIMIT = 100  # hard max per Polymarket's 2026-05-14 changelog
    new_events: list[dict] = []
    meta = {
        "attempted": True,
        "pages_fetched": 0,
        "new_events_found": 0,
        "stopped_reason": None,
        "cursor_bug_suspected": False,
    }

    after_cursor = None
    prev_page_ids: list[str] | None = None
    prev_cursor: str | None = None

    for page_num in range(1, max_pages + 1):
        params = {
            "limit": KEYSET_PAGE_LIMIT,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        if after_cursor:
            params["after_cursor"] = after_cursor
        try:
            resp = _get(session, "/events/keyset", params)
        except PaginationBoundaryError as exc:
            print(f"  [keyset] page {page_num}: HTTP {exc.status_code} — {exc.body_text[:300]}")
            meta["stopped_reason"] = f"http_error_{exc.status_code}"
            break

        # Response wrapper per changelog: {"events": [...], "next_cursor": "..."}
        page_events = resp.get("events") if isinstance(resp, dict) else None
        next_cursor = resp.get("next_cursor") if isinstance(resp, dict) else None
        if page_events is None:
            print(f"  [keyset] page {page_num}: unexpected response shape (keys={list(resp.keys()) if isinstance(resp, dict) else type(resp)}) — stopping")
            meta["stopped_reason"] = "unexpected_response_shape"
            break

        page_ids = [str(e.get("id")) for e in page_events]

        # Bug check 1: identical event IDs to the immediately prior page
        if prev_page_ids is not None and page_ids == prev_page_ids and page_ids:
            print(f"  [keyset] page {page_num}: returned identical event IDs to page {page_num - 1} — "
                  f"cursor does not appear to be advancing (matches Polymarket/agents#227 symptom). "
                  f"Stopping keyset continuation here; coverage beyond this point is unverified.")
            meta["stopped_reason"] = "cursor_not_advancing_duplicate_page"
            meta["cursor_bug_suspected"] = True
            break

        # Bug check 2: next_cursor itself isn't changing
        if next_cursor is not None and next_cursor == prev_cursor:
            print(f"  [keyset] page {page_num}: next_cursor unchanged from previous page ('{next_cursor}') — "
                  f"treating as the same known cursor-not-advancing symptom. Stopping.")
            meta["stopped_reason"] = "cursor_not_advancing_static_token"
            meta["cursor_bug_suspected"] = True
            break

        if not page_events:
            print(f"  [keyset] page {page_num}: empty — stopping")
            meta["stopped_reason"] = "empty_page"
            break

        genuinely_new = [e for e in page_events if str(e.get("id")) not in already_seen_event_ids]
        for e in genuinely_new:
            already_seen_event_ids.add(str(e.get("id")))
        new_events.extend(genuinely_new)
        meta["pages_fetched"] = page_num
        print(f"  [keyset] page {page_num}: {len(page_events)} events, {len(genuinely_new)} new "
              f"(running new total={len(new_events)})")

        prev_page_ids = page_ids
        prev_cursor = next_cursor

        if not next_cursor or next_cursor == "LTE=":
            meta["stopped_reason"] = "no_next_cursor"
            break

        after_cursor = next_cursor
        time.sleep(SLEEP_BETWEEN_PAGES)
    else:
        meta["stopped_reason"] = "max_pages_safety"

    meta["new_events_found"] = len(new_events)
    return new_events, meta





MAX_PAGES_SAFETY = 200  # hard stop at 200 * observed page size, in case "empty page" never arrives


def fetch_all_events(session: requests.Session, active: bool = True, closed: bool = False) -> tuple[list[dict], dict]:
    """Page through /events until a genuinely empty page signals we've hit the end.

    Deliberately does NOT stop just because a page came back shorter than the
    requested `limit` — some APIs (this one, apparently) silently cap the
    effective page size below whatever you ask for. Requesting limit=500 and
    getting exactly 100 back on page 1 previously caused this loop to assume
    that was the whole dataset. Only an empty page (or the safety cap) ends it.

    Returns (events, pagination_meta) — pagination_meta records whether we hit
    a hard boundary (deep-offset 4xx) so downstream reporting can flag the
    results as a possible lower bound rather than silently treating them as complete.
    """
    events: list[dict] = []
    offset = 0
    observed_page_size = None
    pagination_meta = {"hit_boundary": False, "boundary_offset": None, "boundary_detail": None}
    for page_num in range(1, MAX_PAGES_SAFETY + 1):
        params = {
            "limit": PAGE_LIMIT,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        try:
            page = _get(session, "/events", params)
        except PaginationBoundaryError as exc:
            print(f"  page {page_num}: HTTP {exc.status_code} at offset={offset} — {exc.body_text[:500]}")
            if offset == 0:
                raise  # first page failing is a real problem, not a pagination ceiling
            print(f"  [note] treating this as a pagination ceiling, not a crash. "
                  f"Stopping with {len(events)} events collected. This may be a LOWER BOUND "
                  f"on total open events — see coverage_report for total_events_fetched and "
                  f"cross-check against Polymarket's site count if this number looks low.")
            pagination_meta["hit_boundary"] = True
            pagination_meta["boundary_offset"] = offset
            pagination_meta["boundary_detail"] = f"HTTP {exc.status_code}: {exc.body_text[:300]}"
            break
        if not page:
            print(f"  page {page_num}: empty — stopping (offset={offset})")
            break
        if observed_page_size is None:
            observed_page_size = len(page)
            if observed_page_size < PAGE_LIMIT:
                print(f"  [note] requested limit={PAGE_LIMIT} but server returned {observed_page_size} "
                      f"on first page — effective page size appears capped at {observed_page_size}. "
                      f"Continuing to page by actual returned count.")
        events.extend(page)
        print(f"  page {page_num}: fetched {len(page)} events (offset={offset}, total so far={len(events)})")
        offset += len(page)
        if len(page) < observed_page_size:
            # a page shorter than the established page size IS a reliable end-of-data signal
            print(f"  page {page_num}: short page ({len(page)} < {observed_page_size}) — this is the last page")
            break
        time.sleep(SLEEP_BETWEEN_PAGES)
    else:
        print(f"  [warning] hit MAX_PAGES_SAFETY={MAX_PAGES_SAFETY} without an empty page — "
              f"data may be incomplete, investigate before trusting coverage numbers")
    return events, pagination_meta


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


# Single-game sports markets consistently embed the match date directly in
# their slug (e.g. "nwsl-rac-das-2026-07-18", "mls-sea-rsl-2026-04-12").
# Season-long futures slugs don't ("us-presidential-election"). Verified
# against real data on 2026-07-08 — the gameId field originally used for
# this was assumed present but turned out NOT to be populated on these
# markets, so slug-date detection replaces it rather than supplementing it.
_SLUG_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _slug_looks_game_tied(slug: str | None) -> bool:
    return bool(slug and _SLUG_DATE_PATTERN.search(slug))


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
    sports_priority: bool         # True specifically when priority came from futures-like sports, not crypto/politics/economy
    covered: bool
    coverage_reason: str
    noise_excluded: bool          # hard-excluded via NOISE_TAG_SLUGS regardless of floor/priority
    stale_expired: bool           # hard-excluded — days_to_end < 0 but Polymarket still shows it open

    # sports research-suitability signals — descriptive only, not used to exclude.
    # See Mike's "delve deeper before deciding" call: gameId presence marks a
    # market as tied to one specific game (moneyline/spread/total/prop-style);
    # days_to_end gives the horizon. Season-long futures should show no gameId
    # and a long horizon; single-game props should show a gameId and <1-3 days.
    has_game_id: bool              # raw field presence — kept for transparency, NOT reliable alone (see below)
    slug_looks_game_tied: bool     # date embedded in slug — the signal actually used for classification
    days_to_end: float | None

    # live implied probability snapshot (Metaculus CP analogue — not time-gated here)
    outcome_prices: list[str]

    fetched_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


def _days_to_end(end_date_str: str | None, now: dt.datetime) -> float | None:
    if not end_date_str:
        return None
    try:
        end_dt = dt.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return round((end_dt - now).total_seconds() / 86400, 2)
    except (ValueError, TypeError):
        return None


def normalize_market(event: dict, market: dict, event_market_count: int, now: dt.datetime) -> MarketRecord | None:
    condition_id = market.get("conditionId")
    if not condition_id:
        return None  # can't build a canonical record without the canonical key

    tags = [t.get("slug") for t in (event.get("tags") or []) if t.get("slug")]
    slug_tied = _slug_looks_game_tied(event.get("slug"))
    is_sports_futures_like = bool(set(tags) & SPORTS_TAG_SLUGS) and not slug_tied
    priority = bool(set(tags) & PRIORITY_TAG_SLUGS) or is_sports_futures_like
    noise_excluded = bool(set(tags) & NOISE_TAG_SLUGS)
    days_to_end = _days_to_end(market.get("endDate"), now)
    # Some markets sit past their real-world deadline while still flagged
    # active/open on Polymarket's side — likely stuck in an unresolved UMA
    # dispute or just abandoned. Confirmed via real pilot sample (2026-07-08):
    # "kraken-ipo-in-2025", "macron-out-in-2025" etc. were still showing as
    # covered despite 2025 deadlines. Forecasting these burns research budget
    # on questions whose real-world outcome is already effectively known —
    # exclude them the same way noise tags are excluded, not silently.
    stale_expired = days_to_end is not None and days_to_end < 0

    liquidity = _to_float(market.get("liquidity"))
    volume = _to_float(market.get("volume"))

    meets_floor = liquidity >= COVERAGE_LIQUIDITY_MIN or volume >= COVERAGE_VOLUME_MIN
    if stale_expired:
        covered = False
        reason = "stale_expired_but_still_open"
    elif noise_excluded:
        covered = False
        reason = "noise_excluded"
    elif priority and not meets_floor:
        covered = True
        reason = "priority_tag_below_floor"
    elif priority and meets_floor:
        covered = True
        reason = "priority_tag_and_floor"
    elif meets_floor:
        covered = True
        reason = "floor"
    else:
        covered = False
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
        sports_priority=is_sports_futures_like,
        covered=covered,
        coverage_reason=reason,
        noise_excluded=noise_excluded,
        stale_expired=stale_expired,
        has_game_id=bool(market.get("gameId")),
        slug_looks_game_tied=slug_tied,
        days_to_end=days_to_end,
        outcome_prices=_parse_json_field(market.get("outcomePrices"), []),
    )


# ---------------------------------------------------------------------------
# Diagnostics — mirrors meta_coverage_check.py's gap classification
# ---------------------------------------------------------------------------

def run_diagnostics(records: list[MarketRecord], events: list[dict]) -> dict:
    missing_clob_tokens = [r.condition_id for r in records if not r.clob_token_ids]
    missing_outcomes = [r.condition_id for r in records if not r.outcomes]

    # negRisk sum-check, narrowed to exclude game-tied markets. A negRisk group
    # of independent props bundled for capital efficiency (e.g. "exact score",
    # 17 outcomes each ~0.5) is NOT a probability partition and will always
    # legitimately sum to something >> 1 — that's not mispricing, it's a wrong
    # assumption on this check's part. Real categorical partitions (election
    # winner, inflation bucket) don't carry gameId, so filtering on that keeps
    # this check meaningful instead of drowning in sports false positives.
    neg_risk_flags = []
    events_by_id = {str(e.get("id")): e for e in events}
    grouped: dict[str, list[MarketRecord]] = {}
    for r in records:
        if r.neg_risk and r.is_group_member and not r.slug_looks_game_tied:
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

    # Sports research-suitability breakdown — descriptive, not a filter. Splits
    # by gameId presence (game-tied vs. futures) and time horizon, so the real
    # distribution can inform where to draw the line, rather than guessing.
    sports_records = [r for r in records if set(r.tags) & SPORTS_TAG_SLUGS]
    game_tied = [r for r in sports_records if r.slug_looks_game_tied]
    futures_like = [r for r in sports_records if not r.slug_looks_game_tied]
    game_id_populated_count = sum(1 for r in sports_records if r.has_game_id)

    def _bucket_by_horizon(recs: list[MarketRecord]) -> dict:
        buckets = {name: 0 for name, _ in SPORTS_HORIZON_BUCKETS}
        buckets["unknown_end_date"] = 0
        for r in recs:
            if r.days_to_end is None:
                buckets["unknown_end_date"] += 1
                continue
            for name, max_days in SPORTS_HORIZON_BUCKETS:
                if max_days is None or r.days_to_end <= max_days:
                    buckets[name] += 1
                    break
        return buckets

    sports_breakdown = {
        "total_sports_tagged_markets": len(sports_records),
        "game_tied_by_slug_date": len(game_tied),
        "futures_like_no_slug_date": len(futures_like),
        "gameId_field_actually_populated_count": game_id_populated_count,  # sanity check — expect this to be low/0
        "game_tied_horizon_buckets": _bucket_by_horizon(game_tied),
        "futures_like_horizon_buckets": _bucket_by_horizon(futures_like),
        # a small sample of the futures-like candidates, for a gut check on
        # whether these actually look like research-suitable questions
        "futures_like_sample": [
            {"event_slug": r.event_slug, "question": r.question, "days_to_end": r.days_to_end,
             "liquidity": r.liquidity, "volume": r.volume}
            for r in sorted(futures_like, key=lambda x: -x.volume)[:15]
        ],
    }

    noise_excluded = [r for r in records if r.noise_excluded]
    stale_excluded = [r for r in records if r.stale_expired]
    covered = [r for r in records if r.covered]
    priority_covered = [r for r in covered if r.priority]
    sports_priority_covered = [r for r in covered if r.sports_priority]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total_events_fetched": len(events),
        "total_markets_seen": len(records),
        "markets_covered": len(covered),
        "markets_covered_via_priority_tag": len(priority_covered),
        "markets_covered_via_sports_futures_priority": len(sports_priority_covered),
        "markets_noise_excluded": len(noise_excluded),
        "markets_stale_expired_excluded": len(stale_excluded),
        "markets_excluded": len(records) - len(covered) - len(noise_excluded) - len(stale_excluded),
        "markets_missing_clob_token_ids": len(missing_clob_tokens),
        "markets_missing_outcomes": len(missing_outcomes),
        "missing_clob_token_id_sample": missing_clob_tokens[:20],
        "neg_risk_group_price_sum_flags": neg_risk_flags,
        "sports_research_suitability_breakdown": sports_breakdown,
        "coverage_liquidity_min": COVERAGE_LIQUIDITY_MIN,
        "coverage_volume_min": COVERAGE_VOLUME_MIN,
        "priority_tag_slugs_unconditional": sorted(PRIORITY_TAG_SLUGS),
        "priority_sports_tag_slugs_conditional_on_futures_like": sorted(SPORTS_TAG_SLUGS),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "mike-poly-bot-discovery/0.1"})

    print("Fetching active, open events from Gamma API...")
    events, pagination_meta = fetch_all_events(session, active=True, closed=False)
    print(f"Total events fetched via offset pagination: {len(events)}")

    keyset_meta = {"attempted": False}
    if pagination_meta.get("hit_boundary"):
        print("\nOffset pagination hit its ceiling — attempting to extend coverage via /events/keyset...")
        seen_ids = {str(e.get("id")) for e in events}
        new_events, keyset_meta = fetch_events_keyset_continuation(session, seen_ids, active=True, closed=False)
        events.extend(new_events)
        print(f"Keyset continuation added {len(new_events)} new events "
              f"(stopped: {keyset_meta['stopped_reason']}). Total events now: {len(events)}")
        if keyset_meta.get("cursor_bug_suspected"):
            print("[warning] keyset cursor appears not to be advancing — this matches a previously "
                  "reported Polymarket bug (Polymarket/agents#227). Coverage beyond the offset ceiling "
                  "may be incomplete. Worth re-checking this in a few weeks in case it's since been fixed.")
    pagination_meta["keyset_continuation"] = keyset_meta

    records: list[MarketRecord] = []
    skipped_raw_samples: list[dict] = []
    fetch_now = dt.datetime.now(dt.timezone.utc)
    for event in events:
        markets = event.get("markets") or []
        for market in markets:
            rec = normalize_market(event, market, event_market_count=len(markets), now=fetch_now)
            if rec is not None:
                records.append(rec)
            else:
                print(f"  [skip] market with no conditionId in event {event.get('id')} ({event.get('slug')})")
                if len(skipped_raw_samples) < 5:
                    # capture the raw market fields (minus long text) so we can see
                    # what's actually different about these instead of guessing
                    skipped_raw_samples.append({
                        "event_id": event.get("id"),
                        "event_slug": event.get("slug"),
                        "market_keys_present": sorted(market.keys()),
                        "market_id": market.get("id"),
                        "market_slug": market.get("slug"),
                        "active": market.get("active"),
                        "closed": market.get("closed"),
                        "enableOrderBook": market.get("enableOrderBook"),
                        "clobTokenIds": market.get("clobTokenIds"),
                    })

    report = run_diagnostics(records, events)
    report["pagination"] = pagination_meta
    report["skipped_missing_condition_id_raw_sample"] = skipped_raw_samples

    # Tag frequency across everything seen — sanity check for whether the
    # priority tags are genuinely narrow or effectively matching everything
    tag_counts: dict[str, int] = {}
    for event in events:
        for t in (event.get("tags") or []):
            slug = t.get("slug")
            if slug:
                tag_counts[slug] = tag_counts.get(slug, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:25]
    report["top_25_tags_by_event_count"] = top_tags
    report["total_distinct_tags_seen"] = len(tag_counts)

    print("\n--- Coverage report (summary) ---")
    summary_only_as_count = {
        "missing_clob_token_id_sample", "neg_risk_group_price_sum_flags",
        "skipped_missing_condition_id_raw_sample", "top_25_tags_by_event_count",
    }
    for k, v in report.items():
        if isinstance(v, list) and k in summary_only_as_count:
            print(f"{k}: {len(v)} item(s)")
        elif k == "sports_research_suitability_breakdown":
            print(f"{k}: (see full detail below)")
        else:
            print(f"{k}: {v}")

    print("\n--- Full diagnostic detail (these don't show above, and this is the whole point of a dry run) ---")
    print(f"\ntop_25_tags_by_event_count:\n{json.dumps(top_tags, indent=2)}")
    print(f"\nneg_risk_group_price_sum_flags:\n{json.dumps(report['neg_risk_group_price_sum_flags'], indent=2)}")
    print(f"\nskipped_missing_condition_id_raw_sample:\n{json.dumps(skipped_raw_samples, indent=2)}")
    print(f"\nsports_research_suitability_breakdown:\n{json.dumps(report['sports_research_suitability_breakdown'], indent=2)}")

    # Report is diagnostics only — always write it, dry-run or not, since it costs
    # nothing and saves a round trip when you just want to eyeball numbers.
    STATE_DIR.mkdir(exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    print(f"\nWrote diagnostics to {REPORT_FILE}")

    if dry_run:
        print("[dry-run] Not writing coverage.json (the actual covered-market state).")
        return

    coverage_out = {
        r.condition_id: asdict(r)
        for r in records
        if r.covered
    }
    COVERAGE_FILE.write_text(json.dumps(coverage_out, indent=2))
    print(f"Wrote {len(coverage_out)} covered markets to {COVERAGE_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket discovery/coverage layer")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report but don't write state files")
    args = parser.parse_args()
    run(dry_run=args.dry_run)