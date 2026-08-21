#!/usr/bin/env python3
"""
Builds dashboard.html from the collected sales data.

Run it yourself with:  python dashboard.py
The automation also runs it after every collection, so the file stays current.

It only READS your data and writes dashboard.html. It never changes the log.

--------------------------------------------------------------------------
How the money figures are worked out
--------------------------------------------------------------------------
"Fan spend" is the amount fans actually paid, straight from the feed.

"Bandcamp's cut" is an ESTIMATE, shown as a range, using Bandcamp's own
published rates:
  - Digital (tracks, albums, discographies): 15%, dropping to 10% once an
    artist passes $5,000 in cumulative sales.
  - Physical (vinyl, CDs, merch): 10%, excluding shipping and tax.
  - The revenue share applies only to the first $100 of any one item.

The range exists because the feed does not say which artists have passed the
$5,000 threshold, so the digital rate is genuinely unknown between 10% and
15%. The low end assumes every artist is above the threshold; the high end
assumes every artist is below it. The truth sits somewhere between.

Two known biases, stated plainly rather than hidden:
  - Physical is slightly OVERSTATED, because amount_paid includes shipping
    and tax, which Bandcamp excludes from its 10%.
  - Payment processing fees (4-6%) are NOT included -- they go to the payment
    processor, not to Bandcamp.
"""

import csv
import glob
import gzip
import html
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_GLOB = os.path.join(HERE, "data", "raw", "sales_*.csv.gz")
LOG_PATH = os.path.join(HERE, "data", "bandcamp_log.csv")
OUT_PATH = os.path.join(HERE, "dashboard.html")

# item_type codes seen in the feed: t=track, a=album, b=discography, p=package.
# Packages are the physical goods (vinyl, CDs, shirts); everything else is digital.
PHYSICAL_TYPES = {"p"}

FEE_CAP = 100.0          # revenue share applies to the first $100 of an item only
DIGITAL_RATE_LOW = 0.10  # artist has passed $5,000 cumulative sales
DIGITAL_RATE_HIGH = 0.15 # artist has not
PHYSICAL_RATE = 0.10     # flat, excludes shipping and tax

# A day counts as "complete" only if we observed at least this much of it.
# Anything less is shown but excluded from trailing averages and comparisons,
# so a partial day is never mistaken for a real drop in sales.
COMPLETE_THRESHOLD = 0.98
DAY_SECONDS = 86400


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_sales():
    """Every sale row across all monthly files."""
    out = []
    for path in sorted(glob.glob(RAW_GLOB)):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                # Each appended chunk repeats the header line; skip those.
                iso = row.get("iso_utc")
                if not iso or iso == "iso_utc":
                    continue
                try:
                    usd = float(row.get("amount_paid_usd") or 0)
                    ts = float(row.get("utc_date") or 0)
                except ValueError:
                    continue
                out.append({
                    "date": iso[:10],
                    "ts": ts,
                    "usd": usd,
                    "physical": (row.get("item_type") or "") in PHYSICAL_TYPES,
                })
    return out


def load_gaps():
    """Known un-collected stretches, as (start_epoch, end_epoch) pairs."""
    gaps = []
    if not os.path.exists(LOG_PATH):
        return gaps
    with open(LOG_PATH, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("row_type") != "gap":
                continue
            try:
                start, end = float(row["window_start"]), float(row["window_end"])
            except (ValueError, KeyError, TypeError):
                continue
            if end > start:
                gaps.append((start, end))
    return gaps


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def fees(usd, physical):
    """(low, high) estimate of Bandcamp's cut on a single sale."""
    base = min(usd, FEE_CAP)
    if physical:
        return base * PHYSICAL_RATE, base * PHYSICAL_RATE
    return base * DIGITAL_RATE_LOW, base * DIGITAL_RATE_HIGH


def aggregate(sales):
    days = defaultdict(lambda: {
        "spend": 0.0, "n": 0, "fee_low": 0.0, "fee_high": 0.0,
        "dig_spend": 0.0, "dig_n": 0, "dig_low": 0.0, "dig_high": 0.0,
        "phy_spend": 0.0, "phy_n": 0, "phy_low": 0.0, "phy_high": 0.0,
    })
    for s in sales:
        d = days[s["date"]]
        low, high = fees(s["usd"], s["physical"])
        d["spend"] += s["usd"]
        d["n"] += 1
        d["fee_low"] += low
        d["fee_high"] += high
        prefix = "phy" if s["physical"] else "dig"
        d[prefix + "_spend"] += s["usd"]
        d[prefix + "_n"] += 1
        d[prefix + "_low"] += low
        d[prefix + "_high"] += high
    return days


def day_bounds(date_str):
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return start.timestamp(), start.timestamp() + DAY_SECONDS


def coverage(days, sales, gaps):
    """How much of each calendar day we actually observed, 0.0 to 1.0."""
    if not sales:
        return {}
    first_ts = min(s["ts"] for s in sales)
    last_ts = max(s["ts"] for s in sales)
    out = {}
    for date_str in days:
        start, end = day_bounds(date_str)
        observed_start = max(start, first_ts)
        observed_end = min(end, last_ts)
        span = max(0.0, observed_end - observed_start)
        # Subtract any recorded gap that overlaps this day's observed span.
        for gap_start, gap_end in gaps:
            overlap = min(observed_end, gap_end) - max(observed_start, gap_start)
            if overlap > 0:
                span -= overlap
        out[date_str] = max(0.0, min(1.0, span / DAY_SECONDS))
    return out


def date_range(first, last):
    """Every calendar date from first to last, including days with no data."""
    d0 = datetime.strptime(first, "%Y-%m-%d")
    d1 = datetime.strptime(last, "%Y-%m-%d")
    out = []
    while d0 <= d1:
        out.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# Period maths
# --------------------------------------------------------------------------

EMPTY = {"spend": 0.0, "n": 0, "fee_low": 0.0, "fee_high": 0.0,
         "dig_spend": 0.0, "dig_n": 0, "dig_low": 0.0, "dig_high": 0.0,
         "phy_spend": 0.0, "phy_n": 0, "phy_low": 0.0, "phy_high": 0.0}


def window(days, dates):
    total = dict(EMPTY)
    for d in dates:
        day = days.get(d)
        if not day:
            continue
        for k in total:
            total[k] += day[k]
    return total


def periods(days, cov, n):
    """
    Returns (current, prior, status).

    status is None when both windows are usable, otherwise a plain-language
    explanation of what is still missing. We require every calendar day in a
    window to be complete -- a window containing a partial day would understate
    that period and produce a fake decline.
    """
    if not days:
        return None, None, "No data collected yet."

    all_dates = date_range(min(days), max(days))
    complete = [d for d in all_dates if cov.get(d, 0) >= COMPLETE_THRESHOLD]
    if not complete:
        have = len(complete)
        return None, None, ("No complete days yet (%d of %d needed). "
                            "Collection is still part-way through its first day."
                            % (have, n))

    anchor = complete[-1]
    idx = all_dates.index(anchor)

    cur_dates = all_dates[max(0, idx - n + 1): idx + 1]
    prior_dates = all_dates[max(0, idx - 2 * n + 1): max(0, idx - n + 1)]

    total_complete = len(complete)
    if len(cur_dates) < n or any(cov.get(d, 0) < COMPLETE_THRESHOLD for d in cur_dates):
        return None, None, ("%d of %d complete days collected — ready in about %d more."
                            % (total_complete, n, max(1, n - total_complete)))

    current = window(days, cur_dates)
    if len(prior_dates) < n or any(cov.get(d, 0) < COMPLETE_THRESHOLD for d in prior_dates):
        return current, None, ("Comparison needs %d complete days; %d collected so far."
                               % (2 * n, total_complete))

    return current, window(days, prior_dates), None


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def money(v):
    if v >= 1_000_000:
        return "$%.2fM" % (v / 1_000_000)
    if v >= 10_000:
        return "$%.0fk" % (v / 1000)
    if v >= 1000:
        return "$%.1fk" % (v / 1000)
    return "$%.0f" % v


def money_exact(v):
    return "${:,.2f}".format(v)


def rate_range(low, high, spend):
    if spend <= 0:
        return "--"
    lo, hi = 100 * low / spend, 100 * high / spend
    if abs(hi - lo) < 0.05:
        return "%.1f%%" % lo
    return "%.1f%%&#8202;–&#8202;%.1f%%" % (lo, hi)


def money_range(low, high):
    if abs(high - low) < 0.005:
        return money(low)
    return "%s&#8202;–&#8202;%s" % (money(low), money(high))


def delta(cur, prior):
    """(text, direction) for a percentage change, or None if not computable."""
    if prior is None or prior == 0:
        return None
    pct = 100.0 * (cur - prior) / prior
    if pct > 0.05:
        return ("&#9650; +%.1f%%" % pct, "up")
    if pct < -0.05:
        return ("&#9660; %.1f%%" % pct, "down")
    return ("&#9644; %.1f%%" % pct, "flat")


def esc(s):
    return html.escape(str(s), quote=True)


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bandcamp platform revenue</title>
<style>
:root{
  color-scheme: light;
  --page:#f9f9f7; --surface-1:#fcfcfb;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7;
  --series-1:#2a78d6; --series-2:#eb6834;
  --good:#006300; --bad:#d03b3b;
  --border:rgba(11,11,11,0.10);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --page:#0d0d0d; --surface-1:#1a1a19;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835;
    --series-1:#3987e5; --series-2:#d95926;
    --good:#0ca30c; --bad:#d03b3b;
    --border:rgba(255,255,255,0.10);
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --page:#0d0d0d; --surface-1:#1a1a19;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835;
  --series-1:#3987e5; --series-2:#d95926;
  --good:#0ca30c; --bad:#d03b3b;
  --border:rgba(255,255,255,0.10);
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text-primary);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
header h1{margin:0 0 6px;font-size:26px;letter-spacing:-0.02em}
.meta{margin:0;color:var(--text-secondary);font-size:13.5px}
h2{margin:0 0 14px;font-size:15px;font-weight:600;letter-spacing:-0.01em}
.notice{margin:20px 0 0;padding:12px 14px;border:1px solid var(--border);
  border-left:3px solid var(--series-2);border-radius:8px;
  background:var(--surface-1);color:var(--text-secondary);font-size:13.5px}
.notice.alarm{border-left-color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:20px 0 0}
.tile,.panel,.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px}
.tile{padding:14px 16px}
.label{color:var(--text-secondary);font-size:12px;font-weight:500;
  text-transform:uppercase;letter-spacing:0.05em}
.big{font-size:27px;font-weight:650;letter-spacing:-0.025em;margin:6px 0 2px;
  font-variant-numeric:tabular-nums}
.sub{color:var(--text-secondary);font-size:12.5px;font-variant-numeric:tabular-nums}
.panel{padding:18px 18px 20px;margin:20px 0 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{padding:14px 16px;background:transparent}
.card .value{font-size:23px;font-weight:650;letter-spacing:-0.02em;margin:6px 0 4px;
  font-variant-numeric:tabular-nums}
.card .value.muted{font-size:15px;font-weight:500;color:var(--muted);letter-spacing:0}
.card.pending{border-style:dashed}
.delta{font-size:13px;margin-bottom:6px;font-variant-numeric:tabular-nums}
.delta .up{color:var(--good);font-weight:600}
.delta .down{color:var(--bad);font-weight:600}
.delta .flat{color:var(--text-secondary);font-weight:600}
.delta .vs{color:var(--text-secondary)}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:0 0 12px;
  font-size:12.5px;color:var(--text-secondary)}
.key{display:inline-flex;align-items:center;gap:6px}
.key i{width:11px;height:11px;border-radius:3px;display:inline-block}
.key .hatchkey{background:repeating-linear-gradient(45deg,var(--muted) 0 2px,transparent 2px 5px);
  border:1px solid var(--border)}
.scroll{overflow-x:auto}
.chart{display:block;max-width:100%;height:auto}
.chart .ax{fill:var(--muted);font-size:11px}
.chart .ax.partial{font-size:9.5px;font-style:italic}
.chart .val{fill:var(--text-secondary);font-size:11px;font-weight:600;
  font-variant-numeric:tabular-nums}
.bar{cursor:default}
.bar:hover,.bar:focus{opacity:0.82;outline:none}
table{border-collapse:collapse;width:100%;font-size:13px;
  font-variant-numeric:tabular-nums;margin-top:12px}
th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--border);
  white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--text-secondary);font-weight:600;font-size:11.5px;
  text-transform:uppercase;letter-spacing:0.04em}
.partial-row td{color:var(--muted);font-style:italic}
summary{cursor:pointer;font-size:14px;font-weight:600}
.foot{margin:24px 0 0;color:var(--text-secondary);font-size:12.5px;line-height:1.65}
.foot code{background:var(--surface-1);padding:1px 5px;border-radius:4px;
  border:1px solid var(--border);font-size:12px}
.empty{color:var(--muted)}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;
  background:var(--text-primary);color:var(--page);padding:7px 10px;border-radius:6px;
  font-size:12px;max-width:300px;z-index:9;font-variant-numeric:tabular-nums}
#tip.on{opacity:1}
#themer{position:absolute;top:32px;right:20px;background:var(--surface-1);
  border:1px solid var(--border);color:var(--text-secondary);border-radius:7px;
  padding:6px 11px;font-size:12px;cursor:pointer}
</style>
</head>
<body>
<button id="themer" type="button">Light / dark</button>
"""

FOOTNOTE = """<div class="foot">
<p><strong>How these figures are worked out.</strong>
&ldquo;Fan spend&rdquo; is what fans actually paid, taken straight from Bandcamp's
public sales feed. &ldquo;Bandcamp's cut&rdquo; is an <em>estimate</em> shown as a
range, using Bandcamp's published rates: <strong>15% on digital</strong>, falling to
<strong>10%</strong> once an artist passes $5,000 in cumulative sales, and
<strong>10% on physical goods</strong>. The revenue share applies only to the first
$100 of any single item.</p>
<p>The range exists for an honest reason: the feed does not reveal which artists have
passed the $5,000 threshold, so the true digital rate is genuinely unknown between 10%
and 15%. The low end assumes every artist is above it; the high end assumes none are.</p>
<p><strong>Two known biases, stated rather than hidden.</strong> Physical take rate is
slightly <em>overstated</em>, because the feed bundles shipping and tax into the amount
paid while Bandcamp excludes them from its 10%. And payment processing fees (4&ndash;6%)
are <em>not</em> counted here, because they go to the payment processor rather than to
Bandcamp.</p>
<p>Media type comes from the feed's own item code: packages
(<code>p</code>&nbsp;&mdash; vinyl, CDs, merch) count as physical; tracks, albums and
discographies count as digital.</p>
<p>Days marked <em>partial</em> were not observed for a full 24 hours, so they are drawn
with hatching and left out of every average and comparison &mdash; a partial day must
never be mistaken for a fall in sales.</p>
</div>"""

SCRIPT = """<script>
(function(){
  var tip=document.getElementById('tip');
  function show(e,t){tip.textContent=t;tip.classList.add('on');
    var x=e.clientX+14,y=e.clientY+14;
    if(x+tip.offsetWidth>innerWidth-8)x=e.clientX-tip.offsetWidth-14;
    if(y+tip.offsetHeight>innerHeight-8)y=e.clientY-tip.offsetHeight-14;
    tip.style.left=x+'px';tip.style.top=y+'px';}
  function hide(){tip.classList.remove('on');}
  document.querySelectorAll('.bar').forEach(function(el){
    var t=(el.getAttribute('data-tip')||'').split('|').join(' \\u00b7 ');
    el.addEventListener('mousemove',function(e){show(e,t);});
    el.addEventListener('mouseleave',hide);
    el.addEventListener('focus',function(){
      var r=el.getBoundingClientRect();
      show({clientX:r.left+r.width/2,clientY:r.top},t);});
    el.addEventListener('blur',hide);
  });
  var b=document.getElementById('themer');
  b.addEventListener('click',function(){
    var cur=document.documentElement.getAttribute('data-theme');
    var dark=cur?cur==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.setAttribute('data-theme',dark?'light':'dark');
  });
})();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------

def nice_ceiling(v):
    if v <= 0:
        return 1.0
    import math
    mag = 10 ** math.floor(math.log10(v))
    for step in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if v <= step * mag:
            return step * mag
    return 10 * mag


def top_rounded(x, y, w, h, r=4.0):
    """Path for a bar segment with only its top corners rounded."""
    r = min(r, w / 2.0, max(h, 0.01))
    if h <= 0.5:
        return ""
    # bottom-left -> up -> round the top-left -> across -> round the top-right -> down
    return ("M%.2f %.2f L%.2f %.2f Q%.2f %.2f %.2f %.2f "
            "L%.2f %.2f Q%.2f %.2f %.2f %.2f L%.2f %.2f Z" % (
                x, y + h,
                x, y + r,
                x, y, x + r, y,
                x + w - r, y,
                x + w, y, x + w, y + r,
                x + w, y + h))


def build_chart(dates, days, cov):
    """Stacked daily revenue bars. Partial days are hatched and labelled."""
    if not dates:
        return "<p class='empty'>No days collected yet.</p>", ""

    pad_l, pad_r, pad_t, pad_b = 62, 16, 16, 46
    plot_h = 240
    slot = max(38.0, min(96.0, 620.0 / max(len(dates), 1)))
    plot_w = slot * len(dates)
    width = pad_l + plot_w + pad_r
    height = pad_t + plot_h + pad_b

    top = nice_ceiling(max([days.get(d, EMPTY)["spend"] for d in dates] + [1]))
    bar_w = min(56.0, slot * 0.62)

    parts = []
    parts.append(
        "<svg viewBox='0 0 %.0f %.0f' width='%.0f' height='%.0f' role='img' "
        "aria-label='Daily fan spend, split by digital and physical' "
        "class='chart'>" % (width, height, width, height))
    parts.append(
        "<defs><pattern id='hatch' width='6' height='6' patternTransform='rotate(45)' "
        "patternUnits='userSpaceOnUse'>"
        "<rect width='6' height='6' fill='var(--surface-1)' opacity='0.55'/>"
        "<line x1='0' y1='0' x2='0' y2='6' stroke='var(--muted)' stroke-width='2.5'/>"
        "</pattern></defs>")

    # Gridlines and y-axis labels
    for i in range(5):
        val = top * i / 4.0
        y = pad_t + plot_h - (plot_h * i / 4.0)
        parts.append("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' "
                     "stroke='var(--grid)' stroke-width='1'/>"
                     % (pad_l, y, pad_l + plot_w, y))
        parts.append("<text x='%.1f' y='%.1f' class='ax' text-anchor='end'>%s</text>"
                     % (pad_l - 10, y + 4, money(val) if i else "$0"))

    labelled = len(dates) <= 14
    for i, date_str in enumerate(dates):
        day = days.get(date_str, EMPTY)
        c = cov.get(date_str, 0.0)
        partial = c < COMPLETE_THRESHOLD
        x = pad_l + slot * i + (slot - bar_w) / 2.0
        total = day["spend"]

        dig_h = (day["dig_spend"] / top) * plot_h if top else 0
        phy_h = (day["phy_spend"] / top) * plot_h if top else 0
        base_y = pad_t + plot_h

        tip = ("%s%s | fan spend %s | digital %s | physical %s | %s sales | %.0f%% of day observed"
               % (date_str, " (PARTIAL)" if partial else "", money_exact(total),
                  money_exact(day["dig_spend"]), money_exact(day["phy_spend"]),
                  "{:,}".format(day["n"]), c * 100))

        parts.append("<g class='bar' tabindex='0' data-tip=\"%s\">" % esc(tip))
        # Digital sits at the bottom; physical stacks on top with a 2px surface gap.
        if phy_h > 0.5:
            y_phy = base_y - dig_h - 2 - phy_h
            parts.append("<path d='%s' fill='var(--series-2)'/>"
                         % top_rounded(x, y_phy, bar_w, phy_h))
            if partial:
                parts.append("<path d='%s' fill='url(#hatch)'/>"
                             % top_rounded(x, y_phy, bar_w, phy_h))
        if dig_h > 0.5:
            y_dig = base_y - dig_h
            # Only the topmost segment gets rounded ends.
            if phy_h > 0.5:
                parts.append("<rect x='%.2f' y='%.2f' width='%.2f' height='%.2f' "
                             "fill='var(--series-1)'/>" % (x, y_dig, bar_w, dig_h))
                if partial:
                    parts.append("<rect x='%.2f' y='%.2f' width='%.2f' height='%.2f' "
                                 "fill='url(#hatch)'/>" % (x, y_dig, bar_w, dig_h))
            else:
                parts.append("<path d='%s' fill='var(--series-1)'/>"
                             % top_rounded(x, y_dig, bar_w, dig_h))
                if partial:
                    parts.append("<path d='%s' fill='url(#hatch)'/>"
                                 % top_rounded(x, y_dig, bar_w, dig_h))
        parts.append("</g>")

        if labelled and total > 0:
            parts.append("<text x='%.1f' y='%.1f' class='val' text-anchor='middle'>%s</text>"
                         % (x + bar_w / 2.0, base_y - dig_h - phy_h - 10, money(total)))

        parts.append("<text x='%.1f' y='%.1f' class='ax' text-anchor='middle'>%s</text>"
                     % (x + bar_w / 2.0, base_y + 18, date_str[5:]))
        if partial:
            parts.append("<text x='%.1f' y='%.1f' class='ax partial' "
                         "text-anchor='middle'>partial</text>"
                         % (x + bar_w / 2.0, base_y + 32))

    parts.append("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' stroke='var(--axis)' "
                 "stroke-width='1'/>" % (pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h))
    parts.append("</svg>")

    legend = (
        "<div class='legend'>"
        "<span class='key'><i style='background:var(--series-1)'></i>Digital</span>"
        "<span class='key'><i style='background:var(--series-2)'></i>Physical</span>"
        "<span class='key'><i class='hatchkey'></i>Partial day — excluded from averages</span>"
        "</div>")
    return "".join(parts), legend


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------

def metric_card(title, cur, prior, status, per_day=False, n=1, scope="all"):
    """One trailing-period card. Falls back to an honest 'not yet' state."""
    if cur is None:
        return ("<div class='card pending'><div class='label'>%s</div>"
                "<div class='value muted'>Not enough data yet</div>"
                "<div class='sub'>%s</div></div>" % (esc(title), esc(status or "")))

    if scope == "digital":
        spend, low, high = cur["dig_spend"], cur["dig_low"], cur["dig_high"]
        p_spend = prior["dig_spend"] if prior else None
    elif scope == "physical":
        spend, low, high = cur["phy_spend"], cur["phy_low"], cur["phy_high"]
        p_spend = prior["phy_spend"] if prior else None
    else:
        spend, low, high = cur["spend"], cur["fee_low"], cur["fee_high"]
        p_spend = prior["spend"] if prior else None

    shown = spend / n if per_day else spend
    p_shown = (p_spend / n) if (per_day and p_spend is not None) else p_spend

    d = delta(shown, p_shown)
    if d:
        delta_html = ("<div class='delta'><span class='%s'>%s</span> "
                      "<span class='vs'>vs previous %d days</span></div>" % (d[1], d[0], n))
    else:
        delta_html = ("<div class='delta'><span class='vs'>%s</span></div>"
                      % esc(status or "No comparison period yet"))

    return ("<div class='card'><div class='label'>%s</div>"
            "<div class='value'>%s</div>%s"
            "<div class='sub'>Bandcamp's cut %s &middot; take rate %s</div></div>"
            % (esc(title), money(shown), delta_html,
               money_range(low / (n if per_day else 1), high / (n if per_day else 1)),
               rate_range(low, high, spend)))


def load_run_times():
    """Timestamp of every collection run, from the permanent log."""
    out = []
    if not os.path.exists(LOG_PATH):
        return out
    with open(LOG_PATH, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("row_type") == "sales":
                try:
                    out.append(float(row["utc_date"]))
                except (ValueError, KeyError, TypeError):
                    pass
    return sorted(out)


def _median_gap_minutes(sales):
    """Median minutes between collection runs -- the real polling frequency."""
    ts = load_run_times()
    if len(ts) < 2:
        return 0.0
    deltas = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    return deltas[len(deltas) // 2] / 60.0


def overall_coverage(sales, gaps):
    """Fraction of the whole collection span we actually observed."""
    if not sales:
        return 0.0
    first_ts, last_ts = min(s["ts"] for s in sales), max(s["ts"] for s in sales)
    span = last_ts - first_ts
    if span <= 0:
        return 0.0
    missed = 0.0
    for gap_start, gap_end in gaps:
        overlap = min(last_ts, gap_end) - max(first_ts, gap_start)
        if overlap > 0:
            missed += overlap
    return max(0.0, min(1.0, (span - missed) / span))


def render(days, cov, sales, gaps):
    dates = date_range(min(days), max(days)) if days else []
    total = window(days, dates)
    observed = overall_coverage(sales, gaps)

    complete = [d for d in dates if cov.get(d, 0) >= COMPLETE_THRESHOLD]
    cur7, prior7, status7 = periods(days, cov, 7)
    cur30, prior30, status30 = periods(days, cov, 30)

    chart_svg, legend = build_chart(dates, days, cov)

    first_iso = min(s["ts"] for s in sales) if sales else 0
    last_iso = max(s["ts"] for s in sales) if sales else 0
    fmt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")

    out = []
    out.append(HEAD)
    out.append("<div class='wrap'>")

    out.append("<header><h1>Bandcamp platform revenue</h1>"
               "<p class='meta'>%s &rarr; %s &middot; %s sales collected &middot; "
               "%.0f%% of the period observed &middot; %d complete day%s</p></header>"
               % (esc(fmt(first_iso)), esc(fmt(last_iso)),
                  "{:,}".format(total["n"]), observed * 100, len(complete),
                  "" if len(complete) == 1 else "s"))

    if observed < 0.95:
        out.append("<div class='notice alarm'><strong>These totals are undercounts.</strong> "
                   "Only <strong>%.0f%%</strong> of the period was actually observed, because "
                   "the collector is running roughly every %.0f minutes instead of every 5, and "
                   "Bandcamp's feed only holds 10 minutes at a time. Everything below counts "
                   "<em>observed</em> sales only &mdash; real platform totals are roughly "
                   "<strong>%.1f&times;</strong> these figures. Fixing the collection frequency "
                   "fixes this.</div>"
                   % (observed * 100, _median_gap_minutes(sales), 1.0 / max(observed, 0.01)))

    if len(complete) < 60:
        out.append("<div class='notice'><strong>Still filling up.</strong> "
                   "7-day comparisons need 14 complete days; 30-day comparisons need 60. "
                   "You have <strong>%d</strong>. Panels below show what is missing rather "
                   "than a number calculated from a partial day.</div>" % len(complete))

    # Headline tiles
    out.append("<section class='tiles'>")
    out.append("<div class='tile'><div class='label'>Fan spend collected</div>"
               "<div class='big'>%s</div><div class='sub'>%s sales</div></div>"
               % (money(total["spend"]), "{:,}".format(total["n"])))
    out.append("<div class='tile'><div class='label'>Bandcamp's estimated cut</div>"
               "<div class='big'>%s</div><div class='sub'>take rate %s</div></div>"
               % (money_range(total["fee_low"], total["fee_high"]),
                  rate_range(total["fee_low"], total["fee_high"], total["spend"])))
    out.append("<div class='tile'><div class='label'>Digital</div>"
               "<div class='big'>%s</div><div class='sub'>%s sales &middot; take %s</div></div>"
               % (money(total["dig_spend"]), "{:,}".format(total["dig_n"]),
                  rate_range(total["dig_low"], total["dig_high"], total["dig_spend"])))
    out.append("<div class='tile'><div class='label'>Physical</div>"
               "<div class='big'>%s</div><div class='sub'>%s sales &middot; take %s</div></div>"
               % (money(total["phy_spend"]), "{:,}".format(total["phy_n"]),
                  rate_range(total["phy_low"], total["phy_high"], total["phy_spend"])))
    out.append("</section>")

    # Chart
    out.append("<section class='panel'><h2>Fan spend per day</h2>%s"
               "<div class='scroll'>%s</div></section>" % (legend, chart_svg))

    # Trailing periods
    out.append("<section class='panel'><h2>Trailing periods &mdash; all media</h2>"
               "<div class='cards'>")
    out.append(metric_card("Trailing 7 days · average per day", cur7, prior7, status7,
                           per_day=True, n=7))
    out.append(metric_card("Trailing 30 days · total", cur30, prior30, status30, n=30))
    out.append("</div></section>")

    for scope, title in (("digital", "Digital"), ("physical", "Physical")):
        out.append("<section class='panel'><h2>Trailing periods &mdash; %s</h2>"
                   "<div class='cards'>" % title)
        out.append(metric_card("Trailing 7 days · average per day", cur7, prior7, status7,
                               per_day=True, n=7, scope=scope))
        out.append(metric_card("Trailing 30 days · total", cur30, prior30, status30,
                               n=30, scope=scope))
        out.append("</div></section>")

    # Table view
    out.append("<section class='panel'><details><summary>Show the numbers as a table</summary>"
               "<div class='scroll'><table><thead><tr>"
               "<th>Date</th><th>Fan spend</th><th>Digital</th><th>Physical</th>"
               "<th>Sales</th><th>Bandcamp cut</th><th>Take rate</th><th>Day observed</th>"
               "</tr></thead><tbody>")
    for d in reversed(dates):
        day = days.get(d, EMPTY)
        c = cov.get(d, 0.0)
        out.append("<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td><td>%s</td><td>%.0f%%%s</td></tr>"
                   % (" class='partial-row'" if c < COMPLETE_THRESHOLD else "",
                      d, money_exact(day["spend"]), money_exact(day["dig_spend"]),
                      money_exact(day["phy_spend"]), "{:,}".format(day["n"]),
                      money_range(day["fee_low"], day["fee_high"]),
                      rate_range(day["fee_low"], day["fee_high"], day["spend"]),
                      c * 100, " (partial)" if c < COMPLETE_THRESHOLD else ""))
    out.append("</tbody></table></div></details></section>")

    out.append(FOOTNOTE)
    out.append("</div><div id='tip' role='status'></div>")
    out.append(SCRIPT)
    return "".join(out)


def main():
    sales = load_sales()
    if not sales:
        print("No sales data yet. Run: python collect.py")
        return
    days = aggregate(sales)
    gaps = load_gaps()
    cov = coverage(days, sales, gaps)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(render(days, cov, sales, gaps))
    print("Wrote %s (%s sales across %d days)" % (OUT_PATH, "{:,}".format(len(sales)), len(days)))


if __name__ == "__main__":
    main()
