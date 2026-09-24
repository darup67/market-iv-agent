#!/usr/bin/env python3
"""Health care implied-volatility agent (biotech, pharma, medical devices, services).

Scans every holding of the SPDR health-care ETFs (XBI biotech, XPH pharma, XHE
medical devices, XHS health care services, XLV large-cap health care) plus a list
of foreign/large-cap pharma names once a day, and
emails two lists:

  1. Highest IV: ranked by 30-day at-the-money implied volatility.
  2. Next to explode: names whose options are pricing a big move *soon*
     (front-month IV well above the back months, IV above realized vol, heavy
     volume against open interest, IV rising day over day, a large implied
     move). This finds priced-in binary catalysts such as FDA dates, trial
     readouts and earnings. It says nothing about direction.

Read-only. Email is the only output channel: no banner and no sound.

Usage:
  agent.py              scan, save the snapshot, send the email
  agent.py --dry        scan and write preview.html; no email, no history write
  agent.py --tickers MRNA,VRTX --dry   scan a subset (for testing)
  agent.py --test-email send a one-line test email
"""
import argparse
import datetime as dt
import html
import io
import json
import math
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

import logging

import pandas as pd
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HISTORY = DATA / "history.csv"      # one row per ticker per day; drives IV rank + 1d change
PICKS = DATA / "picks.csv"          # explode picks, for the scorecard
CFG = json.loads((ROOT / "config.json").read_text())
ETFS = {e["etf"] for e in CFG["universe_etfs"]} | {"IBB", "IHI"}


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- universe
def load_universe():
    """{ticker: sector} from the SPDR health-care ETF holdings. The first ETF listed wins a ticker's sector."""
    out = {}
    for spec in CFG["universe_etfs"]:
        etf, sector = spec["etf"], spec["sector"]
        cached = DATA / f"holdings-{etf.lower()}.txt"
        tickers = []
        try:
            url = CFG["universe_url_template"].format(etf=etf.lower())
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=30).read()
            df = pd.read_excel(io.BytesIO(raw), header=4)
            tickers = [str(t).strip() for t in df["Ticker"].dropna()
                       if str(t).strip().isalpha() and str(t).strip().isupper()]
            cached.write_text("\n".join(tickers))
        except Exception as e:
            log(f"{etf} holdings download failed ({e}); using cache")
            if cached.exists():
                tickers = cached.read_text().split()
        for t in tickers:
            out.setdefault(t, sector)
    for t in CFG["extra_tickers"]:
        out.setdefault(t, "ETF" if t in ETFS else CFG["extra_sector"])
    return out


# ---------------------------------------------------------------- Black-Scholes IV
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(s, k, t, r, vol, call):
    if vol <= 0 or t <= 0:
        return max(0.0, (s - k) if call else (k - s))
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if call:
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)


def implied_vol(price, s, k, t, r, call):
    intrinsic = max(0.0, (s - k) if call else (k - s))
    if price is None or price <= intrinsic + 1e-4 or t <= 0:
        return None
    lo, hi = 0.01, 10.0
    if bs_price(s, k, t, r, hi, call) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(s, k, t, r, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- per-ticker scan
RISK_FREE = 0.04


def atm_for_expiry(tk, exp, spot, today):
    """ATM IV, implied move, volume and OI for one expiry."""
    dte = (dt.date.fromisoformat(exp) - today).days
    t = max(dte, 0.5) / 365.0
    ch = tk.option_chain(exp)
    calls, puts = ch.calls, ch.puts
    if calls.empty or puts.empty:
        return None
    # Median IV over the 3 strikes nearest spot, calls and puts, skipping quotes
    # with no bid or a spread wider than max_spread_pct of mid. Single ATM quotes are often
    # stale (e.g. a put priced 4x through parity), which a median shrugs off.
    ivs = []
    for df, is_call in ((calls, True), (puts, False)):
        near = df.assign(d=(df.strike - spot).abs()).nsmallest(3, "d")
        for _, row in near.iterrows():
            b, a = row.get("bid") or 0, row.get("ask") or 0
            if b <= 0 or a < b or (a - b) / ((a + b) / 2) > CFG["max_spread_pct"]:
                continue
            v = implied_vol((a + b) / 2, spot, row.strike, t, RISK_FREE, is_call)
            if v and v < 5.0:       # >500% here means a stale or crossed quote, not a real IV
                ivs.append(v)
    if len(ivs) < 2:
        return None
    ivs.sort()
    iv = ivs[len(ivs) // 2] if len(ivs) % 2 else (ivs[len(ivs) // 2 - 1] + ivs[len(ivs) // 2]) / 2
    vol = float(calls.volume.fillna(0).sum() + puts.volume.fillna(0).sum())
    oi = float(calls.openInterest.fillna(0).sum() + puts.openInterest.fillna(0).sum())
    cvol = float(calls.volume.fillna(0).sum())
    # ATM straddle ≈ S·σ·√t·√(2/π); expressed as a fraction of spot
    move = iv * math.sqrt(t) * math.sqrt(2 / math.pi)
    return {"exp": exp, "dte": dte, "iv": iv, "n_quotes": len(ivs),
            "move": move, "volume": vol, "oi": oi, "call_volume": cvol}


def interp_iv(points, target_dte):
    """Total-variance interpolation of ATM IV to target_dte."""
    pts = sorted(points, key=lambda x: x["dte"])
    below = [p for p in pts if p["dte"] <= target_dte]
    above = [p for p in pts if p["dte"] >= target_dte]
    if not below:
        return above[0]["iv"]
    if not above:
        return below[-1]["iv"]
    a, b = below[-1], above[0]
    if a["dte"] == b["dte"]:
        return a["iv"]
    va, vb = a["iv"] ** 2 * a["dte"], b["iv"] ** 2 * b["dte"]
    w = (target_dte - a["dte"]) / (b["dte"] - a["dte"])
    return math.sqrt(max(va + w * (vb - va), 1e-8) / target_dte)


def hv(closes, n):
    r = (closes / closes.shift(1)).apply(math.log).dropna().tail(n)
    return float(r.std() * math.sqrt(252)) if len(r) >= max(5, n // 2) else None


def scan_ticker(sym, today):
    for attempt in range(4):
        try:
            return _scan(sym, today)
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "429" in msg or "Rate" in msg:
                time.sleep(15 * (attempt + 1))
                continue
            return {"ticker": sym, "error": msg[:120]}
    return {"ticker": sym, "error": "rate limited"}


def _scan(sym, today):
    tk = yf.Ticker(sym)
    hist = tk.history(period="6mo", auto_adjust=False)
    if hist.empty:
        return {"ticker": sym, "error": "no price history"}
    closes = hist["Close"].dropna()
    spot = float(closes.iloc[-1])
    if spot < CFG["min_price"]:
        return {"ticker": sym, "error": f"price {spot:.2f} below min"}
    exps = [e for e in tk.options
            if CFG["min_dte"] <= (dt.date.fromisoformat(e) - today).days <= CFG["max_dte"]]
    if not exps:
        return {"ticker": sym, "error": "no listed options in window"}

    # front expiry, the ~30d expiry(s) for interpolation, and a ~75d back month
    dtes = {e: (dt.date.fromisoformat(e) - today).days for e in exps}
    pick = {exps[0]}
    under30 = [e for e in exps if dtes[e] <= 30]
    over30 = [e for e in exps if dtes[e] >= 30]
    if under30:
        pick.add(under30[-1])
    if over30:
        pick.add(over30[0])
    pick.add(min(exps, key=lambda e: abs(dtes[e] - 75)))
    points = [p for p in (atm_for_expiry(tk, e, spot, today) for e in sorted(pick)) if p]
    if not points:
        return {"ticker": sym, "error": "illiquid options (no clean ATM quotes)"}

    oi = sum(p["oi"] for p in points)
    if oi < CFG["min_open_interest"]:
        return {"ticker": sym, "error": f"open interest {oi:.0f} below min"}
    vol = sum(p["volume"] for p in points)
    cvol = sum(p["call_volume"] for p in points)
    points.sort(key=lambda p: p["dte"])
    front, back = points[0], points[-1]
    hv20 = hv(closes, 20)
    iv30 = interp_iv(points, 30)

    earnings = None
    try:
        if sym in ETFS:
            raise ValueError("ETFs have no earnings")
        cal = tk.calendar or {}
        ed = cal.get("Earnings Date") or []
        future = [d for d in ed if isinstance(d, dt.date) and d >= today]
        earnings = future[0].isoformat() if future else None
    except Exception:
        pass

    return {
        "ticker": sym,
        "spot": round(spot, 2),
        "chg_5d": round(float(closes.iloc[-1] / closes.iloc[-6] - 1), 4) if len(closes) > 6 else None,
        "iv30": round(iv30, 4),
        "front_iv": round(front["iv"], 4),
        "front_exp": front["exp"],
        "front_dte": front["dte"],
        "back_iv": round(back["iv"], 4),
        "back_exp": back["exp"],
        # needs two distinct expiries; a single surviving expiry says nothing about term structure
        "term_ratio": round(front["iv"] / back["iv"], 3) if back["dte"] - front["dte"] >= 14 else None,
        # only a near-dated move is an "about to happen" signal
        "implied_move": round(front["move"], 4) if front["dte"] <= 45 else None,
        "hv20": round(hv20, 4) if hv20 else None,
        "iv_hv": round(iv30 / hv20, 3) if hv20 else None,
        "opt_volume": int(vol),
        "opt_oi": int(oi),
        "vol_oi": round(vol / oi, 3) if oi else None,
        "call_share": round(cvol / vol, 3) if vol else None,
        "earnings": earnings,
    }


# ---------------------------------------------------------------- history-based fields
def enrich_with_history(df, today):
    if not HISTORY.exists():
        df["iv_change_1d"] = None
        df["iv_rank"] = None
        df["hist_days"] = 0
        return df
    h = pd.read_csv(HISTORY)
    h = h[h["date"] < today.isoformat()]
    prev = h.sort_values("date").groupby("ticker").tail(1).set_index("ticker")["iv30"]
    stats = h.groupby("ticker")["iv30"].agg(["min", "max", "count"])
    df["iv_change_1d"] = df.apply(
        lambda r: round(r.iv30 - prev[r.ticker], 4) if r.ticker in prev.index else None, axis=1)

    def rank(r):
        if r.ticker not in stats.index or stats.loc[r.ticker, "count"] < 20:
            return None
        lo = min(stats.loc[r.ticker, "min"], r.iv30)
        hi = max(stats.loc[r.ticker, "max"], r.iv30)
        return round(100 * (r.iv30 - lo) / (hi - lo), 0) if hi > lo else None

    df["iv_rank"] = df.apply(rank, axis=1)
    df["hist_days"] = df["ticker"].map(stats["count"]).fillna(0).astype(int)
    return df


def explode_score(df):
    w = CFG["explode_weights"]
    total = pd.Series(0.0, index=df.index)
    wsum = 0.0
    for col, wt in w.items():
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() < 5:
            continue          # no history yet (e.g. iv_change_1d on day 1): drop the factor, renormalise
        total += s.rank(pct=True).fillna(0.5) * wt
        wsum += wt
    df["score"] = (100 * total / wsum).round(0) if wsum else 0
    return df


def reasons(r, today):
    out = []
    if pd.notna(r.term_ratio) and r.term_ratio >= 1.15:
        out.append(f"front IV {r.term_ratio:.2f}× back → event priced by {r.front_exp[5:]}")
    if pd.notna(r.implied_move) and r.implied_move >= 0.08:
        out.append(f"±{r.implied_move*100:.0f}% implied by {r.front_exp[5:]}")
    if r.iv_hv and r.iv_hv >= 1.3:
        out.append(f"IV {r.iv_hv:.1f}× realized")
    if r.vol_oi and r.vol_oi >= 0.5:
        side = ""
        if r.call_share is not None:
            side = " calls" if r.call_share >= 0.65 else " puts" if r.call_share <= 0.35 else ""
        out.append(f"options vol {r.vol_oi:.1f}× OI{side}")
    if pd.notna(r.get("iv_change_1d")) and r.iv_change_1d >= 0.05:
        out.append(f"IV +{r.iv_change_1d*100:.0f} pts today")
    if isinstance(r.earnings, str):
        days = (dt.date.fromisoformat(r.earnings) - today).days
        if days <= CFG["earnings_window_days"]:
            out.append(f"earnings {r.earnings[5:]} ({days}d)")
    return "; ".join(out) or "—"


# ---------------------------------------------------------------- scorecard
def scorecard(df, today):
    """How did the explode picks from N sessions ago actually move?"""
    if not PICKS.exists():
        return None
    p = pd.read_csv(PICKS)
    dates = sorted(d for d in p["date"].unique() if d < today.isoformat())
    lag = CFG["scorecard_lag_sessions"]
    if len(dates) < lag:
        return None
    d0 = dates[-lag]
    old = p[p["date"] == d0].merge(df[["ticker", "spot"]], on="ticker", suffixes=("_then", "_now"))
    if old.empty:
        return None
    old["move"] = (old.spot_now / old.spot_then - 1)
    old["beat"] = old.move.abs() >= old.implied_move
    return d0, old


# ---------------------------------------------------------------- email
def pct(x, d=0):
    return "—" if x is None or pd.isna(x) else f"{x*100:.{d}f}%"


def build_html(df, today, errors, card):
    top_iv = df.sort_values("iv30", ascending=False).head(CFG["top_iv"])
    top_ex = df.sort_values("score", ascending=False).head(CFG["top_explode"])
    th = 'style="text-align:left;padding:4px 8px;border-bottom:1px solid #ccc;font-size:12px;color:#555"'
    td = 'style="padding:4px 8px;border-bottom:1px solid #eee;font-size:13px"'

    def table(rows, cols):
        h = "".join(f"<th {th}>{c}</th>" for c, _ in cols)
        body = ""
        for _, r in rows.iterrows():
            body += "<tr>" + "".join(f"<td {td}>{f(r)}</td>" for _, f in cols) + "</tr>"
        return f'<table style="border-collapse:collapse;width:100%">{h}{body}</table>'

    has_rank = df["iv_rank"].notna().any()
    stocks = df[df.sector != "ETF"]
    sec = stocks.groupby("sector").agg(n=("ticker", "size"), med=("iv30", "median"),
                                       top=("iv30", "idxmax")).sort_values("med", ascending=False)
    sector_html = "".join(
        f"<tr><td {td}>{name}</td><td {td}>{int(r.n)}</td><td {td}>{pct(r.med)}</td>"
        f"<td {td}>{df.loc[r.top, 'ticker']} {pct(df.loc[r.top, 'iv30'])}</td></tr>"
        for name, r in sec.iterrows())
    iv_cols = [
        ("Ticker", lambda r: f"<b>{r.ticker}</b>"),
        ("Sector", lambda r: r.sector),
        ("Price", lambda r: f"${r.spot:,.2f}"),
        ("IV30", lambda r: f"<b>{pct(r.iv30)}</b>"),
        ("Δ1d", lambda r: "—" if pd.isna(r.iv_change_1d) else f"{r.iv_change_1d*100:+.0f}"),
    ]
    if has_rank:
        iv_cols.append(("IV rank", lambda r: "—" if pd.isna(r.iv_rank) else f"{r.iv_rank:.0f}"))
    iv_cols += [
        ("HV20", lambda r: pct(r.hv20)),
        ("Front", lambda r: f"{pct(r.front_iv)} {r.front_exp[5:]}"),
        ("Implied move", lambda r: "—" if pd.isna(r.implied_move) else "±" + pct(r.implied_move)),
        ("Earnings", lambda r: r.earnings[5:] if isinstance(r.earnings, str) else "—"),
    ]
    ex_cols = [
        ("Ticker", lambda r: f"<b>{r.ticker}</b>"),
        ("Sector", lambda r: r.sector),
        ("Score", lambda r: f"<b>{r.score:.0f}</b>"),
        ("Price", lambda r: f"${r.spot:,.2f}"),
        ("5d", lambda r: pct(r.chg_5d, 1)),
        ("IV30", lambda r: pct(r.iv30)),
        ("Why", lambda r: html.escape(r.why)),
    ]
    card_html = ""
    if card:
        d0, old = card
        n, beat = len(old), int(old.beat.sum())
        rows = "".join(
            f"<tr><td {td}>{r.ticker}</td><td {td}>±{r.implied_move*100:.0f}%</td>"
            f"<td {td}>{r.move*100:+.1f}%</td><td {td}>{'✓' if r.beat else '·'}</td></tr>"
            for _, r in old.sort_values("move", key=abs, ascending=False).iterrows())
        card_html = (f"<h3 style='font-family:sans-serif'>Scorecard: picks from {d0}</h3>"
                     f"<p style='font-family:sans-serif;font-size:13px'>{beat}/{n} moved more than their "
                     f"front-expiry implied move.</p>"
                     f"<table style='border-collapse:collapse'><tr><th {th}>Ticker</th><th {th}>Implied</th>"
                     f"<th {th}>Actual</th><th {th}>Beat</th></tr>{rows}</table>")
    hist_note = ("" if has_rank else
                 "<p style='font-size:12px;color:#777'>IV rank appears after 20 days of saved history. "
                 "The 1-day IV change starts on day 2.</p>")
    return f"""<div style="font-family:-apple-system,Helvetica,sans-serif;max-width:900px">
<h2>Health care IV · {today:%a %b %d}</h2>
<p style="font-size:13px;color:#555">{len(df)} optionable names scanned: biotech, pharma, medical devices, health care services and large-cap health care (SPDR XBI/XPH/XHE/XHS/XLV holdings).</p>
<table style="border-collapse:collapse"><tr><th {th}>Sector</th><th {th}>Names</th><th {th}>Median IV30</th><th {th}>Highest</th></tr>{sector_html}</table>
<h3>🚀 Next to explode</h3>
<p style="font-size:12px;color:#777">Ranks names whose options price a large move soon. It is not a forecast of direction: a high score means the market expects a big move, and the premium already reflects that.</p>
{table(top_ex, ex_cols)}
<h3>🔥 Highest implied volatility</h3>
{table(top_iv, iv_cols)}
{hist_note}
{card_html}
<p style="font-size:11px;color:#999;margin-top:24px">Data: Yahoo Finance option chains (end of day), SPDR ETF holdings. ATM IV is computed from bid/ask mids.
{len(errors)} tickers were skipped (no options, low open interest, or price under ${CFG['min_price']:.0f}). Read-only agent, not investment advice.</p>
</div>"""


def gmail_password():
    pw = os.environ.get("FLIP_GMAIL_APP_PASSWORD")
    if pw:
        return pw
    return subprocess.run(
        ["security", "find-generic-password", "-a", CFG["keychain_account"], "-s", CFG["keychain_service"], "-w"],
        capture_output=True, text=True, check=True).stdout.strip()


def send_email(subject, body_html):
    user = CFG["keychain_account"]
    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"Health Care IV Agent <{user}>"
    msg["To"] = CFG["email_to"]
    pw = gmail_password()
    last = None
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, pw)
                s.sendmail(user, [CFG["email_to"]], msg.as_string())
            return True
        except Exception as e:
            last = e
            log(f"email attempt {attempt+1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    log(f"email FAILED after 3 attempts: {last}")
    return False


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="no email, no history write; writes preview.html")
    ap.add_argument("--tickers", help="comma-separated subset")
    ap.add_argument("--test-email", action="store_true")
    a = ap.parse_args()
    DATA.mkdir(exist_ok=True)

    if a.test_email:
        ok = send_email("🧬 Biotech IV agent: test email", "<p>Test from ~/biotech-iv-agent. Email delivery works.</p>")
        sys.exit(0 if ok else 1)

    today = dt.date.today()
    if today.weekday() >= 5 and not a.dry and not a.tickers:
        log("weekend, skipping")
        return
    universe = load_universe()
    if a.tickers:
        universe = {t: universe.get(t, "?") for t in a.tickers.split(",")}
    log(f"scanning {len(universe)} tickers")
    t0 = time.time()
    rows, errors = [], []
    with ThreadPoolExecutor(CFG["workers"]) as ex:
        futs = {ex.submit(scan_ticker, s, today): s for s in universe}
        for f in as_completed(futs):
            r = f.result()
            r["sector"] = universe[futs[f]]
            (errors if "error" in r else rows).append(r)
    log(f"scanned in {time.time()-t0:.0f}s: {len(rows)} ok, {len(errors)} skipped")
    reasons_count = pd.Series([e["error"].split(" ")[0] + " " + e["error"].split(" ")[1] if " " in e["error"]
                               else e["error"] for e in errors]).value_counts()
    log("skip reasons: " + ", ".join(f"{k} ×{v}" for k, v in reasons_count.items()))
    if not rows:
        log("nothing scanned; aborting")
        sys.exit(1)

    df = pd.DataFrame(rows)
    df = enrich_with_history(df, today)
    df = explode_score(df)
    df["why"] = df.apply(lambda r: reasons(r, today), axis=1)
    card = scorecard(df, today)

    top_iv = df.sort_values("iv30", ascending=False).iloc[0]
    top_ex = df.sort_values("score", ascending=False).iloc[0]
    subject = (f"🧬 Health care IV · Top IV {top_iv.ticker} {top_iv.iv30*100:.0f}% · "
               f"Watch {top_ex.ticker}" + (f" (±{top_ex.implied_move*100:.0f}% by {top_ex.front_exp[5:]})" if pd.notna(top_ex.implied_move) else ""))
    body = build_html(df, today, errors, card)

    if a.dry:
        (ROOT / "preview.html").write_text(body)
        log("DRY: wrote preview.html")
        log("subject:", subject)
        print(df.sort_values("score", ascending=False)
              [["ticker", "sector", "score", "iv30", "term_ratio", "iv_hv", "vol_oi", "implied_move", "why"]]
              .head(12).to_string(index=False))
        return

    # save the snapshot before the email, so a mail failure does not lose the day's IV history
    df["date"] = today.isoformat()
    df.to_csv(DATA / f"snapshot-{today}.csv", index=False)
    keep = ["date", "ticker", "sector", "spot", "iv30", "front_iv", "back_iv", "hv20", "term_ratio", "implied_move",
            "vol_oi", "score"]
    hist = df[keep]
    if HISTORY.exists():
        old = pd.read_csv(HISTORY)
        hist = pd.concat([old[old["date"] != today.isoformat()], hist])
    hist.to_csv(HISTORY, index=False)
    picks = df.sort_values("score", ascending=False).head(CFG["top_explode"])[
        ["date", "ticker", "spot", "score", "implied_move", "front_exp"]]
    if PICKS.exists():
        oldp = pd.read_csv(PICKS)
        picks = pd.concat([oldp[oldp["date"] != today.isoformat()], picks])
    picks.to_csv(PICKS, index=False)

    if CFG["email_enabled"]:
        ok = send_email(subject, body)
        log("email sent" if ok else "email failed")
        if not ok:
            sys.exit(2)
    else:
        log("email disabled in config.json")


if __name__ == "__main__":
    main()
