"""
poly_alerts.py

Push notifications for the Polymarket bot, via ntfy.sh — same pattern as
the Metaculus bot's meta_alerts.py, deliberately kept consistent rather
than reinvented, including a fix that bit that module in production:

ntfy.sh: a free, no-signup-required push notification service. Pick a
private topic name (treat it like a password — anyone who knows it can
read your notifications), then either install the ntfy app (iOS/Android)
and subscribe to that topic, or just visit https://ntfy.sh/<topic> in a
browser to see alerts live.

Setup:
  1. Pick a topic name (can reuse the Metaculus bot's, or use a separate
     one — GitHub secrets don't carry across repos either way, so
     ALERT_NTFY_TOPIC needs adding as a repository secret HERE regardless).
  2. Add to .env for local runs:  ALERT_NTFY_TOPIC=<your-topic>
  3. Add the same value as a repository secret in polymarket_bot's GitHub
     settings, for the workflow to use.

If ALERT_NTFY_TOPIC isn't set, send_alert() is a silent no-op — nothing
breaks, you just don't get notifications until it's configured.

ASCII-header fix (carried over from meta_alerts.py, confirmed root cause
there via binwiederhier/ntfy#1410 and Sonarr/Sonarr#6679): HTTP header
VALUES must be ASCII-only. requests/urllib3 try to encode header values
as Latin-1 by default, which can't represent emoji or most non-ASCII
characters — a title with an emoji crashes the request entirely. This
restriction is HEADER-ONLY: the message body (sent via `data=`,
UTF-8-encoded) supports emoji/unicode fine; only the Title header needs
sanitizing.
"""

import os
import re
import requests


def _ascii_safe_title(title: str) -> str:
    """Make a title safe for an HTTP header: substitute common
    typographic characters with plain ASCII equivalents first (so
    em-dashes and smart quotes degrade gracefully instead of just
    vanishing), then drop anything still non-ASCII (emoji, etc.) rather
    than letting the request crash. Never touches the message body."""
    title = (title
             .replace("\u2014", "-").replace("\u2013", "-")     # em dash, en dash
             .replace("\u2019", "'").replace("\u2018", "'")     # right/left single quote
             .replace("\u201c", '"').replace("\u201d", '"'))    # left/right double quote
    title = title.encode("ascii", errors="ignore").decode("ascii")
    title = re.sub(r"\s+", " ", title).strip()
    return title or "Polymarket Bot"


def send_alert(message: str, title: str = "Polymarket Bot") -> bool:
    """Returns True only if the alert was actually sent successfully.
    Alerts are a nice-to-have — never let a notification failure break
    or slow down an actual forecast run; failures are caught and printed,
    not raised."""
    topic = os.getenv("ALERT_NTFY_TOPIC")
    if not topic:
        return False  # not configured — silent no-op, never blocks the main flow
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": _ascii_safe_title(title)},
            timeout=5,
        )
        if resp.status_code == 200:
            return True
        print(f"  [alert] ntfy returned {resp.status_code}, not treating as sent")
        return False
    except requests.RequestException as e:
        print(f"  [alert] failed to send (non-fatal): {e}")
        return False
