"""Bull call spreads for the top Bull-labelled names, with a fixed act-on rule.

For each of the top `spreads.candidates` Bull names in the email pool (by bias):
  expiry  the first expiry `min_dte`..`max_dte` days out
  long    the listed call nearest spot
  short   the listed call nearest the 1σ bull level (spot·e^{σ√t}), above the long
  limit   leg mids, net, rounded UP to $0.05

Act-on rule (fixed before looking at any result; do not tune it on outcomes):
  1. liquidity: both legs have a bid, each leg's bid/ask spread is ≤ max_leg_spread of
     its mid OR ≤ $0.10 wide (a dime is as tight as a cheap option gets), and each
     leg's open interest ≥ the order quantity
  2. still labelled Bull at run time
  3. rank the survivors by bias (ties: higher probability of profit); the top
     `act_on` split `budget_total` equally
Others are shown as "watch only" with the reason.

Probabilities are risk-neutral (from the ATM IV): the market's own odds, not a forecast.
Read-only: this prints tickets, it never places orders.
"""
import datetime as dt
import math
from statistics import NormalDist

import yfinance as yf

_N = NormalDist().cdf


def p_above(spot, k, iv, t):
    return 1 - _N((math.log(k / spot) + 0.5 * iv * iv * t) / (iv * math.sqrt(t)))


def leg_ok(row, qty, max_spread):
    b, a = row.bid or 0, row.ask or 0
    if b <= 0 or a < b:
        return "no bid"
    if (a - b) / ((a + b) / 2) > max_spread and (a - b) > 0.10 + 1e-9:
        return f"${b:.2f}/${a:.2f} spread too wide"
    if (row.openInterest or 0) < qty:
        return f"OI {int(row.openInterest or 0)} < {qty} contracts"
    return None


def build(r, cfg, today, budget, atm_for_expiry):
    """One spread ticket for row r of the scan. Returns a dict (with 'fail' set if it can't be built)."""
    c = cfg["spreads"]
    tk = yf.Ticker(r.ticker)
    exps = [e for e in tk.options if c["min_dte"] <= (dt.date.fromisoformat(e) - today).days <= c["max_dte"]]
    if not exps:
        return {"ticker": r.ticker, "fail": "no expiry in window"}
    exp = exps[0]
    pt = atm_for_expiry(tk, exp, r.spot, today)
    if not pt:
        return {"ticker": r.ticker, "fail": "no clean ATM quotes"}
    iv, t, calls = pt["iv"], pt["t"], pt["_calls"].set_index("strike").sort_index()
    spot = r.spot
    long_k = min(calls.index, key=lambda k: abs(k - spot))
    target = spot * math.exp(iv * math.sqrt(t))
    above = [k for k in calls.index if k > long_k]
    if not above:
        return {"ticker": r.ticker, "fail": "no strike above the long leg"}
    short_k = min(above, key=lambda k: abs(k - target))
    L, H = calls.loc[long_k], calls.loc[short_k]
    mid = ((L.bid or 0) + (L.ask or 0)) / 2 - ((H.bid or 0) + (H.ask or 0)) / 2
    width = short_k - long_k
    if mid <= 0 or mid >= width:
        return {"ticker": r.ticker, "fail": "spread mid not tradable"}
    limit = math.ceil(mid * 20 - 1e-9) / 20
    qty = int(budget // (limit * 100))
    be = long_k + limit
    return {
        "ticker": r.ticker, "sector": r.sector, "bias": r.bias, "bias_label": r.bias_label, "spot": spot,
        "exp": exp, "long": long_k, "short": short_k, "long_q": (L.bid, L.ask, int(L.openInterest or 0)),
        "short_q": (H.bid, H.ask, int(H.openInterest or 0)), "limit": limit, "natural": (L.ask or 0) - (H.bid or 0),
        "qty": qty, "cost": qty * limit * 100, "max_gain": qty * (width - limit) * 100,
        "rr": (width - limit) / limit, "be": be, "p_profit": p_above(spot, be, iv, t),
        "p_max": p_above(spot, short_k, iv, t), "iv": iv, "catalysts": getattr(r, "catalysts", ""),
        "_L": L, "_H": H,
    }


def plan(pool, cfg, today, atm_for_expiry):
    """Tickets for the top Bull candidates; the first `act_on` passing the rule are marked act."""
    c = cfg["spreads"]
    bulls = pool[pool.bias_label == "Bull"].sort_values("bias", ascending=False).head(c["candidates"])
    if bulls.empty:
        return []
    per = c["budget_total"] / c["act_on"]
    tickets = []
    for _, r in bulls.iterrows():
        try:
            tk = build(r, cfg, today, per, atm_for_expiry)
        except Exception as e:
            tk = {"ticker": r.ticker, "fail": f"error: {str(e)[:60]}"}
        if "fail" not in tk:
            reasons = [f"buy leg {m}" for m in [leg_ok(tk["_L"], tk["qty"], c["max_leg_spread"])] if m]
            reasons += [f"sell leg {m}" for m in [leg_ok(tk["_H"], tk["qty"], c["max_leg_spread"])] if m]
            tk["fail"] = "; ".join(reasons) or None
        tk.setdefault("bias", r.bias)
        tk.setdefault("sector", r.sector)
        tickets.append(tk)
    n = 0
    for tk in sorted(tickets, key=lambda x: (-x["bias"], -x.get("p_profit", 0))):
        tk["act"] = not tk.get("fail") and n < c["act_on"]
        n += tk["act"]
    return tickets
