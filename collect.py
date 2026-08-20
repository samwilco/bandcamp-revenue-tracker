#!/usr/bin/env python3
"""
Bandcamp platform-wide sales collector.

What this does, in plain terms:
  1. Asks Bandcamp's public sales feed for the most recent sales.
  2. Writes every individual sale into a compressed monthly file (data/raw/).
  3. Writes a small summary row per run into ONE permanent log (data/bandcamp_log.csv).
  4. Once an hour, reads the homepage banner ("Fans have paid artists $X billion...")
     and adds it to that same permanent log as its own row type.

Important limitation, discovered by testing the API directly:
  The feed only ever returns the LAST 10 MINUTES of sales. There is no archive and
  no way to ask for older data. So a missed run cannot be backfilled -- the data is
  gone from Bandcamp's side. Instead of pretending otherwise, this script DETECTS
  gaps and records them as visible 'gap' rows so you always know what was missed.

Uses only the Python standard library -- nothing to install.
"""

import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW_DIR = os.path.join(DATA, "raw")
LOG_PATH = os.path.join(DATA, "bandcamp_log.csv")
STATE_PATH = os.path.join(HERE, "state.json")

FEED_URL = "https://bandcamp.com/api/salesfeed/1/get"
HOME_URL = "https://bandcamp.com/"
UA = "bandcamp-revenue-tracker/1.0 (personal research project)"

# The feed's hard limit, measured empirically: it never returns more than 600s.
FEED_WINDOW_SEC = 600
# How long to remember event fingerprints so overlapping runs don't double-count.
DEDUPE_RETENTION_SEC = 1800
# Check the homepage banner at most this often (it changes very slowly).
BANNER_INTERVAL_SEC = 3600
# Start a new raw part-file before we get anywhere near GitHub's 100MB hard limit.
MAX_RAW_BYTES = 90 * 1024 * 1024

RAW_COLUMNS = [
    "utc_date", "iso_utc", "event_type", "artist_name", "item_description",
    "album_title", "item_type", "currency", "amount_paid", "item_price",
    "amount_paid_usd", "country", "country_code", "url", "art_id",
]

LOG_COLUMNS = [
    "row_type", "iso_utc", "utc_date", "window_start", "window_end",
    "sales_count", "gross_usd", "top_country", "top_currency",
    "alltime_usd", "records_yesterday", "banner_text", "note",
]


def iso(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url, tries=4):
    """GET with retries and exponential backoff. Returns bytes, or raises."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
    raise last


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass  # corrupt state should never stop collection
    return {"cursor": None, "seen": {}, "last_banner_check": 0, "last_banner_value": None}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)  # atomic: never leave a half-written state file


def fingerprint(item):
    """Stable ID for a sale. The feed gives no unique key, so we hash the fields."""
    parts = [
        repr(item.get("utc_date")), item.get("artist_name") or "",
        item.get("item_description") or "", repr(item.get("amount_paid")),
        item.get("currency") or "", item.get("country_code") or "",
        item.get("url") or "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def raw_path_for(ts):
    """Monthly file, auto-splitting into -part2, -part3... if one grows too large."""
    month = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
    part = 1
    while True:
        suffix = "" if part == 1 else "-part%d" % part
        path = os.path.join(RAW_DIR, "sales_%s%s.csv.gz" % (month, suffix))
        if not os.path.exists(path) or os.path.getsize(path) < MAX_RAW_BYTES:
            return path
        part += 1


def append_raw(rows, ts):
    if not rows:
        return None
    os.makedirs(RAW_DIR, exist_ok=True)
    path = raw_path_for(ts)
    new_file = not os.path.exists(path)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if new_file:
        writer.writerow(RAW_COLUMNS)
    for row in rows:
        writer.writerow(row)
    # Appending in gzip mode writes a new member; concatenated gzip is valid and
    # is read transparently by gzip, pandas, and Excel-after-unzip.
    with gzip.open(path, "at", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    return path


def append_log(record):
    os.makedirs(DATA, exist_ok=True)
    new_file = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(record)


def collect_sales(state, now):
    """Pull the feed, write new sales, return a summary dict."""
    cursor = state.get("cursor")
    # Always ask from the cursor; the server clamps to its 600s maximum anyway.
    # Asking from slightly before the cursor gives overlap, and dedupe removes repeats.
    ask_from = int(cursor) if cursor else int(now - FEED_WINDOW_SEC)
    payload = json.loads(fetch("%s?start_date=%d" % (FEED_URL, ask_from)))

    win_start = payload.get("start_date")
    win_end = payload.get("end_date")
    events = payload.get("events") or []

    # Did we lose time? If the server's window begins after our cursor, whatever
    # happened in between is permanently unavailable. Record it loudly.
    # Capture these now: `cursor` is overwritten further down, and the gap row
    # needs the ORIGINAL boundaries to describe the missing stretch correctly.
    gap_sec, gap_from, gap_to = 0, cursor, win_start
    if cursor and win_start and win_start > cursor + 1:
        gap_sec = win_start - cursor

    seen = state.get("seen") or {}
    rows, new_keys = [], []
    gross = 0.0
    countries, currencies = {}, {}
    max_ts = cursor or 0

    for event in events:
        etype = event.get("event_type")
        for item in event.get("items") or []:
            key = fingerprint(item)
            if key in seen:
                continue  # already logged by an earlier, overlapping run
            ts = item.get("utc_date") or event.get("utc_date") or now
            new_keys.append((key, ts))
            url = item.get("url") or ""
            if url.startswith("//"):
                url = "https:" + url
            rows.append([
                ts, iso(ts), etype, item.get("artist_name"),
                item.get("item_description"), item.get("album_title"),
                item.get("item_type"), item.get("currency"), item.get("amount_paid"),
                item.get("item_price"), item.get("amount_paid_usd"),
                item.get("country"), item.get("country_code"), url, item.get("art_id"),
            ])
            usd = item.get("amount_paid_usd")
            if isinstance(usd, (int, float)):
                gross += usd
            if item.get("country"):
                countries[item["country"]] = countries.get(item["country"], 0) + 1
            if item.get("currency"):
                currencies[item["currency"]] = currencies.get(item["currency"], 0) + 1
            if ts and ts > max_ts:
                max_ts = ts

    rows.sort(key=lambda r: r[0])
    append_raw(rows, now)

    for key, ts in new_keys:
        seen[key] = ts
    horizon = now - DEDUPE_RETENTION_SEC
    state["seen"] = {k: v for k, v in seen.items() if v and v >= horizon}
    state["cursor"] = max(max_ts, win_end or 0) if (max_ts or win_end) else cursor

    return {
        "count": len(rows), "gross": gross, "gap_sec": gap_sec,
        "gap_from": gap_from, "gap_to": gap_to,
        "win_start": win_start, "win_end": win_end,
        # When nothing new has happened the feed replies with only {"server_time": ...}
        # -- no window at all. That is normal, not an error.
        "no_data": win_start is None,
        "top_country": max(countries, key=countries.get) if countries else "",
        "top_currency": max(currencies, key=currencies.get) if currencies else "",
    }


BANNER_RE = re.compile(
    r"Fans have paid artists\s*<mark[^>]*>\s*\$([0-9.,]+)\s*(billion|million)?\s*</mark>"
    r"\s*using Bandcamp,\s*and yesterday alone bought\s*<mark[^>]*>\s*([0-9,]+)\s*</mark>",
    re.IGNORECASE,
)


def collect_banner(state, now):
    """Light hourly check of the homepage cumulative figure. Never fatal."""
    if now - (state.get("last_banner_check") or 0) < BANNER_INTERVAL_SEC:
        return None
    state["last_banner_check"] = now
    try:
        html = fetch(HOME_URL, tries=2).decode("utf-8", "replace")
    except Exception as exc:
        return {"row_type": "banner_error", "note": "%s: %s" % (type(exc).__name__, exc)}

    match = BANNER_RE.search(html)
    if not match:
        # Bandcamp changed their wording or markup -- worth knowing about.
        return {"row_type": "banner_error", "note": "banner text not found on homepage"}

    amount, scale, records = match.group(1), (match.group(2) or "").lower(), match.group(3)
    dollars = float(amount.replace(",", ""))
    if scale == "billion":
        dollars *= 1000000000
    elif scale == "million":
        dollars *= 1000000
    text = "Fans have paid artists $%s %s using Bandcamp, and yesterday alone bought %s records." % (
        amount, scale, records)

    # Only log when something actually changed -- the rounded $ figure moves rarely.
    if text == state.get("last_banner_value"):
        return None
    state["last_banner_value"] = text
    return {
        "row_type": "banner", "alltime_usd": "%.0f" % dollars,
        "records_yesterday": records.replace(",", ""), "banner_text": text,
    }


def main():
    now = time.time()
    state = load_state()
    exit_code = 0

    try:
        summary = collect_sales(state, now)
        if summary["gap_sec"] > 0:
            append_log({
                "row_type": "gap", "iso_utc": iso(now), "utc_date": "%.3f" % now,
                "window_start": "%.3f" % (summary["gap_from"] or 0),
                "window_end": "%.3f" % (summary["gap_to"] or 0),
                "note": "MISSED %.0f seconds - not recoverable (feed only keeps 10 min)"
                        % summary["gap_sec"],
            })
        append_log({
            "row_type": "sales", "iso_utc": iso(now), "utc_date": "%.3f" % now,
            "window_start": "%.3f" % summary["win_start"] if summary["win_start"] else "",
            "window_end": "%.3f" % summary["win_end"] if summary["win_end"] else "",
            "sales_count": summary["count"], "gross_usd": "%.2f" % summary["gross"],
            "top_country": summary["top_country"], "top_currency": summary["top_currency"],
            "note": "no new data in feed yet" if summary["no_data"] else "",
        })
        print("sales: %d new rows, $%.2f USD%s" % (
            summary["count"], summary["gross"],
            ", GAP %.0fs" % summary["gap_sec"] if summary["gap_sec"] else ""))
    except Exception as exc:
        append_log({
            "row_type": "error", "iso_utc": iso(now), "utc_date": "%.3f" % now,
            "note": "sales fetch failed: %s: %s" % (type(exc).__name__, exc),
        })
        print("ERROR fetching sales: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        exit_code = 1

    try:
        banner = collect_banner(state, now)
        if banner:
            banner.setdefault("iso_utc", iso(now))
            banner.setdefault("utc_date", "%.3f" % now)
            append_log(banner)
            print("banner: %s" % (banner.get("banner_text") or banner.get("note")))
    except Exception as exc:
        print("banner check failed (non-fatal): %s" % exc, file=sys.stderr)

    try:
        save_state(state)
    except OSError as exc:
        # Data is already safely on disk at this point; losing the cursor only
        # costs us one window of overlap on the next run.
        print("WARNING: could not save state: %s" % exc, file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
