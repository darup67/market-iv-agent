"""Upcoming catalyst dates per ticker.

Sources, merged:
  - RTTNews FDA calendar (PDUFA dates, FDA panels)      rttnews.com/corpinfo/fdacalendar.aspx
  - RTTNews clinical-trial calendar (topline readouts)  rttnews.com/corpinfo/ClinicalTrialCalendar.aspx
  - catalysts_manual.json: events the calendars miss, taken from company filings
Earnings dates come from Yahoo in agent.py.

Both calendars are paginated HTML and are cached in data/catalysts.json for a day.
Only events still pending are kept. Dates are either exact (08/27/2026) or windows
("Q4 2026", "2H 2026", "Mid 2026", "Oct 2026"), which are parsed to a start/end range.
"""
import datetime as dt
import html
import json
import re
import ssl
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "catalysts.json"
MANUAL = ROOT / "catalysts_manual.json"
BASE = "https://www.rttnews.com/corpinfo/"
CALENDARS = {"fda": "fdacalendar.aspx", "trial": "ClinicalTrialCalendar.aspx"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DONE = re.compile(r"approv|reported|met |meets|miss|fail|complete|announced|rejected|crl|withdr|positive|negative",
                  re.I)


try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30, context=_SSL).read().decode("utf-8", "ignore")


def _text(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _rows(page):
    out = []
    for blk in page.split('class="grid-row')[1:]:
        cells = dict(re.findall(r'data-th="([^"]+)"[^>]*>(.*?)</div>', blk, re.S))
        if cells:
            out.append(cells)
    return out


def parse_when(s, year_hint=None):
    """'08/27/2026' -> (d, d). 'Q4 2026' / '2H 2026' / 'Mid 2026' / 'Oct 2026' -> (start, end)."""
    s = s.strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        d = dt.date(int(m[3]), int(m[1]), int(m[2]))
        return d, d
    y = re.search(r"(20\d\d)", s)
    if not y:
        return None, None
    y = int(y[1])
    low = s.lower()
    q = re.search(r"q([1-4])", low)
    if q:
        k = int(q[1])
        return dt.date(y, 3 * k - 2, 1), dt.date(y, 3 * k, 30 if k in (2, 3) else 31)
    if re.search(r"\b(1h|first half|h1)\b", low):
        return dt.date(y, 1, 1), dt.date(y, 6, 30)
    if re.search(r"\b(2h|second half|h2)\b", low):
        return dt.date(y, 7, 1), dt.date(y, 12, 31)
    if "mid" in low:
        return dt.date(y, 5, 1), dt.date(y, 8, 31)
    if "early" in low:
        return dt.date(y, 1, 1), dt.date(y, 4, 30)
    if "late" in low or "end" in low:
        return dt.date(y, 9, 1), dt.date(y, 12, 31)
    for name, n in MONTHS.items():
        if re.search(rf"\b{name}", low):
            end = (dt.date(y + (n == 12), n % 12 + 1, 1) - dt.timedelta(days=1))
            return dt.date(y, n, 1), end
    return dt.date(y, 1, 1), dt.date(y, 12, 31)


def _fetch(kind, max_pages=40):
    events, seen = [], set()
    for page_no in range(1, max_pages + 1):
        page = _get(f"{BASE}{CALENDARS[kind]}?PageNum={page_no}")
        rows = _rows(page)
        new = 0
        for c in rows:
            if kind == "fda":
                tickers = re.findall(r">([A-Z]{1,5})</a>", c.get("Company Name", ""))
                ev = c.get("Event", "")
                when = _text((re.search(r"<span[^>]*>(.*?)</span>", ev, re.S) or [None, ""])[1])
                what = _text(re.sub(r"<span[^>]*>.*?</span>", "", ev, count=1, flags=re.S))
                drug = _text(c.get("Drug", ""))
                label = f"FDA: {drug}" if drug else "FDA decision"
            else:
                tickers = re.findall(r">([A-Z]{1,5})</a>", c.get("Ticker", ""))
                when = _text(c.get("Results Date", ""))
                what = _text(c.get("Event", ""))
                ind = _text(c.get("Indication", ""))
                label = f"{what} ({ind})" if ind else what
                what = label
            outcome = _text(c.get("Outcome", "")).strip(" -")
            key = (tuple(tickers), when, what)
            if key in seen:
                continue
            seen.add(key)
            new += 1
            if DONE.search(outcome) and "pending" not in outcome.lower():
                continue
            start, end = parse_when(when)
            for t in tickers:
                events.append({"ticker": t, "when": when, "start": start and start.isoformat(),
                               "end": end and end.isoformat(), "event": label if kind == "fda" else what,
                               "detail": what, "source": f"RTTNews {kind}"})
        if not rows or not new:
            break
        time.sleep(1)
    return events


def load(max_age_hours=20, log=print):
    """All pending catalysts, keyed by ticker. Uses the cache unless it is stale."""
    events = None
    if CACHE.exists():
        c = json.loads(CACHE.read_text())
        age = time.time() - c.get("fetched", 0)
        if age < max_age_hours * 3600:
            events = c["events"]
    if events is None:
        try:
            events = _fetch("fda") + _fetch("trial")
            CACHE.parent.mkdir(exist_ok=True)
            CACHE.write_text(json.dumps({"fetched": time.time(), "events": events}))
            log(f"catalysts: fetched {len(events)} pending events")
        except Exception as e:
            log(f"catalyst calendar fetch failed ({e}); using stale cache if any")
            events = json.loads(CACHE.read_text())["events"] if CACHE.exists() else []
    have = {e["ticker"] for e in events}
    if MANUAL.exists():
        for m in json.loads(MANUAL.read_text()):
            if m["ticker"] in have:
                continue        # the calendars already cover it; manual entries only fill gaps
            start, end = parse_when(m["when"])
            events.append({"ticker": m["ticker"], "when": m["when"], "start": start and start.isoformat(),
                           "end": end and end.isoformat(), "event": m["event"],
                           "detail": m.get("source", ""), "source": "manual"})
    by = {}
    for e in events:
        by.setdefault(e["ticker"], []).append(e)
    return by


def upcoming(by_ticker, ticker, today, earnings=None, horizon_days=120):
    """Pending events for one ticker that could land from today to today+horizon, soonest first."""
    out = []
    limit = today + dt.timedelta(days=horizon_days)
    for e in by_ticker.get(ticker, []):
        if not e["start"]:
            continue
        start, end = dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"])
        if end < today or start > limit:
            continue
        out.append(dict(e, _start=start, _end=end, exact=start == end))
    if isinstance(earnings, str):
        d = dt.date.fromisoformat(earnings)
        if today <= d <= limit:
            out.append({"ticker": ticker, "when": d.strftime("%m/%d/%Y"), "event": "Earnings",
                        "_start": d, "_end": d, "exact": True, "source": "Yahoo"})
    out.sort(key=lambda e: (e["_start"], e["_end"]))
    return out


def describe(e, front_exp):
    """One-line label, flagged when the event can land before the front option expiry."""
    fe = dt.date.fromisoformat(front_exp)
    if e["exact"]:
        when = e["_start"].strftime("%b %d")
        flag = " ⚠ before " + fe.strftime("%b %d") if e["_start"] <= fe else ""
    else:
        when = e["when"]
        flag = " (window open, may land before " + fe.strftime("%b %d") + ")" if e["_start"] <= fe else ""
    ev = e["event"] if len(e["event"]) <= 90 else e["event"][:87] + "…"
    return f"{when}: {ev}{flag}"


if __name__ == "__main__":
    import sys
    by = load()
    today = dt.date.today()
    for t in (sys.argv[1:] or ["AVBP", "GILD", "KOD", "QURE"]):
        print(t, [describe(e, (today + dt.timedelta(days=22)).isoformat()) for e in upcoming(by, t, today)])
