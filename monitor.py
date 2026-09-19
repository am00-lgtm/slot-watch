#!/usr/bin/env python3
"""
H-1B US visa slot monitor - India consulates.

What it does:
  * Polls VisaGrader's PUBLIC India tracker page for newly reported H-1B
    slot sightings.
  * Compares against state.json; sends ONE Telegram message per run when new
    sightings appear (batched, capped). Otherwise stays silent.

Safety properties (auditable - please read before deploying):
  * Only performs HTTPS GET against two allow-listed hosts:
      visagrader.com, api.telegram.org
    It NEVER visits any login page or scheduling portal.
  * No credentials anywhere in code. The only secrets are read from
    environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), which in
    production come from GitHub Actions encrypted secrets - never committed.
  * Standard library only - no third-party packages to audit.
  * Polite: descriptive User-Agent, 10-minute schedule (not aggressive).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

ALLOWED_HOSTS = {"visagrader.com", "api.telegram.org"}

VISAGRADER_INDIA = "https://visagrader.com/us-visa-time-slots-availability/india-ind"

USER_AGENT = (
    "Mozilla/5.0 (compatible; H1BSlotMonitor/1.0; personal research monitor)"
)
TIMEOUT = 30
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
MAX_ALERT_ITEMS = 10
WATCH_VISA_TYPES = {"H-1B", "H1B"}


# ----------------------------------------------------------------------------
# Minimal HTML table extraction (stdlib only)
# ----------------------------------------------------------------------------

class TableExtractor(HTMLParser):
    """Extracts every <table> on a page as rows of cell text, tagged with the
    nearest preceding heading so we can find the right table."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._in_table = False
        self._current = None
        self._row = None
        self._in_cell = False
        self._cell_text = []
        self._in_heading = False
        self._heading_text = []
        self._last_heading = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4"):
            self._in_heading = True
            self._heading_text = []
        elif tag == "table":
            self._in_table = True
            self._current = {"heading": self._last_heading, "rows": []}
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._in_table and self._row is not None:
            self._in_cell = True
            self._cell_text = []

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4"):
            self._in_heading = False
            self._last_heading = "".join(self._heading_text).strip()
        elif tag == "table" and self._in_table:
            self._in_table = False
            self.tables.append(self._current)
            self._current = None
        elif tag == "tr" and self._in_table and self._row is not None:
            self._current["rows"].append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row.append("".join(self._cell_text).strip())

    def handle_data(self, data):
        if self._in_heading:
            self._heading_text.append(data)
        if self._in_cell:
            self._cell_text.append(data)


def fetch(url):
    """GET a URL, but only if its host is on the allow-list."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"refusing to fetch non-allow-listed host: {host}")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def find_table(tables, *header_keywords):
    """Return rows (excluding header) of the first table whose header row
    contains all keywords (case-insensitive)."""
    for table in tables:
        if not table["rows"]:
            continue
        header = " | ".join(table["rows"][0]).lower()
        if all(kw.lower() in header for kw in header_keywords):
            return table["rows"][1:]
    return []


# ----------------------------------------------------------------------------
# Source parsers -> normalized sightings
# ----------------------------------------------------------------------------

def parse_visagrader(html):
    """VisaGrader India page: 'Recent slot activity' table with columns
    Consulate | Type | Visa | Availability date | Count | Reported."""
    parser = TableExtractor()
    parser.feed(html)
    sightings = []
    for row in find_table(parser.tables, "Consulate", "Availability date"):
        if len(row) < 6:
            continue
        consulate, appt_type, visa, avail_date, count, reported = row[:6]
        if visa not in WATCH_VISA_TYPES or not avail_date:
            continue
        sightings.append({
            "source": "VisaGrader",
            "consulate": consulate,
            "appt_type": appt_type,
            "visa": visa,
            "date": avail_date,
            "count": count,
            "reported": reported,
            "key": "|".join(["visagrader", consulate, appt_type, visa, avail_date, count]),
            "label": f"{consulate} - {appt_type} - {avail_date} - {count} slot(s)",
        })
    return sightings


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)"
    host = "api.telegram.org"
    assert host in ALLOWED_HOSTS
    url = f"https://{host}/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            return "sent"
        return f"telegram API error: {body}"
    except Exception as exc:  # noqa: BLE001 - report, don't crash the run
        return f"telegram send failed: {exc}"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"seen": {}}


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    first_run = not os.path.exists(STATE_PATH)
    state = load_state() or {"seen": {}}
    seen = state.get("seen", {})

    current = {}   # key -> sighting
    errors = []

    try:
        current.update({s["key"]: s for s in parse_visagrader(fetch(VISAGRADER_INDIA))})
    except Exception as exc:  # noqa: BLE001 - report, don't crash the run
        errors.append(f"visagrader: {exc}")

    new_keys = [k for k in current if k not in seen]
    telegram_status = "no-alert"

    if first_run:
        # Baseline run: record everything silently so we don't spam on deploy.
        print("first run: saving baseline, no alerts sent")
    elif new_keys:
        items = [current[k]["label"] for k in new_keys[:MAX_ALERT_ITEMS]]
        extra = len(new_keys) - MAX_ALERT_ITEMS
        lines = "\n".join(f"- {item}" for item in items)
        if extra > 0:
            lines += f"\n- ...and {extra} more"
        sources = sorted({current[k]["source"] for k in new_keys})
        text = (
            "🔔 New H-1B slot sighting(s) - India\n\n"
            f"{lines}\n\n"
            f"Sources: {', '.join(sources)}\n"
            "Crowdsourced data - may already be gone. If you act: log in to the "
            "official visa scheduling portal yourself (keep page views low, "
            "one session, no VPN). Never share your login with anyone."
        )
        telegram_status = send_telegram(text)
        print(f"ALERT: {len(new_keys)} new sighting(s); telegram: {telegram_status}")
    else:
        print("no change: no new sightings")

    for key in current:
        seen.setdefault(key, now)
    # Drop keys that vanished from sources so a reappearance re-alerts - but
    # ONLY for sources that succeeded this run. If a source errored (e.g. a
    # transient 429), we must not forget its sightings, or we'd re-alert on
    # everything when it recovers.
    fetched_ok = not errors
    for key in list(seen):
        if key not in current and fetched_ok:
            del seen[key]
    state["seen"] = seen
    state["last_run"] = now
    state["last_errors"] = errors
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)

    summary = {
        "run_at": now,
        "sightings_found": len(current),
        "new_sightings": len(new_keys) if not first_run else 0,
        "telegram": telegram_status,
        "errors": errors,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
