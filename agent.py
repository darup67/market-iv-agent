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

import catalysts
import spreads

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
    # with no bid, a spread wider than max_spread_pct of mid, or open interest
    # under min_quote_oi. Single ATM quotes are often
    # stale (e.g. a put priced 4x through parity), which a median shrugs off.
    ivs = []
    for df, is_call in ((calls, True), (puts, False)):
        near = df.assign(d=(df.strike - spot).abs()).nsmallest(3, "d")
        for _, row in near.iterrows():
            b, a = row.get("bid") or 0, row.get("ask") or 0
            if b <= 0 or a < b or (a - b) / ((a + b) / 2) > CFG["max_spread_pct"]:
                continue
            if (row.get("openInterest") or 0) < CFG["min_quote_oi"]:
                continue            # untraded strikes carry stale quotes (PBH 45C bid 11.5 on a $45.7 stock)
            v = implied_vol((a + b) / 2, spot, row.strike, t, RISK_FREE, is_call)
            if v and v < 5.0:       # >500% here means a stale or crossed quote, not a real IV
                ivs.append(v)
    if len(ivs) < 2:
        return None
    ivs.sort()
    iv = ivs[len(ivs) // 2] if len(ivs) % 2 else (ivs[len(ivs) // 2 - 1] + ivs[len(ivs) // 2]) / 2
    calls, puts = fresh_volume(calls), fresh_volume(puts)
    vol = float(calls.vol_today.sum() + puts.vol_today.sum())
    oi = float(calls.openInterest.fillna(0).sum() + puts.openInterest.fillna(0).sum())
    cvol = float(calls.vol_today.sum())
    # ATM straddle ≈ S·σ·√t·√(2/π); expressed as a fraction of spot
    move = iv * math.sqrt(t) * math.sqrt(2 / math.pi)
    return {"exp": exp, "dte": dte, "iv": iv, "n_quotes": len(ivs),
            "move": move, "volume": vol, "oi": oi, "call_volume": cvol,
            "t": t, "_calls": calls, "_puts": puts}


def fresh_volume(df):
    """Yahoo keeps a contract's volume until it trades again, so a strike last
    traded days ago still shows old volume. Count only the latest session."""
    df = df.copy()
    ltd = pd.to_datetime(df["lastTradeDate"], utc=True, errors="coerce").dt.tz_convert("America/New_York").dt.date
    session = ltd.max()
    df["vol_today"] = df["volume"].fillna(0).where(ltd == session, 0)
    return df


# ---------------------------------------------------------------- call/put deep dive
def side_iv(df, spot, sd, t, is_call):
    """OTM IV near 1σ: the clean-quoted strike between 0.5σ and 1.5σ OTM closest to 1σ; None if none."""
    if df.empty:
        return None
    sign = 1 if is_call else -1
    lo, hi = sorted((spot * math.exp(sign * 0.5 * sd), spot * math.exp(sign * 1.5 * sd)))
    target = spot * math.exp(sign * sd)
    cand = df[(df.strike >= lo) & (df.strike <= hi)]
    for _, row in cand.assign(d=(cand.strike - target).abs()).sort_values("d").iterrows():
        b, a = row.get("bid") or 0, row.get("ask") or 0
        if b <= 0 or a < b or (a - b) / ((a + b) / 2) > CFG["max_spread_pct"]:
            continue
        v = implied_vol((a + b) / 2, spot, row.strike, t, RISK_FREE, is_call)
        if v and v < 5.0:
            return v
    return None


def unusual_activity(points, spot):
    """Contracts whose volume today is large, above open interest (new positions),
    and carries real premium. Uses every fetched expiry, not just the near ones."""
    u = CFG["uoa"]
    hits = []
    for p in points:
        for df, is_call in ((p["_calls"], True), (p["_puts"], False)):
            for _, r in df[df.vol_today >= u["min_volume"]].iterrows():
                oi = int(r.get("openInterest") or 0)
                b, a, last = r.get("bid") or 0, r.get("ask") or 0, r.get("lastPrice") or 0
                prem = r.vol_today * last * 100
                if r.vol_today < u["min_vol_oi"] * max(oi, 1) or prem < u["min_premium"]:
                    continue
                if b > 0 and a >= b and last > 0:
                    side = "bought" if last >= (a + b) / 2 else "sold"
                    lean = "Bull" if (side == "bought") == is_call else "Bear"
                else:
                    side, lean = "?", "?"
                hits.append({"contract": f"{p['exp'][5:]} ${r.strike:g}{'C' if is_call else 'P'}",
                             "dte": p["dte"], "volume": int(r.vol_today), "oi": oi,
                             "premium": round(prem), "side": side, "lean": lean,
                             "otm": round((r.strike / spot - 1) * (1 if is_call else -1), 3)})
    hits.sort(key=lambda h: -h["premium"])
    return hits[:5]


def flow_metrics(points, spot):
    """Call vs put positioning across the near-term chains (≤45 DTE, else the front one).

    - pc_vol / pc_oi: put/call ratios of today's volume and of open interest
    - rr: 1σ risk reversal, OTM call IV − OTM put IV (positive = upside priced richer)
    - net_prem: signed premium; a trade printing at/above mid counts as bought,
      below mid as sold. Bullish = calls bought + puts sold. Only the LAST print per
      contract is visible, so this is an estimate, not true order flow.
    - bias: −100..+100 blend of the four, labelled Bull / Bear / Base
    """
    near = [p for p in points if p["dte"] <= 45] or points[:1]
    cv = sum(p["_calls"].vol_today.sum() for p in near)
    pv = sum(p["_puts"].vol_today.sum() for p in near)
    coi = sum(p["_calls"].openInterest.fillna(0).sum() for p in near)
    poi = sum(p["_puts"].openInterest.fillna(0).sum() for p in near)

    signed, gross, strikes = 0.0, 0.0, []
    uoa = unusual_activity(points, spot)
    for p in near:
        for df, is_call in ((p["_calls"], True), (p["_puts"], False)):
            for _, r in df[df.vol_today > 0].iterrows():
                b, a, last = r.get("bid") or 0, r.get("ask") or 0, r.get("lastPrice") or 0
                prem = r.vol_today * last * 100
                if b > 0 and a >= b and last > 0:
                    bought = last >= (a + b) / 2
                    sign = (1 if bought else -1) * (1 if is_call else -1)
                    signed += sign * prem
                    side = "bought" if bought else "sold"
                else:
                    side = "?"
                gross += prem
                strikes.append((r.vol_today, f"{p['exp'][5:]} ${r.strike:g}{'C' if is_call else 'P'}",
                                side, int(r.get("openInterest") or 0)))
    strikes.sort(reverse=True)

    # 1σ risk reversal on the ~30d-nearest near-term expiry
    ref = min(near, key=lambda p: abs(p["dte"] - 30))
    sd = ref["iv"] * math.sqrt(ref["t"])
    civ = side_iv(ref["_calls"], spot, sd, ref["t"], True)
    piv = side_iv(ref["_puts"], spot, sd, ref["t"], False)
    rr = (civ - piv) if civ and piv else None

    pc_vol = pv / cv if cv else None
    pc_oi = poi / coi if coi else None
    comps = []   # each clipped to −1..+1, positive = bullish
    if pc_vol is not None and cv + pv >= 50:
        comps.append((max(-1, min(1, -math.log(max(pc_vol, 1e-3)) / math.log(3))), 0.30))
    if pc_oi is not None:
        comps.append((max(-1, min(1, -math.log(max(pc_oi, 1e-3)) / math.log(3))), 0.20))
    if rr is not None:
        comps.append((max(-1, min(1, rr / 0.15)), 0.20))
    if gross > 0:
        comps.append((signed / gross, 0.30))
    bias = round(100 * sum(c * w for c, w in comps) / sum(w for _, w in comps)) if comps else None
    th = CFG["bias_threshold"]
    label = None if bias is None else "Bull" if bias >= th else "Bear" if bias <= -th else "Base"

    # Bear / Base / Bull price levels from the front near-term expiry, skew-aware:
    # downside uses the OTM put IV, upside the OTM call IV.
    f = near[0]
    fsd_up = (civ or f["iv"]) * math.sqrt(f["t"])
    fsd_dn = (piv or f["iv"]) * math.sqrt(f["t"])
    return {
        "pc_vol": round(pc_vol, 2) if pc_vol is not None else None,
        "pc_oi": round(pc_oi, 2) if pc_oi is not None else None,
        "rr": round(rr, 3) if rr is not None else None,
        "net_prem": round(signed),
        "gross_prem": round(gross),
        "bias": bias,
        "bias_label": label,
        "case_exp": f["exp"],
        "bear": round(spot * math.exp(-fsd_dn), 2),              # ~16% odds of closing below
        "base_lo": round(spot * math.exp(-0.674 * fsd_dn), 2),  # 50% odds inside base range
        "base_hi": round(spot * math.exp(0.674 * fsd_up), 2),
        "bull": round(spot * math.exp(fsd_up), 2),               # ~16% odds of closing above
        "uoa": json.dumps(uoa) if uoa else "",
        "uoa_n": len(uoa),
        "top_strikes": " | ".join(f"{k} {int(v):,} {sd_}" + (f" (OI {oi:,})" if oi else "")
                                  for v, k, sd_, oi in strikes[:3]),
    }


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
    flow = flow_metrics(points, spot)
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
        **flow,
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
    if "bias_label" not in old:
        old["bias_label"] = None
    old["dir_ok"] = old.apply(lambda r: None if r.bias_label not in ("Bull", "Bear")
                              else (r.move > 0) == (r.bias_label == "Bull"), axis=1)
    return d0, old


# ---------------------------------------------------------------- email
def pct(x, d=0):
    return "—" if x is None or pd.isna(x) else f"{x*100:.{d}f}%"


def email_pool(df):
    """Only names with real options activity today make the email lists."""
    return df[df.opt_volume >= CFG["min_email_volume"]]


BIAS_STYLE = {"Bull": ("🟢", "#1a7f37"), "Bear": ("🔴", "#cf222e"), "Base": ("⚪", "#666")}


def bias_badge(r):
    if not isinstance(r.bias_label, str):
        return "—"
    icon, color = BIAS_STYLE[r.bias_label]
    return f'<span style="color:{color};font-weight:600">{icon} {r.bias_label} {r.bias:+.0f}</span>'


def money(x):
    x = float(x)
    return f"{'-' if x < 0 else '+'}${abs(x)/1e6:.1f}M" if abs(x) >= 1e6 else f"{'-' if x < 0 else '+'}${abs(x)/1e3:.0f}k"


def uoa_rows(r):
    return json.loads(r.uoa) if isinstance(r.uoa, str) and r.uoa else []


def uoa_text(r):
    rows = uoa_rows(r)
    return " | ".join(f"{h['contract']} {h['volume']:,} vs OI {h['oi']:,} {h['side']} {money(h['premium'])[1:]}"
                      for h in rows[:2]) or "—"


def uoa_section(df, th, td):
    u = CFG["uoa"]
    rows = [dict(h, ticker=r.ticker, sector=r.sector, spot=r.spot)
            for _, r in df.iterrows() for h in uoa_rows(r)]
    rows.sort(key=lambda h: -h["premium"])
    for h in rows:
        h["ratio"] = "new" if h["oi"] == 0 else f"{h['volume'] / h['oi']:.1f}×"
        m = h["otm"]
        h["mny"] = "ATM" if abs(m) < 0.01 else f"{abs(m) * 100:.0f}% {'OTM' if m > 0 else 'ITM'}"
    if not rows:
        return "<p style='font-size:13px'>No contract met the unusual-activity rules today.</p>"
    lean = {"Bull": "🟢 Bull", "Bear": "🔴 Bear", "?": "—"}
    body = "".join(
        f"<tr><td {td}><b>{h['ticker']}</b></td><td {td}>{h['sector']}</td><td {td}>{h['contract']}</td>"
        f"<td {td}>{h['volume']:,}</td><td {td}>{h['oi']:,}</td>"
        f"<td {td}>{h['ratio']}</td>"
        f"<td {td}>{money(h['premium'])[1:]}</td><td {td}>{h['side']}</td><td {td}>{lean[h['lean']]}</td>"
        f"<td {td}>{h['mny']}</td></tr>"
        for h in rows[:u["max_rows"]])
    head = "".join(f"<th {th}>{c}</th>" for c in
                   ("Ticker", "Sector", "Contract", "Vol today", "OI", "Vol/OI", "Premium", "Side", "Lean", "Strike vs price"))
    return (f"<p style='font-size:12px;color:#777'>Contracts trading at least {u['min_volume']:,} today, above "
            f"{u['min_vol_oi']:g}× open interest (new positions, not closing trades), with at least "
            f"{money(u['min_premium'])[1:]} premium. Covers every scanned name, not only the lists above. "
            f"Side is estimated from the last print vs mid. A bought call or sold put leans Bull. "
            f"Sold options are often one leg of a spread.</p>"
            f"<table style='border-collapse:collapse;width:100%'>{head}{body}</table>")


def spreads_section(tickets, th, td):
    c = CFG["spreads"]
    if not tickets:
        return "<p style='font-size:13px'>No Bull-labelled names in today's pool, so no spreads.</p>"
    act = [t for t in tickets if t.get("act")]
    head = "".join(f"<th {th}>{h}</th>" for h in (
        "", "Ticker", "Bias", "Expiry", "Buy (bid/ask, OI)", "Sell (bid/ask, OI)", "Limit", "Qty", "Cost = max loss",
        "Max gain", "R:R", "Breakeven", "P(profit)", "P(max)", "Catalyst"))
    body = ""
    for t in tickets:
        if t.get("limit") is None:
            body += (f"<tr><td {td}>👀</td><td {td}><b>{t['ticker']}</b></td><td {td}>{t['bias']:+.0f}</td>"
                     f"<td {td} colspan=12>watch only: {html.escape(t['fail'])}</td></tr>")
            continue
        lb, la, lo = t["long_q"]
        hb, ha, ho = t["short_q"]
        mark = "✅ ACT" if t["act"] else "👀"
        why = "" if t["act"] else f"<br><span style='color:#999;font-size:11px'>watch only: {html.escape(t['fail'] or 'outranked')}</span>"
        style = td if t["act"] else td.replace('font-size:13px', 'font-size:13px;color:#777')
        body += (f"<tr><td {style}><b>{mark}</b></td><td {style}><b>{t['ticker']}</b> ${t['spot']:,.2f}{why}</td>"
                 f"<td {style}>{t['bias']:+.0f}</td><td {style}>{t['exp'][5:]}</td>"
                 f"<td {style}>${t['long']:g}C ({lb:.2f}/{la:.2f}, {lo:,})</td>"
                 f"<td {style}>${t['short']:g}C ({hb:.2f}/{ha:.2f}, {ho:,})</td>"
                 f"<td {style}>${t['limit']:.2f}</td><td {style}>{t['qty']}</td><td {style}>${t['cost']:,.0f}</td>"
                 f"<td {style}>${t['max_gain']:,.0f}</td><td {style}>{t['rr']:.1f}</td>"
                 f"<td {style}>${t['be']:,.2f} ({t['be'] / t['spot'] - 1:+.0%})</td>"
                 f"<td {style}>{t['p_profit']:.0%}</td><td {style}>{t['p_max']:.0%}</td>"
                 f"<td {style}>{html.escape(t.get('catalysts') or '—')}</td></tr>")
    total = sum(t["cost"] for t in act)
    summary = (f"<b>Act on {len(act)} of {len(tickets)}:</b> {', '.join(t['ticker'] for t in act)} · "
               f"${total:,.0f} of ${c['budget_total']:,.0f}" if act else
               "<b>None of today's candidates passed the rule.</b> No spread to act on.")
    return (f"<p style='font-size:13px'>{summary}</p>"
            f"<p style='font-size:12px;color:#777'>Top {c['candidates']} Bull names by bias. Each spread buys the call "
            f"nearest the price and sells the call nearest the 1σ bull level, on the first expiry "
            f"{c['min_dte']}–{c['max_dte']} days out, at a limit of mid rounded up to $0.05. <b>Act-on rule</b> (fixed, not tuned on "
            f"results): both legs bid, each leg's spread under {c['max_leg_spread']:.0%} of mid or $0.10 wide, open interest at least "
            f"the order size, still Bull; the top {c['act_on']} by bias (ties: higher P(profit)) split "
            f"${c['budget_total']:,.0f}. P(profit) and P(max) are the options market's own odds, so each spread's expected "
            f"payoff is roughly its cost. Most expire worthless. Delayed quotes: re-check prices before entering. "
            f"This is a screen, not advice. Orders are entered by hand.</p>"
            f"<table style='border-collapse:collapse;width:100%'>{head}{body}</table>")


def deep_dive(r, td):
    num = lambda x, f="{:.2f}": "—" if x is None or pd.isna(x) else f.format(x)
    rr = "—" if pd.isna(r.rr) else f"{r.rr*100:+.0f} pts ({'calls' if r.rr > 0 else 'puts'} richer)"
    net = f"{money(r.net_prem)} of {money(r.gross_prem)[1:]} traded"
    cases = (f"🔴 Bear <b>${r.bear:,.2f}</b> ({pct(r.bear/r.spot-1,0)}) · "
             f"⚪ Base <b>${r.base_lo:,.2f}–${r.base_hi:,.2f}</b> · "
             f"🟢 Bull <b>${r.bull:,.2f}</b> ({pct(r.bull/r.spot-1,0)})")
    return f"""<div style="border:1px solid #ddd;border-radius:6px;padding:10px 12px;margin:10px 0">
<div style="font-size:15px"><b>{r.ticker}</b> ${r.spot:,.2f} · {bias_badge(r)} <span style="color:#777;font-size:12px">· {r.sector} · score {r.score:.0f}</span></div>
<div style="font-size:13px;margin-top:6px">{cases} <span style="color:#777">by {r.case_exp[5:]}</span></div>
<table style="border-collapse:collapse;margin-top:6px">
<tr><td {td}>Catalysts</td><td {td}>{html.escape(r.catalysts) or "none on the FDA/trial calendars or earnings in the next 120 days"}</td></tr>
<tr><td {td}>Options volume today</td><td {td}>{int(r.opt_volume):,} ({pct(r.call_share)} calls) · put/call {num(r.pc_vol)}</td></tr>
<tr><td {td}>Open interest</td><td {td}>{int(r.opt_oi):,} · put/call {num(r.pc_oi)}</td></tr>
<tr><td {td}>Skew (1σ risk reversal)</td><td {td}>{rr}</td></tr>
<tr><td {td}>Net premium (est.)</td><td {td}>{net}</td></tr>
<tr><td {td}>Unusual activity</td><td {td}>{uoa_text(r)}</td></tr>
<tr><td {td}>Busiest contracts</td><td {td}>{html.escape(r.top_strikes) or "—"}</td></tr>
</table></div>"""


def build_html(df, today, errors, card, tickets=()):
    pool = email_pool(df)
    top_iv = pool.sort_values("iv30", ascending=False).head(CFG["top_iv"])
    top_ex = pool.sort_values("score", ascending=False).head(CFG["top_explode"])
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
        ("Bias", bias_badge),
        ("Opt vol", lambda r: f"{int(r.opt_volume):,}"),
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
        ("Bias", bias_badge),
        ("Opt vol", lambda r: f"{int(r.opt_volume):,}"),
        ("P/C", lambda r: "—" if pd.isna(r.pc_vol) else f"{r.pc_vol:.2f}"),
        ("Catalyst", lambda r: html.escape(r.catalysts) or "none found"),
        ("Why", lambda r: html.escape(r.why)),
    ]
    card_html = ""
    if card:
        d0, old = card
        n, beat = len(old), int(old.beat.sum())
        called = old[old.dir_ok.notna()]
        dir_line = (f" Bias called direction {int(called.dir_ok.sum())}/{len(called)} times (Bull/Bear only)."
                    if len(called) else "")
        rows = "".join(
            f"<tr><td {td}>{r.ticker}</td><td {td}>{r.bias_label if isinstance(r.bias_label, str) else '—'}</td>"
            f"<td {td}>±{r.implied_move*100:.0f}%</td>"
            f"<td {td}>{r.move*100:+.1f}%</td><td {td}>{'✓' if r.beat else '·'}</td>"
            f"<td {td}>{'—' if r.dir_ok is None or pd.isna(r.dir_ok) else '✓' if r.dir_ok else '✗'}</td></tr>"
            for _, r in old.sort_values("move", key=abs, ascending=False).iterrows())
        card_html = (f"<h3 style='font-family:sans-serif'>Scorecard: picks from {d0}</h3>"
                     f"<p style='font-family:sans-serif;font-size:13px'>{beat}/{n} moved more than their "
                     f"front-expiry implied move.{dir_line}</p>"
                     f"<table style='border-collapse:collapse'><tr><th {th}>Ticker</th><th {th}>Bias</th>"
                     f"<th {th}>Implied</th><th {th}>Actual</th><th {th}>Beat move</th><th {th}>Direction</th></tr>"
                     f"{rows}</table>")
    # names already carded in the explode section are pointed to, not repeated
    shown = set(top_ex.ticker)
    dup = [t for t in top_iv.ticker if t in shown]
    iv_cards = "".join(deep_dive(r, td) for _, r in top_iv.iterrows() if r.ticker not in shown)
    if dup:
        iv_cards += (f'<p style="font-size:12px;color:#777">Also in the top IV list, with cards above: '
                     f'{", ".join(dup)}.</p>')
    hist_note = ("" if has_rank else
                 "<p style='font-size:12px;color:#777'>IV rank appears after 20 days of saved history. "
                 "The 1-day IV change starts on day 2.</p>")
    return f"""<div style="font-family:-apple-system,Helvetica,sans-serif;max-width:900px">
<h2>Health care IV · {today:%a %b %d}</h2>
<p style="font-size:13px;color:#555">{len(df)} optionable names scanned; {len(pool)} traded at least {CFG['min_email_volume']:,} option contracts today and are eligible for the lists below. Universe: biotech, pharma, medical devices, health care services and large-cap health care (SPDR XBI/XPH/XHE/XHS/XLV holdings).</p>
<table style="border-collapse:collapse"><tr><th {th}>Sector</th><th {th}>Names</th><th {th}>Median IV30</th><th {th}>Highest</th></tr>{sector_html}</table>
<h3>🚀 Next to explode</h3>
<p style="font-size:12px;color:#777">Ranks names whose options price a large move soon. It is not a forecast of direction: a high score means the market expects a big move, and the premium already reflects that.</p>
{table(top_ex, ex_cols)}
<h3>🎯 Bull call spreads</h3>
{spreads_section(tickets, th, td)}
<h3>🔍 Call/put deep dive: next to explode</h3>
<p style="font-size:12px;color:#777">Bias (−100 to +100) blends today's put/call volume (30%), put/call open interest (20%), 1σ skew (20%) and estimated net premium (30%). Bull at +{CFG['bias_threshold']} or more, Bear at −{CFG['bias_threshold']} or less, otherwise Base. Net premium counts a trade at or above mid as bought, which is an estimate: Yahoo shows only each contract's last print. Bear/Base/Bull prices come from the options' own implied distribution, using put IV for the downside and call IV for the upside. There is about a 16% chance of finishing below Bear, 50% inside Base, and 16% above Bull.</p>
{"".join(deep_dive(r, td) for _, r in top_ex.iterrows())}
<h3>⚡ Unusual options activity</h3>
{uoa_section(df, th, td)}
<h3>🔥 Highest implied volatility</h3>
{table(top_iv, iv_cols)}
<h3>🔍 Call/put deep dive: highest IV</h3>
{iv_cards}
{hist_note}
{card_html}
<p style="font-size:11px;color:#999;margin-top:24px">Data: Yahoo Finance option chains (end of day), SPDR ETF holdings, RTTNews FDA and clinical-trial calendars (pending events only; windows like "Q4 2026" are company guidance, not fixed dates). ATM IV is computed from bid/ask mids.
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
    ap.add_argument("--tag", help="prefix for the email subject, e.g. '9:45 open screen'")
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
    cal = catalysts.load(log=log)
    df["catalysts"] = df.apply(lambda r: " · ".join(
        catalysts.describe(e, r.front_exp)
        for e in catalysts.upcoming(cal, r.ticker, today, r.earnings)[:CFG["max_catalysts"]]), axis=1)
    card = scorecard(df, today)

    pool = email_pool(df) if len(email_pool(df)) else df
    top_iv = pool.sort_values("iv30", ascending=False).iloc[0]
    top_ex = pool.sort_values("score", ascending=False).iloc[0]
    subject = (f"🧬 Health care IV · Top IV {top_iv.ticker} {top_iv.iv30*100:.0f}% · "
               f"Watch {top_ex.ticker}" + (f" {top_ex.bias_label}" if isinstance(top_ex.bias_label, str) else "") + (f" (±{top_ex.implied_move*100:.0f}% by {top_ex.front_exp[5:]})" if pd.notna(top_ex.implied_move) else ""))
    tickets = spreads.plan(email_pool(df), CFG, today, atm_for_expiry)
    if a.tag:
        subject = f"[{a.tag}] {subject}"
    body = build_html(df, today, errors, card, tickets)

    if a.dry:
        (ROOT / "preview.html").write_text(body)
        log("DRY: wrote preview.html")
        log("subject:", subject)
        print(df.sort_values("score", ascending=False)
              [["ticker", "sector", "score", "opt_volume", "bias_label", "bias", "pc_vol", "rr", "bear", "bull"]]
              .head(12).to_string(index=False))
        return

    # save the snapshot before the email, so a mail failure does not lose the day's IV history
    df["date"] = today.isoformat()
    df.to_csv(DATA / f"snapshot-{today}.csv", index=False)
    keep = ["date", "ticker", "sector", "spot", "iv30", "front_iv", "back_iv", "hv20", "term_ratio", "implied_move",
            "vol_oi", "score", "opt_volume", "pc_vol", "pc_oi", "rr", "net_prem", "bias", "bias_label"]
    hist = df[keep]
    if HISTORY.exists():
        old = pd.read_csv(HISTORY)
        hist = pd.concat([old[old["date"] != today.isoformat()], hist])
    hist.to_csv(HISTORY, index=False)
    picks = email_pool(df).sort_values("score", ascending=False).head(CFG["top_explode"])[
        ["date", "ticker", "spot", "score", "implied_move", "front_exp", "bias", "bias_label",
         "bear", "base_lo", "base_hi", "bull"]]
    if PICKS.exists():
        oldp = pd.read_csv(PICKS)
        picks = pd.concat([oldp[oldp["date"] != today.isoformat()], picks])
    picks.to_csv(PICKS, index=False)

    if tickets:
        tk = pd.DataFrame([{k: v for k, v in t.items() if not k.startswith("_")} for t in tickets])
        tk.insert(0, "date", today.isoformat())
        path = DATA / "spreads.csv"
        if path.exists():
            old = pd.read_csv(path)
            tk = pd.concat([old[old["date"] != today.isoformat()], tk])
        tk.to_csv(path, index=False)

    if CFG["email_enabled"]:
        ok = send_email(subject, body)
        log("email sent" if ok else "email failed")
        if not ok:
            sys.exit(2)
    else:
        log("email disabled in config.json")


if __name__ == "__main__":
    main()
