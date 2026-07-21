"""
poly_batch_forecast.py

The real throttled batch-forecasting loop. Selects a small number of covered
events, runs research -> reasoning -> verification on each, and stops the
moment EITHER the daily event cap OR a spend circuit-breaker is hit —
whichever comes first.

Deliberately conservative by design, per Mike (2026-07-08): the $0.50/$1.00
daily figures from the cost-analysis conversation were WORST-CASE ceilings,
not targets. This is paper trading only right now — no reason to run near
those ceilings. Start small, watch a few real runs, raise NEW_COUNT_DEFAULT
later once it's proven out — don't raise it preemptively.

RESEARCH SOURCE SWAP (2026-07-20, per Mike): research now runs through
Tavily instead of OpenRouter/Gemini :online grounding. Reason: the
OPENROUTER_API_KEY here is the SAME key/balance as Ben's Metaculus-bot
allocation — every dollar spent here was coming out of money earmarked for
a different, higher-priority project. Tavily's free tier is a genuinely
recurring 1,000 API credits/month (not a one-time grant), which comfortably
covers this bot's usage at its current (lowest-priority) scale. Output
shape changed accordingly: Tavily returns search result snippets plus an
optional synthesized "answer" field, not the fuller narrative summary
Gemini's grounding produced — a reasonable trade for a free, low-priority
research source, but worth knowing if forecast quality is ever compared
against the earlier OpenRouter-era batches.

Cost accounting changed from dollars to CREDITS for the research step —
TAVILY_CREDIT_CEILING is a per-run credit budget, not a dollar figure.
The Anthropic reasoning/verification steps are unchanged (still dollars,
still ANTHROPIC_SPEND_CEILING) — only the research source moved.

REFRESH OVERRIDE (2026-07-20, per Mike): an explicit --refresh-count is a
deliberate request that must not get silently dropped by a ceiling — those
events bypass TAVILY_CREDIT_CEILING / ANTHROPIC_SPEND_CEILING entirely.
New-discovery candidates still respect both ceilings normally; the override
only applies to events actually selected via the refresh quota.

State persistence: poly_state/forecast_history.json tracks last-forecast
time per event so re-runs don't burn budget re-forecasting the same handful
of top-volume markets every day — this needs to be committed back to the
repo after each run (see the paired workflow) or every run starts blind.

v1 SCOPE NOTE: for grouped/categorical events (e.g. "World Cup winner" with
30+ country markets), this version forecasts only the single highest-volume
market in the group, not the full outcome distribution. That's a deliberate
simplification to get the end-to-end loop proven out first — extending to
full group-aware distributional forecasting (mirroring the Metaculus
MultipleChoice handling) is a follow-up, not done here.

CANDIDATE SELECTION (2026-07-19 update): new-discovery and refresh-eligible
events are two separate pools competing for the DAILY_EVENT_CAP slots, not
one merged ranking. Two reasons this changed:

  1. With ~40,000 covered markets and single digits forecast so far, a
     merged priority/volume ranking meant already-forecast events almost
     never resurfaced — new high-volume events kept winning the ranking
     every run. --refresh-count reserves N of the cap's slots specifically
     for refresh-eligible events, so old forecasts get revisited on a
     predictable cadence instead of hoping they rank well.
  2. New-discovery ranking now sorts by days-to-close BEFORE volume (was
     volume-only, priority tag aside). A one-shot forecast on a market
     closing in 3 years is stale well before it resolves — nothing updates
     it in between. Preferring near-term closes means the (mostly one-shot)
     forecast stays representative up to resolution, and mirrors a known
     Metaculus pain point: too few near-term-closing questions means a long
     wait before a forecaster's calibration score means anything.

Refresh-eligible events are ranked by a blended score: rank by days-to-close
(soonest = best) and separately by hours-since-last-forecast (oldest = best),
then sum the two ranks and take the lowest (best-of-both) first. This is a
deliberately simple rank-sum blend, not a weighted formula — the two
quantities are different units (days vs. hours) with no principled exchange
rate between them, so combining ranks avoids inventing one.

Run with --refresh-count N to reserve N slots for refresh-eligible events
(default 0 — no dedicated quota, same as pre-2026-07-19 behavior). No
interactive per-event approval — this only ever runs unattended via the
GitHub Actions workflow_dispatch path, by design.

TOTAL FORECAST CAP + --new-count (2026-07-21, per Mike): two related but
separate controls added:

  1. TOTAL_FORECAST_CAP (100) — once forecast_history.json has this many
     DISTINCT events tracked, new-discovery stops growing that set,
     regardless of --new-count. This bounds the SIZE of the tracked pool,
     not any single run's spend. --refresh-count is completely unaffected
     — refreshing an already-tracked event never grows the pool, so it
     keeps working past the cap forever.
  2. --new-count N — how many brand-new (never-forecast) events to select
     this run (default 15, same as the old implicit DAILY_EVENT_CAP
     behavior when --refresh-count wasn't used). Independent of
     --refresh-count now — previously new-discovery only got whatever was
     left of DAILY_EVENT_CAP after refresh took its share; now each is its
     own explicit request. If --new-count asks for more than the total cap
     has room for, it's silently clamped (logged, not an error) rather than
     refused outright.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv

import poly_discovery as disco

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
TAVILY_SEARCH_DEPTH = "advanced"  # 2 credits/request vs. 1 for "basic" — worth it for
                                   # forecasting research quality, still trivial against
                                   # the 1,000/month free allotment at this bot's scale

ANTHROPIC_HAIKU_INPUT_PER_MTOK = 1.00
ANTHROPIC_HAIKU_OUTPUT_PER_MTOK = 5.00

# --- Conservative-by-design throttle settings -------------------------------
# Deliberately far below the stated $0.50 (Anthropic) worst-case daily budget,
# and well below the 1,000/month free Tavily allotment. Raise these only
# after watching real runs.
NEW_COUNT_DEFAULT = 15            # default for --new-count when not passed explicitly
                                   # (was DAILY_EVENT_CAP -- renamed 2026-07-21 since it's
                                   # no longer a shared cap, just new-discovery's own default)
TOTAL_FORECAST_CAP = 100          # once forecast_history.json tracks this many distinct
                                   # events, new-discovery stops growing the pool -- does
                                   # NOT affect --refresh-count, which keeps working on
                                   # already-tracked events forever
TAVILY_CREDIT_CEILING = 40        # ~20 advanced-search events/run at 2 credits each —
                                   # leaves room for many runs/month inside the free 1,000
ANTHROPIC_SPEND_CEILING = 0.05    # vs. $0.50 stated worst case
REFRESH_GATE_HOURS = 72           # don't re-forecast the same event within 3 days — prioritize breadth first

# Contested-market band for candidate selection (2026-07-08): a market priced
# outside this range is close enough to a settled "yes"/"no" that research
# spend adds little — the whole point of paying for research is to inform a
# genuinely uncertain estimate, not confirm what the market already knows.
UNCERTAINTY_PRICE_MIN = 0.05
UNCERTAINTY_PRICE_MAX = 0.95


def _ttl_sort_value(r) -> float:
    """days_to_end for sorting — None (missing/malformed end_date upstream)
    sorts LAST (treated as least urgent), never crashes the sort. Confirmed
    in production (2026-07-19): some covered markets have days_to_end=None,
    which a bare `r.days_to_end` sort key raises TypeError on (None has no
    ordering vs. float in Python 3) — this wasn't caught in testing because
    the old ranking never sorted on days_to_end at all."""
    return r.days_to_end if r.days_to_end is not None else float("inf")


def _ttl_display(r) -> str:
    return f"{r.days_to_end:.1f}d" if r.days_to_end is not None else "?d (no end_date)"


def _yes_price(rec) -> float | None:
    """Best-effort parse of the market's implied Yes probability from
    outcome_prices[0] — same convention used elsewhere in poly_discovery.py's
    negRisk sum-check."""
    if not rec.outcome_prices:
        return None
    try:
        return float(rec.outcome_prices[0])
    except (TypeError, ValueError, IndexError):
        return None

STATE_DIR = Path("poly_state")
HISTORY_FILE = STATE_DIR / "forecast_history.json"
FORECASTS_LOG_FILE = STATE_DIR / "forecasts_log.jsonl"

REQUEST_TIMEOUT = 60


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def select_candidates(history: dict, refresh_count: int = 0,
                       new_count: int = NEW_COUNT_DEFAULT) -> tuple[list, set]:
    """Discover covered markets, group by event, split into never-forecast
    and refresh-eligible pools (see module docstring), and return the final
    candidate list plus the set of event_ids that were chosen via the
    refresh quota (so callers can label them in logs/output).

    new_count is clamped by TOTAL_FORECAST_CAP: once len(history) distinct
    events are already tracked, new-discovery has that much less room
    (down to zero) regardless of what new_count asks for. refresh_count is
    NOT affected by this cap at all.
    """
    print("Discovering covered markets...")
    records, _events, _pagination_meta, _skipped = disco.discover_all_markets(verbose=False)
    covered = [r for r in records if r.covered]
    print(f"Covered markets available: {len(covered)}")

    by_event: dict[str, list] = {}
    for r in covered:
        by_event.setdefault(r.event_id, []).append(r)

    now = dt.datetime.now(dt.timezone.utc)
    candidates_skipped_no_contested_market = 0
    never_forecast = []
    refresh_eligible = []  # list of (record, hours_since_last_forecast)

    for event_id, recs in by_event.items():
        # Within a categorical event, the highest-VOLUME market is often a
        # novelty/meme longshot (e.g. a celebrity candidate), not the one
        # that's actually contested — those draw disproportionate volume
        # precisely because they're a fun bet, not because the outcome is
        # uncertain. Research spend is wasted on a market that's already
        # priced near-certain. Filter to genuinely contested markets first
        # (price inside the uncertainty band), THEN pick by volume among
        # those — keeps liquidity as a tiebreaker without picking obvious "no"s.
        contested = [r for r in recs if (p := _yes_price(r)) is not None
                     and UNCERTAINTY_PRICE_MIN <= p <= UNCERTAINTY_PRICE_MAX]
        if not contested:
            candidates_skipped_no_contested_market += 1
            continue
        top_market = max(contested, key=lambda r: r.volume)

        last = history.get(event_id, {}).get("last_forecast_at")
        if last is None:
            never_forecast.append(top_market)
        else:
            hours_since = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600
            if hours_since >= REFRESH_GATE_HOURS:
                refresh_eligible.append((top_market, hours_since))
            # else: inside the gate, excluded entirely — not ready yet

    # --- Refresh pool: blended rank (days-to-close rank + staleness rank) --
    chosen_refresh_ids: set = set()
    chosen_refresh = []
    if refresh_count > 0 and refresh_eligible:
        by_ttl = sorted(refresh_eligible, key=lambda x: _ttl_sort_value(x[0]))
        ttl_rank = {r.event_id: i for i, (r, _) in enumerate(by_ttl)}
        by_staleness = sorted(refresh_eligible, key=lambda x: -x[1])  # oldest forecast first
        staleness_rank = {r.event_id: i for i, (r, _) in enumerate(by_staleness)}

        blended = sorted(
            refresh_eligible,
            key=lambda x: ttl_rank[x[0].event_id] + staleness_rank[x[0].event_id],
        )
        chosen_refresh = [r for r, _hours in blended[:refresh_count]]
        chosen_refresh_ids = {r.event_id for r in chosen_refresh}

    # --- New-discovery pool: priority tag, then days-to-close, then volume --
    tracked_count = len(history)
    room_under_total_cap = max(TOTAL_FORECAST_CAP - tracked_count, 0)
    new_planned = min(new_count, room_under_total_cap)
    never_forecast.sort(key=lambda r: (not r.priority, _ttl_sort_value(r), -r.volume))
    chosen_new = never_forecast[:new_planned]

    final_candidates = chosen_refresh + chosen_new

    print(f"Never-forecast candidates available: {len(never_forecast)}")
    print(f"Refresh-eligible candidates available: {len(refresh_eligible)} "
          f"(gate: {REFRESH_GATE_HOURS}h since last forecast)")
    print(f"Events skipped — no market priced in the contested "
          f"[{UNCERTAINTY_PRICE_MIN}, {UNCERTAINTY_PRICE_MAX}] band: {candidates_skipped_no_contested_market}")
    if refresh_count > 0:
        print(f"Refresh quota requested: {refresh_count} -> selected {len(chosen_refresh)}")
        for r in chosen_refresh:
            print(f"  [refresh] {r.event_slug}  closes in {_ttl_display(r)}")
    print(f"Total distinct events tracked: {tracked_count} "
          f"(cap {TOTAL_FORECAST_CAP}, {room_under_total_cap} room remaining)")
    clamp_note = ""
    if new_count > room_under_total_cap:
        clamp_note = f" — clamped from requested {new_count} by the {TOTAL_FORECAST_CAP}-event total cap"
    print(f"New-discovery requested: {new_count} -> planned {new_planned}{clamp_note} "
          f"-> filled {len(chosen_new)} (of {len(never_forecast)} never-forecast candidates available)")

    return final_candidates, chosen_refresh_ids


TAVILY_CREDITS_PER_REQUEST = {"basic": 1, "advanced": 2}[TAVILY_SEARCH_DEPTH]


def call_tavily_research(question: str) -> dict:
    """Tavily returns search result snippets plus an optional synthesized
    "answer" field — combine both into one research text block for the
    reasoning prompt. Credit cost is deterministic per Tavily's own billing
    (not returned in the response body), so it's tracked from the request
    parameters, not parsed out of the response."""
    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"},
        json={
            "query": question,
            "search_depth": TAVILY_SEARCH_DEPTH,
            "max_results": 5,
            "include_answer": True,
            "include_raw_content": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    parts = []
    if data.get("answer"):
        parts.append(f"Summary: {data['answer']}")
    for r in data.get("results", []):
        title = r.get("title", "?")
        content = r.get("content", "")
        url = r.get("url", "?")
        parts.append(f"- {title}: {content} (source: {url})")
    text = "\n".join(parts) if parts else "(no research results returned)"

    return {
        "text": text,
        "credits_used": TAVILY_CREDITS_PER_REQUEST,
        "result_count": len(data.get("results", [])),
    }


def call_anthropic(prompt: str, max_tokens: int = 500) -> dict:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": ANTHROPIC_MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"text": text, "usage": data.get("usage", {})}


def anthropic_cost(usage: dict) -> float:
    return (usage.get("input_tokens", 0) / 1_000_000 * ANTHROPIC_HAIKU_INPUT_PER_MTOK
            + usage.get("output_tokens", 0) / 1_000_000 * ANTHROPIC_HAIKU_OUTPUT_PER_MTOK)


def extract_probability(text: str) -> tuple[float | None, str]:
    """Pull the probability from the reasoning output.

    v1 (loose regex over the whole text) had a confirmed bug: with no
    explicit probability statement present, it fell back to grabbing ANY
    0/1/0.XX-looking token — including markdown numbered-list markers like
    "1. **The succession...**", which is not a probability at all. Real
    example (2026-07-16): a truncated response with no stated probability
    got misread as 1.0 purely from a list marker.

    Fix: only trust an EXPLICIT "Probability: X" statement (which the
    reasoning prompt now asks for on the first line, specifically so
    truncation at max_tokens can't lose it). No loose fallback — an
    honestly missing estimate is safer than a confidently wrong one.

    Returns (probability_or_None, method) where method is "explicit" or
    "not_found" — callers should treat "not_found" as missing data, not
    silently default it to something.
    """
    import re
    m = re.search(r"probability\s*:?\s*(\d?\.\d+|\d)\b", text, re.IGNORECASE)
    if m:
        try:
            val = float(m.group(1))
            if 0.0 <= val <= 1.0:
                return val, "explicit"
        except ValueError:
            pass
    return None, "not_found"


def run_forecast_loop(live: bool, refresh_count: int = 0, new_count: int = NEW_COUNT_DEFAULT) -> None:
    if not ANTHROPIC_API_KEY or not TAVILY_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY and/or TAVILY_API_KEY not found in environment/.env")
        sys.exit(1)

    history = load_history()
    candidates, refresh_ids = select_candidates(history, refresh_count=refresh_count, new_count=new_count)

    refresh_n = len(refresh_ids)
    new_n = len(candidates) - refresh_n
    print(f"\nSelected {len(candidates)} events for this run "
          f"({refresh_n} refresh + {new_n} new — refresh_count and new_count are independent, "
          f"neither caps the other; new-discovery is separately capped by the "
          f"{TOTAL_FORECAST_CAP}-event total tracked-event limit):")
    for c in candidates:
        price = _yes_price(c)
        price_str = f"{price:.2f}" if price is not None else "?"
        label = "refresh" if c.event_id in refresh_ids else ("priority" if c.priority else "floor")
        print(f"  [{label}] {c.event_slug}: {c.question} "
              f"(vol=${c.volume:,.0f}, price={price_str}, closes in {_ttl_display(c)})")

    if not live:
        print(f"\n[dry-run] Not spending anything. New-discovery events stop early if Tavily "
              f"credits > {TAVILY_CREDIT_CEILING} or Anthropic spend > ${ANTHROPIC_SPEND_CEILING:.2f} "
              f"— refresh events ({refresh_n} selected) bypass that ceiling entirely and always run. "
              f"Re-run with --live to execute.")
        return

    tavily_credits_spent = 0
    anthropic_spend = 0.0
    processed = 0
    stop_reason = "completed_all_candidates"

    for c in candidates:
        is_refresh = c.event_id in refresh_ids
        if not is_refresh:
            if tavily_credits_spent >= TAVILY_CREDIT_CEILING:
                stop_reason = f"tavily_credit_ceiling_hit ({tavily_credits_spent} >= {TAVILY_CREDIT_CEILING})"
                break
            if anthropic_spend >= ANTHROPIC_SPEND_CEILING:
                stop_reason = f"anthropic_spend_ceiling_hit (${anthropic_spend:.4f} >= ${ANTHROPIC_SPEND_CEILING:.2f})"
                break
        elif tavily_credits_spent >= TAVILY_CREDIT_CEILING or anthropic_spend >= ANTHROPIC_SPEND_CEILING:
            print(f"  [refresh override] proceeding past normal ceiling for {c.event_slug} "
                  f"(explicit --refresh-count request, Tavily credits={tavily_credits_spent}, Anthropic=${anthropic_spend:.4f})")

        print(f"\n--- {c.event_slug} ---")
        try:
            research = call_tavily_research(c.question)
            print(f"  Tavily research: {research['credits_used']} credit(s), {research['result_count']} result(s)")

            # Deliberately NOT showing the market price here. Same principle as
            # FutureEval time-gating community-prediction reveal until close:
            # showing the market's own number before asking for an estimate
            # anchors the model toward "market price plus a small adjustment"
            # rather than a genuinely independent view. Confirmed happening in
            # practice (2026-07-18) — reasoning text explicitly said "the market
            # prices this at 20.5%... slightly conservative," which is anchoring,
            # not independent forecasting. market_price_at_forecast is still
            # recorded separately for the edge calculation — nothing is lost,
            # the model just doesn't get to see it before committing to a number.
            reasoning_prompt = (
                f"Prediction market question: {c.question}\n"
                f"Research:\n{research['text']}\n\n"
                f"Start your response with your calibrated probability estimate on its own "
                f"first line, exactly as: 'Probability: 0.35' (a decimal between 0 and 1). "
                f"Base this ONLY on the research above — form your own independent view. "
                f"Then give 2-3 sentences of reasoning. Leading with the number matters — "
                f"if your response gets cut off, the probability must already be captured."
            )
            reasoning = call_anthropic(reasoning_prompt, max_tokens=500)
            reasoning_cost = anthropic_cost(reasoning["usage"])
            print(f"  Anthropic reasoning: ${reasoning_cost:.4f}")

            verify_prompt = (
                f"Review this forecast reasoning for internal contradictions or unsupported "
                f"claims. Be brief.\n\n{reasoning['text']}"
            )
            verify = call_anthropic(verify_prompt, max_tokens=200)
            verify_cost = anthropic_cost(verify["usage"])
            print(f"  Anthropic verification: ${verify_cost:.4f}")

            probability, extraction_method = extract_probability(reasoning["text"])
            if extraction_method == "not_found":
                print(f"  [warning] no explicit probability statement found — leaving unset "
                      f"rather than guessing (see extract_probability docstring)")

            tavily_credits_spent += research["credits_used"]
            anthropic_spend += reasoning_cost + verify_cost
            processed += 1

            forecast_record = {
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "event_id": c.event_id,
                "event_slug": c.event_slug,
                "condition_id": c.condition_id,
                "question": c.question,
                "category": "priority" if c.priority else "floor",
                "market_price_at_forecast": c.outcome_prices,
                "estimated_probability": probability,
                "probability_extraction_method": extraction_method,
                "end_date": c.end_date,
                "days_to_end_at_forecast": c.days_to_end,
                "reasoning_text": reasoning["text"],
                "verification_text": verify["text"],
                "research_source": "tavily",
                "tavily_credits_used": research["credits_used"],
                "anthropic_cost": reasoning_cost + verify_cost,
            }
            STATE_DIR.mkdir(exist_ok=True)
            with open(FORECASTS_LOG_FILE, "a") as f:
                f.write(json.dumps(forecast_record) + "\n")

            history[c.event_id] = {
                "last_forecast_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "event_slug": c.event_slug,
                "last_probability": probability,
            }

        except requests.RequestException as exc:
            print(f"  [error] request failed, skipping this event: {exc}")
            continue

    save_history(history)

    print(f"\n=== Run summary ===")
    print(f"Events processed: {processed}")
    print(f"Stop reason: {stop_reason}")
    print(f"Tavily credits used: {tavily_credits_spent} (ceiling {TAVILY_CREDIT_CEILING}, "
          f"of 1,000/month free)")
    print(f"Anthropic spend: ${anthropic_spend:.4f} (ceiling ${ANTHROPIC_SPEND_CEILING:.2f})")
    print(f"Wrote {FORECASTS_LOG_FILE} and {HISTORY_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Throttled batch forecasting loop")
    parser.add_argument("--live", action="store_true", help="Actually spend money (default: dry preview only)")
    parser.add_argument("--refresh-count", type=int, default=0,
                         help="Reserve this many slots for refresh-eligible events (blended rank: "
                              "soonest-to-close + oldest-forecast first). Default 0 — no dedicated "
                              "quota. Always runs in full regardless of any ceiling or cap.")
    parser.add_argument("--new-count", type=int, default=NEW_COUNT_DEFAULT,
                         help=f"How many brand-new (never-forecast) events to select this run "
                              f"(default {NEW_COUNT_DEFAULT}). Silently clamped (logged, not an "
                              f"error) once forecast_history.json has {TOTAL_FORECAST_CAP} distinct "
                              f"events tracked — does not affect --refresh-count.")
    args = parser.parse_args()
    run_forecast_loop(live=args.live, refresh_count=args.refresh_count, new_count=args.new_count)