"""
poly_dashboard.py

Static HTML dashboard generator for the polymarket_bot project. Reads
poly_state/*.json(l) and writes out poly_dashboard.html (plus one detail
page per forecast under poly_dashboard_details/) — no server, nothing to
keep running. Since positions/resolution are run manually in batches
anyway, regenerate this after each run: open positions -> resolve
positions -> generate dashboard -> open the html file.

Run: python poly_dashboard.py
Then open poly_dashboard.html in a browser.

v1 scope: no calibration view. Calibration (predicted vs. actual) needs
resolved outcomes, and the current forecast set skews long-horizon
(futures-like markets, by design — see the sports/crypto prioritization
work) so there's nothing to show yet. This is a "forecasts made + spend
to date, plus paper P&L" view for now; calibration is a natural v2 once
more markets resolve.

Accessibility note: Mike is red-green colorblind (same constraint already
applied in the ByBit dashboard). Edge direction and P&L both use
blue/amber, not red/green.
"""

import json
import datetime as dt
from pathlib import Path

from jinja2 import Template

STATE_DIR = Path("poly_state")
FORECASTS_LOG_FILE = STATE_DIR / "forecasts_log.jsonl"
HISTORY_FILE = STATE_DIR / "forecast_history.json"
COVERAGE_REPORT_FILE = STATE_DIR / "coverage_report.json"
POSITIONS_FILE = STATE_DIR / "paper_positions.json"
BALANCE_FILE = STATE_DIR / "paper_balance.json"

OUTPUT_FILE = Path("poly_dashboard.html")
DETAILS_DIR = Path("poly_dashboard_details")


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


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


def _market_price(record: dict) -> float | None:
    prices = record.get("market_price_at_forecast")
    if not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError, IndexError):
        return None


def load_dashboard_data() -> dict:
    log = _load_jsonl(FORECASTS_LOG_FILE)
    history = _load_json(HISTORY_FILE) or {}
    coverage = _load_json(COVERAGE_REPORT_FILE)
    positions = _load_json(POSITIONS_FILE) or []
    balance_data = _load_json(BALANCE_FILE) or {
        "balance": 1000.0, "starting_balance": 1000.0, "realized_pnl": 0.0,
    }
    positions_by_event = {p["event_id"]: p for p in positions}

    forecasts = []
    total_or_cost = 0.0
    total_anthropic_cost = 0.0
    priority_count = 0

    for idx, r in enumerate(sorted(log, key=lambda x: x.get("timestamp", ""), reverse=True)):
        market_price = _market_price(r)
        probability = r.get("estimated_probability")
        edge = None
        if market_price is not None and probability is not None:
            edge = round(probability - market_price, 3)

        or_cost = r.get("openrouter_cost", 0) or 0
        anthropic_cost = r.get("anthropic_cost", 0) or 0
        total_or_cost += or_cost
        total_anthropic_cost += anthropic_cost
        if r.get("category") == "priority":
            priority_count += 1

        # Older records predate both fields — treat missing extraction_method
        # as "legacy" (captured before the fix, don't claim it's reliable)
        # rather than pretending it's "explicit".
        extraction_method = r.get("probability_extraction_method", "legacy")

        end_date_raw = r.get("end_date")
        close_display = "unknown"
        if end_date_raw:
            try:
                close_display = dt.datetime.fromisoformat(
                    end_date_raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                close_display = "unknown"

        position = positions_by_event.get(r.get("event_id"))
        if position is None:
            position_status, position_label = "none", "no position"
        else:
            position_status = position["status"]
            position_label = {
                "open": f"open · {position['direction']} @ {position['entry_price']:.2f} · ${position['size_usd']:.2f}",
                "backfill_no_position": "no position (pre-dates tracking)",
                "resolved_no_position": "closed · $0 (pre-dates tracking)",
                "resolved_win": f"WIN · {position['direction']} · ${position['pnl_usd']:+.2f}",
                "resolved_loss": f"LOSS · {position['direction']} · ${position['pnl_usd']:+.2f}",
            }.get(position_status, position_status)

        forecasts.append({
            "idx": idx,
            "event_slug": r.get("event_slug", "?"),
            "question": r.get("question", ""),
            "category": r.get("category", "floor"),
            "market_price": market_price,
            "probability": probability,
            "extraction_method": extraction_method,
            "edge": edge,
            "cost": round(or_cost + anthropic_cost, 4),
            "cost_measured": r.get("openrouter_cost_measured", False),
            "timestamp": r.get("timestamp", ""),
            "close_display": close_display,
            "condition_id": r.get("condition_id", ""),
            "reasoning_text": r.get("reasoning_text", ""),
            "verification_text": r.get("verification_text", ""),
            "position_status": position_status,
            "position_label": position_label,
        })

    now = dt.datetime.now(dt.timezone.utc)
    refresh_gate_hours = 72
    eligible_for_refresh = 0
    for h in history.values():
        last = h.get("last_forecast_at")
        if last:
            hours_since = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600
            if hours_since >= refresh_gate_hours:
                eligible_for_refresh += 1

    open_positions = [p for p in positions if p["status"] == "open"]
    resolved_real = [p for p in positions if p["status"] in ("resolved_win", "resolved_loss")]
    wins = [p for p in resolved_real if p["status"] == "resolved_win"]

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "forecasts": forecasts,
        "total_forecasts": len(forecasts),
        "priority_count": priority_count,
        "floor_count": len(forecasts) - priority_count,
        "total_or_cost": round(total_or_cost, 4),
        "total_anthropic_cost": round(total_anthropic_cost, 4),
        "total_cost": round(total_or_cost + total_anthropic_cost, 4),
        "eligible_for_refresh": eligible_for_refresh,
        "history_count": len(history),
        "coverage": coverage,
        "balance": balance_data.get("balance", 1000.0),
        "starting_balance": balance_data.get("starting_balance", 1000.0),
        "realized_pnl": balance_data.get("realized_pnl", 0.0),
        "open_position_count": len(open_positions),
        "resolved_count": len(resolved_real),
        "win_count": len(wins),
        "win_rate": round(100 * len(wins) / len(resolved_real), 0) if resolved_real else None,
    }


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot — Status</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0B0E14;
    --panel: #131822;
    --panel-border: #1F2733;
    --text: #E7ECF3;
    --muted: #7C8798;
    --blue: #4C8DFF;
    --amber: #E0A339;
    --teal: #35C8B3;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    margin: 0;
    padding: 40px 32px 80px;
  }
  h1 {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 28px;
    margin: 0 0 4px;
    letter-spacing: -0.01em;
  }
  .subtitle {
    color: var(--muted);
    font-size: 13px;
    margin-bottom: 32px;
  }
  .subtitle .paper-badge {
    color: var(--amber);
    border: 1px solid var(--amber);
    border-radius: 3px;
    padding: 1px 6px;
    font-size: 11px;
    margin-left: 8px;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 36px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 16px 18px;
  }
  .card .eyebrow {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .card .value {
    font-size: 24px;
    font-weight: 600;
  }
  .card .detail {
    font-size: 12px;
    color: var(--muted);
    margin-top: 4px;
  }
  section {
    margin-bottom: 36px;
  }
  section h2 {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 15px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    border-bottom: 1px solid var(--panel-border);
    padding-bottom: 8px;
    margin-bottom: 16px;
  }
  .forecast-row {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 16px 18px;
    margin-bottom: 10px;
  }
  .forecast-top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 10px;
  }
  .forecast-question {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 14px;
    font-weight: 500;
    color: var(--text);
    text-decoration: none;
    border-bottom: 1px dotted var(--panel-border);
  }
  .forecast-question:hover {
    color: var(--blue);
    border-bottom-color: var(--blue);
  }
  .badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 7px;
    border-radius: 3px;
    white-space: nowrap;
  }
  .badge.priority { background: rgba(53, 200, 179, 0.15); color: var(--teal); }
  .badge.floor { background: rgba(124, 135, 152, 0.15); color: var(--muted); }
  .badge.pending { background: rgba(224, 163, 57, 0.15); color: var(--amber); border: 1px solid rgba(224, 163, 57, 0.3); }
  .badge.pos-open { background: rgba(76, 141, 255, 0.15); color: var(--blue); border: 1px solid rgba(76, 141, 255, 0.3); }
  .badge.pos-win { background: rgba(53, 200, 179, 0.15); color: var(--teal); border: 1px solid rgba(53, 200, 179, 0.3); }
  .badge.pos-loss { background: rgba(224, 163, 57, 0.15); color: var(--amber); border: 1px solid rgba(224, 163, 57, 0.3); }
  .badge.pos-none { background: rgba(124, 135, 152, 0.1); color: var(--muted); }
  .bars {
    display: grid;
    grid-template-columns: 100px 1fr 60px;
    align-items: center;
    gap: 10px;
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 4px;
  }
  .bar-track {
    position: relative;
    height: 6px;
    background: #1B222E;
    border-radius: 3px;
    overflow: visible;
  }
  .bar-fill {
    position: absolute;
    top: 0; left: 0; bottom: 0;
    border-radius: 3px;
  }
  .bar-fill.market { background: var(--muted); opacity: 0.5; }
  .bar-fill.estimate { background: var(--blue); }
  .bar-fill.estimate.bearish { background: var(--amber); }
  .bar-fill.estimate.neutral { background: var(--muted); }
  .bar-marker {
    position: absolute;
    top: -3px;
    width: 2px;
    height: 12px;
    background: var(--muted);
  }
  .meta-row {
    display: flex;
    gap: 18px;
    font-size: 11px;
    color: var(--muted);
    margin-top: 10px;
  }
  .meta-row .edge-positive { color: var(--blue); }
  .meta-row .edge-negative { color: var(--amber); }
  .empty-state {
    background: var(--panel);
    border: 1px dashed var(--panel-border);
    border-radius: 6px;
    padding: 32px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
  }
  .empty-state code {
    background: #1B222E;
    padding: 2px 6px;
    border-radius: 3px;
    color: var(--text);
  }
  .quirk-note {
    font-size: 12px;
    color: var(--muted);
    margin-top: 8px;
  }
</style>
</head>
<body>
  <h1>Polymarket Bot</h1>
  <div class="subtitle">
    Generated {{ data.generated_at }}
    <span class="paper-badge">PAPER TRADING ONLY</span>
  </div>

  <div class="cards">
    <div class="card">
      <div class="eyebrow">Forecasts made</div>
      <div class="value">{{ data.total_forecasts }}</div>
      <div class="detail">{{ data.priority_count }} priority / {{ data.floor_count }} floor</div>
    </div>
    <div class="card">
      <div class="eyebrow">Total spend</div>
      <div class="value">${{ "%.4f"|format(data.total_cost) }}</div>
      <div class="detail">OR ${{ "%.4f"|format(data.total_or_cost) }} · Anthropic ${{ "%.4f"|format(data.total_anthropic_cost) }}</div>
    </div>
    <div class="card">
      <div class="eyebrow">Eligible for refresh</div>
      <div class="value">{{ data.eligible_for_refresh }}</div>
      <div class="detail">of {{ data.history_count }} tracked (72h gate)</div>
    </div>
    {% if data.coverage %}
    <div class="card">
      <div class="eyebrow">Markets covered</div>
      <div class="value">{{ "{:,}".format(data.coverage.markets_covered) }}</div>
      <div class="detail">of {{ "{:,}".format(data.coverage.total_markets_seen) }} seen</div>
    </div>
    {% endif %}
    <div class="card">
      <div class="eyebrow">Paper balance</div>
      <div class="value" style="color: {% if data.realized_pnl > 0 %}var(--blue){% elif data.realized_pnl < 0 %}var(--amber){% else %}var(--text){% endif %};">${{ "%.2f"|format(data.balance) }}</div>
      <div class="detail">
        {{ "%+.2f"|format(data.realized_pnl) }} realized from ${{ "%.0f"|format(data.starting_balance) }} start
        · {{ data.open_position_count }} open
        {% if data.win_rate is not none %} · {{ data.win_count }}/{{ data.resolved_count }} won ({{ "%.0f"|format(data.win_rate) }}%){% endif %}
      </div>
    </div>
  </div>

  <section>
    <h2>Forecasts</h2>
    {% if not data.forecasts %}
    <div class="empty-state">
      No forecasts logged yet. Run <code>poly_batch_forecast.py --live</code> (or trigger the
      "Poly Batch Forecast" workflow) to generate the first batch.
    </div>
    {% else %}
      {% for f in data.forecasts %}
      <div class="forecast-row">
        <div class="forecast-top">
          <a class="forecast-question" href="poly_dashboard_details/{{ f.idx }}.html">{{ f.question }}</a>
          <div style="display:flex; gap:6px; align-items:center;">
            <span class="badge {{ f.category }}">{{ f.category }}</span>
            <span class="badge {% if f.position_status == 'open' %}pos-open{% elif f.position_status == 'resolved_win' %}pos-win{% elif f.position_status == 'resolved_loss' %}pos-loss{% else %}pos-none{% endif %}">{{ f.position_label }}</span>
          </div>
        </div>
        {% if f.market_price is not none and f.probability is not none %}
        <div class="bars">
          <span>MARKET</span>
          <div class="bar-track">
            <div class="bar-fill market" style="width: {{ (f.market_price * 100)|round(1) }}%"></div>
            <div class="bar-marker" style="left: {{ (f.probability * 100)|round(1) }}%"></div>
          </div>
          <span>{{ "%.0f"|format(f.market_price * 100) }}%</span>
        </div>
        <div class="bars">
          <span>BOT EST.{% if f.extraction_method != "explicit" %} ⚠{% endif %}</span>
          <div class="bar-track">
            <div class="bar-fill estimate {% if f.edge is not none and f.edge < 0 %}bearish{% elif f.edge is none %}neutral{% endif %}"
                 style="width: {{ (f.probability * 100)|round(1) }}%"></div>
          </div>
          <span>{{ "%.0f"|format(f.probability * 100) }}%</span>
        </div>
        {% if f.extraction_method != "explicit" %}
        <div class="quirk-note" style="margin-top:0; margin-bottom:6px;">
          ⚠ probability extraction: {{ f.extraction_method }}
          {% if f.extraction_method == "legacy" %}(recorded before the extraction fix — may be unreliable, check detail page){% endif %}
        </div>
        {% endif %}
        {% endif %}
        <div class="meta-row">
          <span>{{ f.event_slug }}</span>
          <span>closes {{ f.close_display }}</span>
          <span>{{ f.timestamp[:16] }}</span>
          <span>${{ "%.4f"|format(f.cost) }}{% if not f.cost_measured %} (est.){% endif %}</span>
          {% if f.edge is not none %}
            {% if f.edge > 0 %}
              <span class="edge-positive">edge +{{ "%.2f"|format(f.edge) }} (bot more bullish)</span>
            {% elif f.edge < 0 %}
              <span class="edge-negative">edge {{ "%.2f"|format(f.edge) }} (bot more bearish)</span>
            {% else %}
              <span>edge 0.00</span>
            {% endif %}
          {% endif %}
        </div>
      </div>
      {% endfor %}
    {% endif %}
  </section>

  {% if data.coverage %}
  <section>
    <h2>Last discovery run</h2>
    <div class="quirk-note">
      {{ data.coverage.generated_at }} — {{ "{:,}".format(data.coverage.total_events_fetched) }} events,
      {{ "{:,}".format(data.coverage.total_markets_seen) }} markets seen,
      {{ "{:,}".format(data.coverage.markets_noise_excluded) }} noise-excluded,
      {{ "{:,}".format(data.coverage.markets_stale_expired_excluded) }} stale-excluded
    </div>
  </section>
  {% endif %}

</body>
</html>
"""


DETAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ f.event_slug }} — Polymarket Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0B0E14; --panel: #131822; --panel-border: #1F2733;
    --text: #E7ECF3; --muted: #7C8798; --blue: #4C8DFF; --amber: #E0A339; --teal: #35C8B3;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: 'IBM Plex Mono', monospace;
         margin: 0; padding: 40px 32px 80px; max-width: 900px; }
  a.back { color: var(--muted); font-size: 12px; text-decoration: none; }
  a.back:hover { color: var(--blue); }
  h1 { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 22px;
       margin: 14px 0 4px; letter-spacing: -0.01em; }
  .meta-grid { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px;
               font-size: 12px; color: var(--muted); margin: 16px 0 28px; }
  .meta-grid .k { text-transform: uppercase; letter-spacing: 0.05em; }
  .meta-grid .v { color: var(--text); }
  section { margin-bottom: 28px; }
  section h2 { font-family: 'Space Grotesk', sans-serif; font-size: 13px; font-weight: 700;
               text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
               border-bottom: 1px solid var(--panel-border); padding-bottom: 8px; margin-bottom: 12px; }
  .text-block { background: var(--panel); border: 1px solid var(--panel-border); border-radius: 6px;
                padding: 16px 18px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }
  .warn-block { background: rgba(224, 163, 57, 0.08); border: 1px solid rgba(224, 163, 57, 0.3);
                border-radius: 6px; padding: 14px 16px; font-size: 12px; color: var(--amber); margin-bottom: 16px; }
</style>
</head>
<body>
  <a class="back" href="../poly_dashboard.html">&larr; back to dashboard</a>
  <h1>{{ f.question }}</h1>
  <div class="meta-grid">
    <div class="k">Event</div><div class="v">{{ f.event_slug }}</div>
    <div class="k">Condition ID</div><div class="v">{{ f.condition_id }}</div>
    <div class="k">Category</div><div class="v">{{ f.category }}</div>
    <div class="k">Forecast at</div><div class="v">{{ f.timestamp }}</div>
    <div class="k">Closes</div><div class="v">{{ f.close_display }}</div>
    <div class="k">Market price</div><div class="v">{{ "%.1f"|format(f.market_price * 100) if f.market_price is not none else "?" }}%</div>
    <div class="k">Bot estimate</div><div class="v">{{ "%.1f"|format(f.probability * 100) if f.probability is not none else "not captured" }}%</div>
    <div class="k">Extraction</div><div class="v">{{ f.extraction_method }}</div>
    <div class="k">Cost</div><div class="v">${{ "%.4f"|format(f.cost) }}{% if not f.cost_measured %} (est.){% endif %}</div>
  </div>

  {% if f.extraction_method != "explicit" %}
  <div class="warn-block">
    ⚠ This probability was not captured from an explicit "Probability: X" statement
    ({{ f.extraction_method }}). Treat it as unverified — read the reasoning below directly
    rather than trusting the number alone.
  </div>
  {% endif %}

  <section>
    <h2>Reasoning</h2>
    <div class="text-block">{{ f.reasoning_text or "(not recorded)" }}</div>
  </section>

  <section>
    <h2>Verification pass</h2>
    <div class="text-block">{{ f.verification_text or "(not recorded)" }}</div>
  </section>
</body>
</html>
"""


def main():
    data = load_dashboard_data()

    main_template = Template(TEMPLATE)
    OUTPUT_FILE.write_text(main_template.render(data=data), encoding="utf-8", newline="\n")

    detail_template = Template(DETAIL_TEMPLATE)
    DETAILS_DIR.mkdir(exist_ok=True)
    for f in data["forecasts"]:
        detail_path = DETAILS_DIR / f"{f['idx']}.html"
        detail_path.write_text(detail_template.render(f=f), encoding="utf-8", newline="\n")

    print(f"Wrote {OUTPUT_FILE} and {len(data['forecasts'])} detail page(s) to {DETAILS_DIR}/")
    print(f"Open {OUTPUT_FILE.resolve()} in a browser.")


if __name__ == "__main__":
    main()