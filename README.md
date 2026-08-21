# Bandcamp Revenue Tracker

Automatically records Bandcamp's platform-wide sales around the clock, using a free
GitHub robot so it keeps working when your computer is off.

---

## Read this first: the one real limitation

Bandcamp's public sales feed **only remembers the last 10 minutes.**

This was measured directly, not assumed. Asking the feed for 1 hour, 2 hours, or even
2 days of history returns the exact same 10-minute window every time. There is no
archive to request.

Two consequences worth understanding:

1. **It has to poll at least every 10 minutes**, not hourly. An hourly schedule would
   only ever see the last 10 minutes of each hour and would permanently miss about
   **83% of all sales**. In practice the collector polls every 4 minutes, continuously
   — see [The automation](#the-automation) for why that took two attempts to get right.

2. **A missed run cannot be caught up.** If the collector is offline for 20 minutes,
   those sales are gone from Bandcamp's side forever. No amount of retrying recovers
   them.

Because catching up is impossible, this project does the next best thing: it makes any
loss **visible instead of silent**. Every gap is written into the log as its own `gap`
row saying exactly how many seconds were missed. You will never have a hole in your
data that you don't know about.

---

## What gets collected

| File | What's in it | Grows by |
|---|---|---|
| `data/bandcamp_log.csv` | **The permanent log.** One summary row per run, plus banner readings, plus any gaps or errors. Never rotates. | ~13 MB/year |
| `data/raw/sales_YYYY-MM.csv.gz` | Every individual sale, one file per month, compressed. | ~60 MB/month |
| `state.json` | Remembers where collection left off, so nothing is double-counted. | tiny |
| `dashboard.html` | The dashboard, rebuilt after every run. Double-click to open. | ~15 KB |

### Why the raw file is compressed

Bandcamp does roughly **70,000 sales a day**, which is about 12.6 MB of raw CSV daily.
GitHub outright refuses any file larger than 100 MB, so a single uncompressed file would
have broken the automation after about **8 days**. Compressed monthly files stay
comfortably under that limit.

As an extra safety net, if any monthly file ever approaches 90 MB, the script
automatically starts `sales_YYYY-MM-part2.csv.gz` on its own. The automation cannot
break from file size, even if traffic doubles.

### The row types in the permanent log

- `sales` — a normal collection run (how many sales, total USD, top country)
- `banner` — the homepage headline figure, e.g. *"Fans have paid artists $1.78 billion
  using Bandcamp, and yesterday alone bought 77,397 records."* Checked once an hour and
  recorded **only when it changes**, so it doesn't spam the log
- `gap` — time that was missed and is not recoverable
- `error` / `banner_error` — something went wrong, with the reason

---

## The dashboard

Open `dashboard.html` (double-click it, or `git pull` first for the newest data).
It shows fan spend per day, trailing 7- and 30-day windows against the periods before
them, and an estimated Bandcamp take rate, each split by digital and physical.

Take rate is shown as a **range**, not a single number, because Bandcamp charges 15% on
digital but 10% once an artist passes $5,000 in sales — and the feed never says which
artists have. A single figure would be false precision. Physical is a flat 10%, so that
side of the range is exact, though slightly overstated because the feed bundles shipping
and tax into the amount paid.

Days that were not observed for a full 24 hours are drawn with hatching and left out of
every average, so a partial day is never mistaken for a drop in sales.

## Seeing your numbers

```
python daily_totals.py
```

Prints sales and dollar totals per day. It only reads your data; it never changes it.

A useful accuracy check: measured at full polling rate, the feed implies roughly
**70,000 sales/day**, against the **~77,000/day** Bandcamp publishes on its own
homepage. Being within ~10% suggests the feed is broadly complete rather than a partial
sample. Compare that against your own collected totals to see how much the collector is
actually keeping up with — the dashboard shows this as "% of the period observed".

---

## Running it by hand

```
python collect.py
```

Needs Python 3 and nothing else — no packages to install, so there are no dependencies
that can break later.

---

## The automation

`.github/workflows/collect.yml` keeps the collector running continuously on GitHub's
servers and commits the results back to this repository.

**Why it works the way it does.** We first asked GitHub to start a run every 5
minutes. Measured over the first day, GitHub actually started one roughly every **34
minutes** — it quietly coalesces frequent schedules. Because the feed only holds 10
minutes, we were seeing 10 minutes in every 34 and capturing just **25%** of sales.

So the collector no longer depends on GitHub being punctual. Each run now stays alive
and polls every 4 minutes for up to 50 minutes, saving as it goes. The schedule keeps
firing in the background, so a replacement run waits in the queue and starts the moment
the current one ends. Coverage is continuous rather than a sample.

Things it handles by itself:

- **Network hiccups** — retries 4 times with increasing delays before giving up
- **Overlapping runs** — only one run may write at a time, so commits can't collide
- **Push conflicts** — retries up to 5 times, rebasing onto the newest data
- **A failed collection** — still commits whatever it got, and records an `error` row
- **A corrupted state file** — rebuilds it and carries on rather than stopping

### Two honest caveats

- **GitHub's scheduler is not punctual**, which is exactly why each run polls for 50
  minutes instead of checking once. Gaps can still happen if GitHub fails to start any
  run for a long stretch, and those are logged as `gap` rows. Check the `gap` rows and
  the dashboard's "% of the period observed" figure to see how well it is keeping up.
- **Cost:** free and unlimited on a **public** repository. If you make this repo
  **private**, GitHub only gives 2,000 free minutes/month and this uses far more, which
  would cost money. Either keep it public, or tell me and I'll slow the schedule to fit
  the free allowance.

---

## If it stops working

1. Go to the **Actions** tab in the GitHub repo and look at the latest run.
2. Check `data/bandcamp_log.csv` for `error` or `banner_error` rows — the `note` column
   says what happened in plain words.
3. A `banner_error` saying *"banner text not found"* means Bandcamp reworded their
   homepage. Sales collection is unaffected; only that one figure needs a fix.

GitHub disables scheduled workflows in repositories with no activity for 60 days.
This one commits constantly, so that won't be triggered.
