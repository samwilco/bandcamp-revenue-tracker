#!/usr/bin/env python3
"""
Show daily totals from what has been collected so far.

Just run:  python daily_totals.py

This only READS your data -- it never changes or deletes anything.
"""

import csv
import glob
import gzip
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_GLOB = os.path.join(HERE, "data", "raw", "sales_*.csv.gz")
LOG_PATH = os.path.join(HERE, "data", "bandcamp_log.csv")


def main():
    days = defaultdict(lambda: {"sales": 0, "usd": 0.0})
    files = sorted(glob.glob(RAW_GLOB))

    for path in files:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                # Each appended chunk repeats the header; skip those lines.
                if not row.get("iso_utc") or row["iso_utc"] == "iso_utc":
                    continue
                day = days[row["iso_utc"][:10]]
                day["sales"] += 1
                try:
                    day["usd"] += float(row.get("amount_paid_usd") or 0)
                except ValueError:
                    pass

    if not days:
        print("No sales collected yet. Run: python collect.py")
        return

    # Bandcamp's own published figure, for comparison against our own count.
    banner = {}
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("row_type") == "banner" and row.get("records_yesterday"):
                    banner[row["iso_utc"][:10]] = row["records_yesterday"]

    print("%-12s %10s %14s" % ("DATE", "SALES", "GROSS USD"))
    print("-" * 40)
    for day in sorted(days):
        d = days[day]
        print("%-12s %10s %14s" % (day, "{:,}".format(d["sales"]), "${:,.2f}".format(d["usd"])))

    total_sales = sum(d["sales"] for d in days.values())
    total_usd = sum(d["usd"] for d in days.values())
    print("-" * 40)
    print("%-12s %10s %14s" % ("TOTAL", "{:,}".format(total_sales), "${:,.2f}".format(total_usd)))

    coverage_note(days)

    if banner:
        print("\nBandcamp's own published 'yesterday' figures, for comparison:")
        for day in sorted(banner):
            print("  seen on %s: %s records" % (day, "{:,}".format(int(banner[day]))))


def coverage_note(days):
    """Warn plainly if a day looks incomplete, so partial days are never mistaken
    for real declines in sales."""
    counts = [d["sales"] for d in days.values()]
    if len(counts) > 1 and min(counts) < max(counts) * 0.5:
        print("\nNote: the first and last days are usually partial "
              "(collection did not run for the whole day).")


if __name__ == "__main__":
    main()
