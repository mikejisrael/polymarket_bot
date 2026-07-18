"""
poly_batch_forecast.py

The real throttled batch-forecasting loop. Selects a small number of covered
events, runs research -> reasoning -> verification on each, and stops the
moment EITHER the daily event cap OR a spend circuit-breaker is hit —
whichever comes first.

Deliberately conservative by design, per Mike (2026-07-08): the $0.50/$1.00
daily figures from the cost-analysis conversation were WORST-CASE ceilings,
not targets. This is paper trading only right now — no reason to run near
those ceilings. The OpenRouter balance in particular is earmarked for the
Metaculus bot (Ben's allocation), not this project, so this script's actual
spend ceilings are set well below the stated budget on purpose. Start small,
watch a few real runs, raise DAILY_EVENT_CAP later once it's proven out —
don't raise it preemptively.

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
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv

import poly_discovery as disco

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OPENROUTER_MODEL = "google/gemini-2.5-flash:online"

ANTHROPIC_HAIKU_INPUT_PER_MTOK = 1.00
ANTHROPIC_HAIKU_OUTPUT_PER_MTOK = 5.00

# --- Conservative-by-design throttle settings -------------------------------
# Deliberately far below the stated $0.50 (Anthropic) / $1.00 (OpenRouter)
# worst-case daily budgets. Raise these only after watching real runs.
DAILY_EVENT_CAP = 15
OPENROUTER_SPEND_CEILING = 0.10   # vs. $1.00 stated worst case
ANTHROPIC_SPEND_CEILING = 0.05    # vs. $0.50 stated worst case
REFRESH_GATE_HOURS = 72           # don't re-forecast the same event within 3 days — prioritize breadth first

# Contested-market band for candidate selection (2026-07-08): a market priced
# outside this range is close enough to a settled "yes"/"no" that research
# spend adds little — the whole point of paying for research is to inform a
# genuinely uncertain estimate, not confirm what the market already knows.
UNCERTAINTY_PRICE_MIN = 0.05
UNCERTAINTY_PRICE_MAX = 0.95


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
GENERATION_STATS_RETRIES = 5
GENERATION_STATS_RETRY_DELAY = 2


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def select_candidates(history: dict) -> list[dict]:
    """Discover covered markets, group by event, exclude recently-forecast
    events, rank priority-first then by volume, return one representative
    market per candidate event (see v1 scope note in the module docstring)."""
    print("Discovering covered markets...")
    records, _events, _pagination_meta, _skipped = disco.discover_all_markets(verbose=False)
    covered = [r for r in records if r.covered]
    print(f"Covered markets available: {len(covered)}")

    by_event: dict[str, list] = {}
    for r in covered:
        by_event.setdefault(r.event_id, []).append(r)

    now = dt.datetime.now(dt.timezone.utc)
    candidates_skipped_no_contested_market = 0
    candidates = []
    for event_id, recs in by_event.items():
        last = history.get(event_id, {}).get("last_forecast_at")
        if last:
            last_dt = dt.datetime.fromisoformat(last)
            hours_since = (now - last_dt).total_seconds() / 3600
            if hours_since < REFRESH_GATE_HOURS:
                continue

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
        candidates.append(top_market)

    candidates.sort(key=lambda r: (not r.priority, -r.volume))
    print(f"Candidate events after refresh-gate filter: {len(candidates)} "
          f"(gate: {REFRESH_GATE_HOURS}h since last forecast)")
    print(f"Events skipped — no market priced in the contested "
          f"[{UNCERTAINTY_PRICE_MIN}, {UNCERTAINTY_PRICE_MAX}] band: {candidates_skipped_no_contested_market}")
    return candidates[:DAILY_EVENT_CAP]


def call_openrouter_research(question: str) -> dict:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{
                "role": "user",
                "content": (
                    f"Research the following prediction market question and summarize the "
                    f"most relevant, current facts that would inform a probability estimate. "
                    f"Be concise — bullet points, cite what you find.\n\nQuestion: {question}"
                ),
            }],
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "text": data["choices"][0]["message"]["content"],
        "generation_id": data.get("id"),
        "usage": data.get("usage", {}),
    }


def get_openrouter_actual_cost(generation_id: str) -> float | None:
    if not generation_id:
        return None
    for _ in range(GENERATION_STATS_RETRIES):
        try:
            resp = requests.get(
                "https://openrouter.ai/api/v1/generation",
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                cost = resp.json().get("data", {}).get("total_cost")
                if cost is not None:
                    return float(cost)
        except requests.RequestException:
            pass
        time.sleep(GENERATION_STATS_RETRY_DELAY)
    return None


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


def run_forecast_loop(live: bool) -> None:
    if not ANTHROPIC_API_KEY or not OPENROUTER_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY and/or OPENROUTER_API_KEY not found in environment/.env")
        sys.exit(1)

    history = load_history()
    candidates = select_candidates(history)

    print(f"\nSelected {len(candidates)} events for this run (cap={DAILY_EVENT_CAP}):")
    for c in candidates:
        price = _yes_price(c)
        price_str = f"{price:.2f}" if price is not None else "?"
        print(f"  [{'priority' if c.priority else 'floor'}] {c.event_slug}: {c.question} "
              f"(vol=${c.volume:,.0f}, price={price_str})")

    if not live:
        print(f"\n[dry-run] Not spending anything. Would process up to {len(candidates)} events, "
              f"stopping early if OpenRouter spend > ${OPENROUTER_SPEND_CEILING:.2f} or "
              f"Anthropic spend > ${ANTHROPIC_SPEND_CEILING:.2f}. Re-run with --live to execute.")
        return

    or_spend = 0.0
    anthropic_spend = 0.0
    processed = 0
    stop_reason = "completed_all_candidates"

    for c in candidates:
        if or_spend >= OPENROUTER_SPEND_CEILING:
            stop_reason = f"openrouter_spend_ceiling_hit (${or_spend:.4f} >= ${OPENROUTER_SPEND_CEILING:.2f})"
            break
        if anthropic_spend >= ANTHROPIC_SPEND_CEILING:
            stop_reason = f"anthropic_spend_ceiling_hit (${anthropic_spend:.4f} >= ${ANTHROPIC_SPEND_CEILING:.2f})"
            break

        print(f"\n--- {c.event_slug} ---")
        try:
            research = call_openrouter_research(c.question)
            or_cost = get_openrouter_actual_cost(research["generation_id"])
            or_cost_measured = or_cost is not None
            if or_cost is None:
                u = research["usage"]
                or_cost = (u.get("prompt_tokens", 0) / 1_000_000 * 0.30
                           + u.get("completion_tokens", 0) / 1_000_000 * 2.50)
                print(f"  [warning] OpenRouter generation stats unavailable — token-only floor estimate: ${or_cost:.4f}")
            else:
                print(f"  OpenRouter research: ${or_cost:.4f}")

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

            or_spend += or_cost
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
                "openrouter_cost": or_cost,
                "openrouter_cost_measured": or_cost_measured,
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
    print(f"OpenRouter spend: ${or_spend:.4f} (ceiling ${OPENROUTER_SPEND_CEILING:.2f})")
    print(f"Anthropic spend: ${anthropic_spend:.4f} (ceiling ${ANTHROPIC_SPEND_CEILING:.2f})")
    print(f"Total spend: ${or_spend + anthropic_spend:.4f}")
    print(f"Wrote {FORECASTS_LOG_FILE} and {HISTORY_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Throttled batch forecasting loop")
    parser.add_argument("--live", action="store_true", help="Actually spend money (default: dry preview only)")
    args = parser.parse_args()
    run_forecast_loop(live=args.live)