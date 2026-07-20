"""
poly_status.py

Crib sheet for the whole polymarket_bot project. Run this any time you (or a
fresh Claude session) need to get oriented fast instead of re-deriving config
values or re-reading five conversations of context.

Design principle: every config value shown here is IMPORTED from the actual
modules (poly_discovery.py, poly_batch_forecast.py), never re-typed as a
separate constant in this file. If a threshold changes there, this crib sheet
updates automatically instead of quietly drifting out of sync — the exact
failure mode this script exists to prevent.

No API keys required, no paid calls made. Reads local state files only;
gracefully reports what's missing rather than erroring, since a fresh clone
or a machine that hasn't pulled the latest committed state won't have
everything yet.
"""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path

import poly_discovery as disco
import poly_batch_forecast as bf
import poly_open_positions as op
import poly_resolve_positions as rp


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return "CORRUPT — file exists but isn't valid JSON, worth investigating"


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


def print_section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def show_config() -> None:
    print_section("CONFIGURATION (live-imported from source — never manually re-typed here)")

    print("\n-- Coverage rules (poly_discovery.py) --")
    print(f"  Liquidity/volume floor: ${disco.COVERAGE_LIQUIDITY_MIN:,.0f} / ${disco.COVERAGE_VOLUME_MIN:,.0f}")
    print(f"  Priority tags (unconditional): {sorted(disco.PRIORITY_TAG_SLUGS)}")
    print(f"  Sports priority tags (conditional on NOT game-tied by slug date): {sorted(disco.SPORTS_TAG_SLUGS)}")
    print(f"  Noise tags (hard-excluded): {sorted(disco.NOISE_TAG_SLUGS)}")
    print(f"  Stale-expired exclusion: any market with days_to_end < 0 "
          f"(deadline passed, Polymarket still shows it open)")
    print(f"  NegRisk sum-check tolerance: {disco.NEG_RISK_SUM_TOLERANCE}")

    print("\n-- Batch forecast throttle (poly_batch_forecast.py) --")
    print(f"  Daily event cap: {bf.DAILY_EVENT_CAP}")
    print(f"  Tavily credit ceiling/run: {bf.TAVILY_CREDIT_CEILING} "
          f"(of 1,000/month free — research source as of 2026-07-20, was OpenRouter/Gemini before)")
    print(f"  Anthropic spend ceiling/run: ${bf.ANTHROPIC_SPEND_CEILING:.2f} "
          f"(stated worst-case budget was $0.50/day — same reasoning)")
    print(f"  Refresh gate: {bf.REFRESH_GATE_HOURS}h (won't re-forecast same event within this window)")
    print(f"  Models: {bf.ANTHROPIC_MODEL} (reasoning+verification), Tavily search depth={bf.TAVILY_SEARCH_DEPTH!r} (research)")

    print("\n-- Pricing basis (Anthropic side from published rates, verified 2026-07-08) --")
    print(f"  Anthropic Haiku 4.5: ${bf.ANTHROPIC_HAIKU_INPUT_PER_MTOK}/${bf.ANTHROPIC_HAIKU_OUTPUT_PER_MTOK} per MTok in/out")
    print(f"  Tavily: {bf.TAVILY_CREDITS_PER_REQUEST} credit(s)/search at depth={bf.TAVILY_SEARCH_DEPTH!r} "
          f"— free tier is 1,000 credits/month, recurring (confirmed from Tavily's own docs, 2026-07-20)")

    print("\n-- Paper trading (poly_open_positions.py / poly_resolve_positions.py) --")
    print(f"  Position sizing: {op.SIZE_PCT:.0%} of current paper balance per position")
    print(f"  Minimum edge to open a position: {op.EDGE_THRESHOLD}")
    print(f"  Only trades forecasts with probability_extraction_method == 'explicit' "
          f"(skips 'legacy' — unreliable probability capture)")
    print(f"  poly_resolve_positions.py resolution check: {rp.GAMMA_MARKETS_URL} "
          f"— UNTESTED against live API as of this writing, verify before trusting")


def show_coverage() -> None:
    print_section("LAST KNOWN COVERAGE (from poly_state/coverage_report.json, if present)")
    report = _load_json(disco.REPORT_FILE)
    if report is None:
        print("  No coverage_report.json found locally. Either no discovery run has been "
              "committed back to the repo yet, or you haven't pulled it. This file is written "
              "on every poly_discovery.py run (dry-run or not) but only PERSISTS if committed —"
              "worth checking whether the discovery workflow commits its state, since the cost "
              "pilot / connectivity test workflows currently don't.")
        return
    if isinstance(report, str):
        print(f"  {report}")
        return
    print(f"  Generated at: {report.get('generated_at')}")
    print(f"  Total events fetched: {report.get('total_events_fetched'):,}")
    print(f"  Total markets seen: {report.get('total_markets_seen'):,}")
    print(f"  Markets covered: {report.get('markets_covered'):,} "
          f"(via priority tag: {report.get('markets_covered_via_priority_tag', 0):,}, "
          f"via sports-futures: {report.get('markets_covered_via_sports_futures_priority', 0):,})")
    print(f"  Noise-excluded: {report.get('markets_noise_excluded', 0):,}")
    print(f"  Stale-expired-excluded: {report.get('markets_stale_expired_excluded', 0):,}")
    pagination = report.get("pagination", {})
    if pagination.get("hit_boundary"):
        keyset = pagination.get("keyset_continuation", {})
        bug = keyset.get("cursor_bug_suspected")
        print(f"  Pagination: hit offset ceiling, keyset continuation added "
              f"{keyset.get('new_events_found', '?')} more events "
              f"(cursor bug suspected: {bug})")


def show_forecast_activity() -> None:
    print_section("FORECAST ACTIVITY TO DATE (from poly_state/forecast_history.json + forecasts_log.jsonl)")
    history = _load_json(bf.HISTORY_FILE)
    log = _load_jsonl(bf.FORECASTS_LOG_FILE)

    if not history and not log:
        print("  No forecast runs recorded yet locally. Run poly_batch_forecast.py --live "
              "at least once, and make sure its workflow's commit-state step has run, or "
              "pull the latest committed state.")
        return

    if isinstance(history, dict):
        print(f"  Distinct events with forecast history: {len(history)}")
        now = dt.datetime.now(dt.timezone.utc)
        eligible_for_refresh = 0
        for event_id, h in history.items():
            last = h.get("last_forecast_at")
            if last:
                hours_since = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600
                if hours_since >= bf.REFRESH_GATE_HOURS:
                    eligible_for_refresh += 1
        print(f"  Currently eligible for refresh (past the {bf.REFRESH_GATE_HOURS}h gate): {eligible_for_refresh}")

    if log:
        legacy_or_records = [r for r in log if "openrouter_cost" in r]
        tavily_records = [r for r in log if "tavily_credits_used" in r]
        total_or_cost = sum(r.get("openrouter_cost", 0) for r in legacy_or_records)
        total_tavily_credits = sum(r.get("tavily_credits_used", 0) for r in tavily_records)
        total_anthropic_cost = sum(r.get("anthropic_cost", 0) for r in log)
        measured_count = sum(1 for r in legacy_or_records if r.get("openrouter_cost_measured"))
        priority_count = sum(1 for r in log if r.get("category") == "priority")
        print(f"  Total forecast events logged: {len(log)}")
        print(f"  Priority vs floor: {priority_count} / {len(log) - priority_count}")
        if legacy_or_records:
            print(f"  Legacy OpenRouter spend ({len(legacy_or_records)} events, pre-2026-07-20): "
                  f"${total_or_cost:.4f} ({measured_count}/{len(legacy_or_records)} measured, rest floor-estimated)")
        if tavily_records:
            print(f"  Tavily credits used ({len(tavily_records)} events, 2026-07-20 onward): "
                  f"{total_tavily_credits} of 1,000/month free")
        print(f"  Cumulative Anthropic spend: ${total_anthropic_cost:.4f} "
              f"(NOTE: not combined with OpenRouter/Tavily above — different units, "
              f"and OpenRouter is a dead research source now, not an ongoing cost)")
        most_recent = max(log, key=lambda r: r.get("timestamp", ""))
        print(f"  Most recent forecast: {most_recent.get('event_slug')} at {most_recent.get('timestamp')}")


def show_paper_trading() -> None:
    print_section("PAPER TRADING STATUS (from poly_state/paper_balance.json + paper_positions.json)")
    balance_data = _load_json(op.BALANCE_FILE)
    positions = _load_json(op.POSITIONS_FILE)

    if balance_data is None:
        print("  No paper_balance.json found locally — paper trading hasn't been initialized "
              "here yet, or you haven't pulled the latest committed state.")
        return
    if isinstance(balance_data, str):
        print(f"  {balance_data}")
        return

    print(f"  Balance: ${balance_data.get('balance', 0):.2f} "
          f"(started at ${balance_data.get('starting_balance', 0):.2f})")
    print(f"  Realized P&L: ${balance_data.get('realized_pnl', 0):+.2f}")
    print(f"  Last updated: {balance_data.get('last_updated') or '(never — no resolution run yet)'}")

    if not positions:
        print("  No paper_positions.json found, or it's empty.")
        return
    if isinstance(positions, str):
        print(f"  {positions}")
        return

    by_status: dict[str, int] = {}
    for p in positions:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    print(f"  Total position records: {len(positions)}")
    for status, count in sorted(by_status.items()):
        print(f"    {status}: {count}")


def show_workflow() -> None:
    print_section("PAPER TRADING WORKFLOW (run in this order after each forecast batch)")
    steps = [
        "python poly_batch_forecast.py --live   (or your usual forecast run)",
        "python poly_open_positions.py          (opens real paper positions on new forecasts; "
        "pre-existing forecasts that predate paper trading get a $0 placeholder instead — see "
        "backfill_skip_ids.json)",
        "python poly_resolve_positions.py       (checks Gamma API for resolution on anything "
        "past its end_date, settles P&L, updates the balance — UNTESTED against live API, "
        "verify before trusting)",
        "python poly_dashboard.py               (regenerates poly_dashboard.html + "
        "poly_dashboard_details/ — static files, no server; open poly_dashboard.html directly)",
    ]
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")
    print("\n  All four are manual (workflow_dispatch only) — nothing here is on a cron yet.")


def show_known_quirks() -> None:
    print_section("KNOWN QUIRKS / GOTCHAS (institutional memory — read before debugging from scratch)")
    quirks = [
        "Polymarket blocked in Australia at ISP level (ACMA order, Aug 2025) — affects your home "
        "connection only. GitHub Actions runners (US-hosted) are unaffected — confirmed empirically.",
        "Gamma /events silently caps effective page size at 100 regardless of requested `limit`.",
        "Offset-based pagination on /events hits a hard ceiling around offset=2100 (HTTP 422, "
        "\"use /events/keyset\"). Keyset continuation handles this — see fetch_events_keyset_continuation().",
        "/events/keyset has a REPORTED (not necessarily current) cursor-not-advancing bug "
        "(Polymarket/agents#227). We detect the symptom (duplicate page / static cursor) and "
        "stop cleanly rather than assume it's fixed or loop forever.",
        "Market-level `gameId` field is NOT reliably populated (confirmed: only ~3% of sports "
        "markets have it) — game-tied vs. futures-like sports classification uses slug-date "
        "detection instead (_slug_looks_game_tied), not gameId.",
        "Some markets sit past their real-world deadline while still flagged active/open on "
        "Polymarket's side (e.g. 2025-dated questions still open in mid-2026) — excluded via "
        "the stale_expired coverage check, not just relying on Polymarket's own active/closed flags.",
        "negRisk sum-to-~1 check is only meaningful for genuine categorical partitions (election "
        "winner, inflation bucket) — sports prop/exact-score groups are independent bets bundled "
        "for capital efficiency, NOT a probability partition, and are excluded from this check.",
        "[HISTORICAL — no longer the research source as of 2026-07-20] OpenRouter Gemini "
        ":online grounding cost was measured at ~$0.005/request in practice — "
        "do NOT plan budgets off the $0.035/request published Google rate, it's ~7x too high "
        "for whatever OpenRouter's actual implementation here is.",
        "poly_batch_forecast.py v1 forecasts only the single highest-volume market per grouped "
        "event, not the full outcome distribution — a known simplification, not a bug.",
        "Probability extraction from reasoning text is free-text regex, not structured output — "
        "can misparse (e.g. grabs a dismissed number mentioned earlier in the text). Fine for "
        "proving the loop works; not yet reliable enough to trust for real analysis.",
        "All workflows are currently workflow_dispatch (manual) only — not yet wired to "
        "cron-job.org for scheduled runs.",
        "Windows write_text() defaults to the cp1252 locale encoding, not UTF-8 — any file "
        "with non-ASCII characters (e.g. the ⚠ warning glyph in poly_dashboard.py's template) "
        "will crash with UnicodeEncodeError unless encoding='utf-8' is passed explicitly. Fixed "
        "in poly_dashboard.py, poly_open_positions.py, poly_resolve_positions.py — apply the "
        "same fix to any new file-writing code in this repo (same spirit as the existing "
        "newline='\\n' line-ending rule).",
    ]
    for i, q in enumerate(quirks, 1):
        print(f"  {i}. {q}")


if __name__ == "__main__":
    print(f"poly_status.py — generated {dt.datetime.now(dt.timezone.utc).isoformat()}")
    show_config()
    show_coverage()
    show_forecast_activity()
    show_paper_trading()
    show_workflow()
    show_known_quirks()
    print()