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
import os
import datetime as dt
from pathlib import Path

import poly_discovery as disco
import poly_batch_forecast as bf
import poly_open_positions as op
import poly_resolve_positions as rp
import poly_alerts as alerts


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
    print(f"  New-discovery default (--new-count if not passed): {bf.NEW_COUNT_DEFAULT} "
          f"(was DAILY_EVENT_CAP -- renamed 2026-07-21, no longer a shared cap)")
    print(f"  Total tracked-event cap: {bf.TOTAL_FORECAST_CAP} — ROLLING as of 2026-07-22, "
          f"counts only ACTIVE (not-yet-closed) tracked events; once a tracked event's end_date "
          f"passes it stops counting and frees a slot automatically. --new-count gets silently "
          f"clamped (logged, not an error) once active count hits the cap; --refresh-count is unaffected")
    print(f"  Refresh ranking: blended rank-sum of soonest-to-close + oldest-last-forecast "
          f"(neither dominates alone — see select_candidates() docstring)")
    print(f"  New-discovery ranking: priority tag first, then soonest-to-close, THEN volume "
          f"(TTL beats volume — deliberate, so a one-shot forecast doesn't go stale long before "
          f"its market resolves)")
    print(f"  Tavily credit ceiling/run: {bf.TAVILY_CREDIT_CEILING} "
          f"(of 1,000/month free — research source as of 2026-07-20, was OpenRouter/Gemini before)")
    print(f"  Anthropic spend ceiling/run: ${bf.ANTHROPIC_SPEND_CEILING:.2f} "
          f"(stated worst-case budget was $0.50/day — same reasoning)")
    print(f"  --refresh-count events BYPASS both ceilings entirely (explicit request, honored in "
          f"full) — --new-count events respect both ceilings normally")
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
    print(f"  Placeholder statuses: backfill_no_position (never refreshed since backfill) vs. "
          f"backfill_no_edge (refreshed at least once, still no trade-worthy edge) — BOTH stay "
          f"eligible for future re-evaluation on later refreshes, neither is a dead end")
    print(f"  poly_resolve_positions.py resolution check: {rp.GAMMA_MARKETS_URL} "
          f"— CONFIRMED working against the live API as of 2026-07-21 (ran inside the GitHub "
          f"Actions workflow, no connection errors; still hasn't hit an actually-resolved market "
          f"yet, so the WIN/LOSS settlement path itself remains unexercised in production)")

    print("\n-- Alerts (poly_alerts.py) --")
    topic_set = bool(os.environ.get("ALERT_NTFY_TOPIC"))
    print(f"  ALERT_NTFY_TOPIC set in this environment: {topic_set} "
          f"(never print the actual value — treat it like a password)")
    print(f"  Sends one tally alert per LIVE run only (dry-runs stay silent): "
          f"refreshed/new/total counts, Tavily credits, Anthropic spend, and the stop reason "
          f"if the run was cut short")
    print(f"  ASCII-only HTTP header requirement for the Title field — ntfy/requests will crash "
          f"on emoji or smart-quotes in an un-sanitized title (see _ascii_safe_title, and the "
          f"known quirk below about verifying replace() pairs are genuinely distinct characters)")


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

        # Resolved-position-aware (2026-07-23 fix, same as poly_dashboard.py
        # and poly_batch_forecast.py's own safety net) -- a calendar-only
        # check calls a permanently-closed event "eligible" forever, even
        # though it will never actually get refreshed again. Confirmed in
        # production with two World Cup markets that sat "eligible" here
        # while the batch script found zero refresh candidates for the
        # same underlying reason.
        RESOLVED_STATUSES = {"resolved_win", "resolved_loss", "resolved_no_position"}
        positions = _load_json(op.POSITIONS_FILE)
        if not isinstance(positions, list):
            positions = []  # missing, corrupt (returns a string), or empty -- treat as no positions
        resolved_event_ids = {p["event_id"] for p in positions if p.get("status") in RESOLVED_STATUSES}

        eligible_for_refresh = 0
        for event_id, h in history.items():
            if event_id in resolved_event_ids:
                continue
            last = h.get("last_forecast_at")
            if last:
                hours_since = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600
                if hours_since >= bf.REFRESH_GATE_HOURS:
                    eligible_for_refresh += 1
        print(f"  Currently eligible for refresh (past the {bf.REFRESH_GATE_HOURS}h gate, "
              f"excludes resolved): {eligible_for_refresh}")

    if log:
        legacy_or_records = [r for r in log if "openrouter_cost" in r]
        tavily_records = [r for r in log if "tavily_credits_used" in r]
        total_or_cost = sum(r.get("openrouter_cost", 0) for r in legacy_or_records)
        total_tavily_credits = sum(r.get("tavily_credits_used", 0) for r in tavily_records)
        total_anthropic_cost = sum(r.get("anthropic_cost", 0) for r in log)
        measured_count = sum(1 for r in legacy_or_records if r.get("openrouter_cost_measured"))

        # Category (2026-07-23): replaces the dead priority/floor split, which
        # was structurally guaranteed to show ~0% floor forever (new-discovery
        # always ranks priority first against a permanent backlog). Older
        # records still say "priority"/"floor" -- bucketed into "other" here,
        # same as the dashboard does, since they don't carry real category info.
        category_counts: dict[str, int] = {}
        for r in log:
            cat = r.get("category", "other")
            if cat in ("priority", "floor"):
                cat = "other"
            category_counts[cat] = category_counts.get(cat, 0) + 1

        print(f"  Total forecast events logged: {len(log)}")
        print(f"  Category breakdown: " +
              ", ".join(f"{cat} {count}" for cat, count in sorted(category_counts.items())))
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
    print_section("WORKFLOW (as of 2026-07-21 — mostly server-side now, not a local step-by-step)")
    print("""
  GitHub Actions ("Poly Batch Forecast" workflow) does ALL of:
    1. python poly_batch_forecast.py --live --refresh-count N --new-count N
    2. python poly_open_positions.py     (only runs if live=true)
    3. python poly_resolve_positions.py  (only runs if live=true)
    4. commits forecast_history.json, forecasts_log.jsonl, paper_positions.json,
       paper_balance.json back to the repo

  This moved server-side specifically because gamma-api.polymarket.com is
  unreachable from Mike's AU connection (confirmed via repeated local
  connection timeouts, 2026-07-21) — same root cause as the earlier
  poly_discovery.py geo-block finding, just discovered again independently
  when poly_resolve_positions.py was first pointed at a real open position.
  GitHub's US-hosted runners are unaffected.

  LOCAL, via poly_update.bat:
    1. git pull
    2. python poly_dashboard.py

  That's it now — poly_update.bat no longer touches positions or calls any
  Polymarket API; it's purely pull-then-render.

  workflow_dispatch inputs and their YAML defaults:
    live: false (safety default for manual UI clicks — must be explicitly
          set true, whether by hand or in a cron payload)
    refresh_count: "5"
    new_count: "5"
  (Chosen for the daily cron plan below — override per-run via the GitHub
  UI form, or via the cron payload's "inputs" object, for anything
  different, e.g. the one-off 19-event legacy catch-up used --refresh-count 19.)

  CRON-JOB.ORG (to be set up within 24h of 2026-07-21, once/day):
    POST https://api.github.com/repos/mikejisrael/polymarket_bot/actions/workflows/poly_batch_forecast.yml/dispatches
    Headers: Authorization: Bearer <PAT>, Accept: application/vnd.github+json,
             Content-Type: application/json
    Body: {"ref": "main", "inputs": {"live": "true"}}
  refresh_count/new_count deliberately omitted from that body — GitHub falls
  back to the YAML defaults (5/5) for any input not explicitly included in a
  workflow_dispatch API request. Confirmed directly from GitHub's own docs,
  not assumed.
""")


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
        "All workflows are currently workflow_dispatch (manual) only — a daily cron-job.org "
        "trigger for poly_batch_forecast.yml is PLANNED (as of 2026-07-21, within 24h) but not "
        "yet confirmed live. See the exact payload in show_workflow() above once it's set up — "
        "check back here for whether it's actually running before assuming it is.",
        "Windows write_text() defaults to the cp1252 locale encoding, not UTF-8 — any file "
        "with non-ASCII characters (e.g. the ⚠ warning glyph in poly_dashboard.py's template) "
        "will crash with UnicodeEncodeError unless encoding='utf-8' is passed explicitly. Fixed "
        "in poly_dashboard.py, poly_open_positions.py, poly_resolve_positions.py — apply the "
        "same fix to any new file-writing code in this repo (same spirit as the existing "
        "newline='\\n' line-ending rule).",
        "gamma-api.polymarket.com is ALSO geo-blocked from Mike's AU connection, not just the "
        "main Polymarket site used by poly_discovery.py — poly_resolve_positions.py timed out "
        "on every local attempt (2026-07-21) until moved into the GitHub Actions workflow, "
        "where it worked immediately with zero connection errors. If any FUTURE script needs "
        "to hit gamma-api.polymarket.com (or any polymarket.com subdomain), assume it needs to "
        "run server-side too — don't rediscover this the hard way a third time.",
        "cmd.exe's batch-file parser breaks on parentheses inside echo text located anywhere "
        "near a multi-line if-block — even inside a REM comment, and even when the parens "
        "aren't literally inside the if block's own body — producing a cryptic "
        "\"X was unexpected at this time\" error at the point the block actually executes, not "
        "at parse time. Bit poly_update.bat twice (once inside an if-block's echo, once in a "
        "top-level REM comment). Fix: avoid parentheses in .bat file comments/echo text "
        "entirely, use plain punctuation instead (e.g. \"--\" instead of parenthetical asides).",
        "Python dict LITERALS used as a lookup table (e.g. "
        "`{\"a\": f\"...\", \"b\": f\"...\"}.get(key)`) evaluate ALL their f-string values "
        "immediately at construction time, not lazily per the key that ends up selected. Crashed "
        "poly_dashboard.py in production: an f-string for the \"resolved_win\" branch referenced "
        "position['pnl_usd'], which is legitimately None for \"open\" positions — but since ALL "
        "branches evaluate regardless of which one .get() picks, an \"open\" position crashed on "
        "a branch it was never going to use. Never caught in testing because no real \"open\" "
        "position existed yet at the time. Fixed with an if/elif chain instead (branches only "
        "evaluate when actually taken). Watch for this pattern anywhere a dict-of-f-strings is "
        "used as a status-to-label lookup.",
        "A status-check written as `!= \"specific_status\"` to mean \"already handled, skip\" is "
        "fragile the moment a NEW status value gets introduced — it silently starts treating the "
        "new status as \"already handled\" too, even if that wasn't the intent. Bit "
        "poly_open_positions.py: adding \"backfill_no_edge\" (to distinguish \"refreshed, still "
        "no edge\" from \"never refreshed\") accidentally got caught by an existing "
        "`status != \"backfill_no_position\"` check meant to mean \"real position, don't touch\" "
        "— froze every backfill_no_edge event from ever being re-evaluated again, the same class "
        "of bug the status was added to FIX. Fixed by switching to an explicit allowlist tuple "
        "of placeholder statuses instead of a negative check against one literal. General lesson: "
        "prefer `status in (explicit, allowed, set)` over `status != one_thing` whenever more "
        "status values might get added later.",
        "poly_alerts.py's _ascii_safe_title() replaces smart quotes/em-dashes with plain ASCII "
        "equivalents via .replace() pairs — when first written, a copy-pasted smart-quote "
        "character was accidentally duplicated on both sides of one replace() call, making it a "
        "silent no-op (the apostrophe in \"It's\" vanished instead of converting, since it fell "
        "through to the final ascii-encode-and-drop step instead). Fixed by using explicit "
        "\\uXXXX escapes instead of pasted Unicode glyphs in the source. If this function is ever "
        "edited again, verify with a real test string containing each character being replaced — "
        "don't just eyeball the replace() pairs, since visually-similar smart quotes are exactly "
        "the kind of thing that's easy to accidentally duplicate.",
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