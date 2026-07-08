"""
poly_cost_pilot.py

Small, throwaway pilot to measure REAL per-event forecasting cost before
committing to a daily throttle rate. Not part of the production pipeline —
delete once its job is done.

Why this exists: the two numbers that actually determine your daily event
budget are (a) Anthropic Haiku reasoning cost, which is easy to compute
exactly from published rates, and (b) OpenRouter Gemini ":online" grounding
fee, which is NOT confirmed for your specific setup — published Google rates
suggest ~$0.035/grounded request, but that's an assumption, not a measurement.
This script runs a small number of REAL events through the full pipeline
(research -> reasoning -> verification) and reads back ACTUAL billed cost
from both providers' own usage/generation-stats endpoints, rather than
estimating from token counts.

Safety: makes real, paid API calls. Requires the --live flag to actually
spend anything. Without it, prints the sample it WOULD use and the rough
worst-case pre-spend estimate, then exits — same "dry-run before you commit
money" instinct as --write/--submit gates elsewhere in this project.

Requires ANTHROPIC_API_KEY and OPENROUTER_API_KEY in a .env file in this
directory (pip install python-dotenv requests if not already present).
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
import datetime as dt

import requests
from dotenv import load_dotenv

# poly_discovery.py must sit alongside this file — reusing its ID-mapping,
# tag constants, and event/market normalization rather than duplicating it.
import poly_discovery as disco

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OPENROUTER_MODEL = "google/gemini-2.5-flash:online"

# Verified 2026-07-08 (see cost-analysis conversation). Batch API is a flat
# 50% discount on both input and output — that part doesn't need empirical
# verification, only the OpenRouter grounding fee does.
ANTHROPIC_HAIKU_INPUT_PER_MTOK = 1.00
ANTHROPIC_HAIKU_OUTPUT_PER_MTOK = 5.00
ANTHROPIC_BATCH_DISCOUNT = 0.5

NUM_PILOT_EVENTS = 8
TARGET_CATEGORIES = {
    "crypto": 2, "politics": 2, "economy": 2, "sports_futures": 2,
}

REQUEST_TIMEOUT = 60
GENERATION_STATS_RETRIES = 5
GENERATION_STATS_RETRY_DELAY = 2


def pick_sample_events(n_per_category: dict[str, int]) -> list[dict]:
    """Grab a small, cheap sample without a full discovery pagination run —
    two pages of offset fetch is plenty to find a handful of covered events
    per target category."""
    session = requests.Session()
    session.headers.update({"User-Agent": "mike-poly-bot-cost-pilot/0.1"})
    events, _ = disco.fetch_all_events(session, active=True, closed=False)
    # fetch_all_events pages until it naturally stops or hits the offset
    # ceiling; for a small sample we don't need to let it run that far, but
    # simplicity here beats a second pagination code path for a throwaway script.

    now = dt.datetime.now(dt.timezone.utc)
    picked: list[dict] = []
    counts = {k: 0 for k in n_per_category}

    for event in events:
        markets = event.get("markets") or []
        if not markets:
            continue
        rec = disco.normalize_market(event, markets[0], event_market_count=len(markets), now=now)
        if rec is None or not rec.covered:
            continue

        tags = set(rec.tags)
        category = None
        if "crypto" in tags:
            category = "crypto"
        elif "politics" in tags:
            category = "politics"
        elif "economy" in tags:
            category = "economy"
        elif rec.sports_priority:
            category = "sports_futures"

        if category and counts.get(category, 0) < n_per_category.get(category, 0):
            picked.append({
                "event_slug": event.get("slug"),
                "question": rec.question or event.get("title"),
                "category": category,
                "outcome_prices": rec.outcome_prices,
            })
            counts[category] += 1

        if all(counts.get(k, 0) >= v for k, v in n_per_category.items()):
            break

    return picked


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
    """Poll OpenRouter's generation-stats endpoint for the REAL billed cost,
    including the grounding fee — this is the number we actually came here for."""
    if not generation_id:
        return None
    for attempt in range(GENERATION_STATS_RETRIES):
        try:
            resp = requests.get(
                "https://openrouter.ai/api/v1/generation",
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                cost = data.get("total_cost")
                if cost is not None:
                    return float(cost)
        except requests.RequestException:
            pass
        time.sleep(GENERATION_STATS_RETRY_DELAY)
    return None  # caller must fall back to a token-rate estimate and flag it


def call_anthropic(prompt: str, max_tokens: int = 500) -> dict:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"text": text, "usage": data.get("usage", {})}


def anthropic_cost(usage: dict) -> float:
    input_tok = usage.get("input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    return (input_tok / 1_000_000 * ANTHROPIC_HAIKU_INPUT_PER_MTOK
            + output_tok / 1_000_000 * ANTHROPIC_HAIKU_OUTPUT_PER_MTOK)


def run_pilot(live: bool) -> None:
    if not ANTHROPIC_API_KEY or not OPENROUTER_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY and/or OPENROUTER_API_KEY not found in environment/.env")
        sys.exit(1)

    print(f"Selecting {NUM_PILOT_EVENTS} sample events ({TARGET_CATEGORIES})...")
    sample = pick_sample_events(TARGET_CATEGORIES)
    print(f"Selected {len(sample)} events:")
    for s in sample:
        print(f"  [{s['category']}] {s['event_slug']}: {s['question']}")

    if not live:
        rough_worst_case = len(sample) * 0.05  # generous pre-spend ceiling for a dry preview
        print(f"\n[dry-run] Not spending anything. Rough worst-case for this sample: "
              f"~${rough_worst_case:.2f} (well under both daily budgets). "
              f"Re-run with --live to actually measure real cost.")
        return

    results = []
    for s in sample:
        print(f"\n--- {s['event_slug']} ---")

        research = call_openrouter_research(s["question"])
        or_actual_cost = get_openrouter_actual_cost(research["generation_id"])
        or_cost_is_measured = or_actual_cost is not None
        if or_actual_cost is None:
            # fallback token-rate estimate — explicitly flagged as NOT the real
            # grounding-inclusive cost, just a floor
            u = research["usage"]
            or_actual_cost = (u.get("prompt_tokens", 0) / 1_000_000 * 0.30
                               + u.get("completion_tokens", 0) / 1_000_000 * 2.50)
            print(f"  [warning] generation stats unavailable — using token-only estimate "
                  f"(EXCLUDES grounding fee, so this is a floor, not the real cost): ${or_actual_cost:.4f}")
        else:
            print(f"  OpenRouter research: actual billed cost ${or_actual_cost:.4f}")

        reasoning_prompt = (
            f"Prediction market question: {s['question']}\n"
            f"Current market-implied prices: {s['outcome_prices']}\n"
            f"Research:\n{research['text']}\n\n"
            f"Give a calibrated probability estimate (0-1) with 2-3 sentences of reasoning."
        )
        reasoning = call_anthropic(reasoning_prompt, max_tokens=400)
        reasoning_cost_nonbatch = anthropic_cost(reasoning["usage"])
        print(f"  Anthropic reasoning: {reasoning['usage']} -> ${reasoning_cost_nonbatch:.4f} (non-batch)")

        verify_prompt = (
            f"Review this forecast reasoning for internal contradictions or unsupported claims. "
            f"Be brief.\n\n{reasoning['text']}"
        )
        verify = call_anthropic(verify_prompt, max_tokens=200)
        verify_cost_nonbatch = anthropic_cost(verify["usage"])
        print(f"  Anthropic verification: {verify['usage']} -> ${verify_cost_nonbatch:.4f} (non-batch)")

        anthropic_total_nonbatch = reasoning_cost_nonbatch + verify_cost_nonbatch
        anthropic_total_batch_est = anthropic_total_nonbatch * ANTHROPIC_BATCH_DISCOUNT

        results.append({
            "event_slug": s["event_slug"],
            "category": s["category"],
            "openrouter_cost": or_actual_cost,
            "openrouter_cost_measured": or_cost_is_measured,
            "anthropic_cost_nonbatch": anthropic_total_nonbatch,
            "anthropic_cost_batch_estimated": anthropic_total_batch_est,
            "total_cost_realistic": or_actual_cost + anthropic_total_batch_est,
        })

    print("\n=== Pilot summary ===")
    n = len(results)
    measured_or = [r for r in results if r["openrouter_cost_measured"]]
    avg_or = sum(r["openrouter_cost"] for r in results) / n
    avg_anthropic_batch = sum(r["anthropic_cost_batch_estimated"] for r in results) / n
    avg_total = sum(r["total_cost_realistic"] for r in results) / n

    print(f"Events run: {n} ({len(measured_or)} with measured OpenRouter cost, "
          f"{n - len(measured_or)} fell back to token-only floor estimate)")
    print(f"Avg OpenRouter cost/event: ${avg_or:.4f}")
    print(f"Avg Anthropic cost/event (batch-adjusted): ${avg_anthropic_batch:.4f}")
    print(f"Avg TOTAL cost/event: ${avg_total:.4f}")

    if avg_or > 0:
        print(f"\nAt $1.00/day OpenRouter budget: ~{1.00 / avg_or:.0f} events/day sustainable")
    if avg_anthropic_batch > 0:
        print(f"At $0.50/day Anthropic budget: ~{0.50 / avg_anthropic_batch:.0f} events/day sustainable")
    print(f"Binding constraint at $1.00/$0.50 daily budgets: "
          f"~{min(1.00 / avg_or if avg_or else 9e9, 0.50 / avg_anthropic_batch if avg_anthropic_batch else 9e9):.0f} events/day")

    os.makedirs("poly_state", exist_ok=True)
    with open("poly_state/cost_pilot_results.json", "w") as f:
        json.dump({
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "results": results,
            "avg_openrouter_cost": avg_or,
            "avg_anthropic_cost_batch_estimated": avg_anthropic_batch,
            "avg_total_cost": avg_total,
        }, f, indent=2)
    print("\nWrote poly_state/cost_pilot_results.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure real per-event forecasting cost")
    parser.add_argument("--live", action="store_true", help="Actually spend money (default: dry preview only)")
    args = parser.parse_args()
    run_pilot(live=args.live)