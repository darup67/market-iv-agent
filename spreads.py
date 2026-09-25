"""Bull call spreads for the top Bull-labelled names, with a fixed act-on rule.

For each of the top `spreads.candidates` Bull names in the email pool whose explode
score is at least `spreads.min_explode` (by bias):
  expiry  the first expiry `min_dte`..`max_dte` days out
  long    the listed call nearest spot
  short   the listed call nearest the 1σ bull level (spot·e^{σ√t}), above the long
  limit   leg mids, net, rounded UP to $0.05

Act-on rule (fixed before looking at any result; do not tune it on outcomes):
  1. liquidity: both legs have a bid, each leg's bid/ask spread is ≤ max_leg_spread of
     its mid OR ≤ $0.10 wide (a dime is as tight as a cheap option gets), and each
     leg's open interest ≥ the order quantity
  2. still labelled Bull at run time
  3. rank the survivors by bias (ties: higher probability of profit); up to `act_on`
     (5) are acted on, each sized at budget_total / act_on. Fewer pass on thin days;
     the email says so rather than loosening the rule to reach a count.

Changed 2026-09-24 at the user's request ("2-5 exploding bull call spread tickers
per email"): min_explode gate added, act_on 2 -> 5, candidates 10 -> 15.
Others are shown as "watch only" with the reason.

Construction "odds" (default since 2026-09-24, user: "only market odds of profit of 50%
or more; reduce risk, add guardrails"): instead of ATM long / 1σ short, every listed
call pair within ±25% of spot on that expiry is tried, and a pair qualifies only if
  - P(profit) = P(close above breakeven) >= min_p_profit (0.50)
  - max gain / risk >= min_reward_risk (0.5), so a deep-ITM spread that is mostly cost is out
  - both legs pass the liquidity gates for the order size
  - strike width >= min_width_pct (2.5%) of spot and qty <= max_qty (20): a 50-cent-wide
    spread x80 lots looks great on paper but one cent of slippage per leg eats its edge
The qualifying pair with the best reward/risk wins (ties: higher P(profit)). Higher odds
come from buying an in-the-money call, so the breakeven sits at or below spot; the price
is a smaller max gain. Risk-neutral expected value is about the cost either way: this
lowers how often a spread loses, not what it is worth. "atm_1sigma" restores the old rule.
A catalyst or earnings date before expiry is flagged, not excluded.

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


def bs_call(spot, k, iv, t, r=0.04):
    d1 = (math.log(spot / k) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    return spot * _N(d1) - k * math.exp(-r * t) * _N(d1 - iv * math.sqrt(t))


def pick_odds(calls, spot, iv, t, c, budget):
    """Best (reward/risk) call pair meeting the odds, payoff and liquidity guardrails.
    Returns (long_k, short_k, limit, qty) or a string saying why none qualified."""
    if not iv or iv <= 0 or t <= 0:
        return "no usable IV for this expiry"
    ks = [k for k in calls.index if 0.75 * spot <= k <= 1.25 * spot]
    best, why = None, "no strikes near spot"
    seen_odds = 0
    for i, k1 in enumerate(ks):
        L = calls.loc[k1]
        for k2 in ks[i + 1:]:
            H = calls.loc[k2]
            mid = ((L.bid or 0) + (L.ask or 0)) / 2 - ((H.bid or 0) + (H.ask or 0)) / 2
            width = k2 - k1
            if width < c.get("min_width_pct", 0) * spot:
                continue
            if mid <= 0 or mid >= width:
                continue
            limit = max(0.05, math.ceil(mid * 20 - 1e-9) / 20)  # never $0: a sub-cent mid rounded to 0 and divided by zero
            if limit >= width:
                continue
            # Fair-value guard: a quoted mid far from the model value is a stale quote, not an edge
            # (VKTX 29/30.5 quoted $0.70 against ~$1.45 fair at 09:53 on 2026-09-25, "86% odds").
            theo = bs_call(spot, k1, iv, t) - bs_call(spot, k2, iv, t)
            if abs(mid - theo) > max(0.10, c.get("max_fair_dev", 0.2) * width):
                why = "quotes inconsistent with fair value (stale)"
                continue
            p = p_above(spot, k1 + limit, iv, t)
            rr = (width - limit) / limit
            if p < c["min_p_profit"]:
                continue
            seen_odds += 1
            if rr < c["min_reward_risk"]:
                why = f"pairs with P(profit) >= {c['min_p_profit']:.0%} pay under {c['min_reward_risk']}x risk"
                continue
            qty = min(int(budget // (limit * 100)), c.get("max_qty", 10 ** 6))
            if qty < 1:
                continue
            bad = leg_ok(L, qty, c["max_leg_spread"]) or leg_ok(H, qty, c["max_leg_spread"])
            if bad:
                why = f"odds-qualified pairs fail liquidity ({bad})"
                continue
            key = (rr, p)
            if best is None or key > best[0]:
                best = (key, k1, k2, limit, qty)
    if best:
        return best[1:]
    return why if seen_odds or why != "no strikes near spot" else f"no pair reaches P(profit) {c['min_p_profit']:.0%}"


def event_before(r, exp):
    """True if earnings or a flagged catalyst can land on or before the spread's expiry."""
    e = getattr(r, "earnings", None)
    if isinstance(e, str) and e <= exp:
        return True
    cat = getattr(r, "catalysts", "") or ""
    return "⚠" in cat or "window open" in cat


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
    iv, t = pt["iv"], pt["t"]
    calls = pt["_calls"].drop_duplicates("strike").set_index("strike").sort_index()
    spot = r.spot
    if c.get("construction", "odds") == "odds":
        got = pick_odds(calls, spot, iv, t, c, budget)
        if isinstance(got, str):
            return {"ticker": r.ticker, "fail": got}
        long_k, short_k, limit, qty = got
        L, H = calls.loc[long_k], calls.loc[short_k]
        width, be = short_k - long_k, long_k + limit
        return _ticket(r, exp, spot, iv, t, long_k, short_k, L, H, limit, qty, width, be)
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
    limit = max(0.05, math.ceil(mid * 20 - 1e-9) / 20)  # never $0: a sub-cent mid rounded to 0 and divided by zero
    qty = int(budget // (limit * 100))
    be = long_k + limit
    return _ticket(r, exp, spot, iv, t, long_k, short_k, L, H, limit, qty, width, be)


def _ticket(r, exp, spot, iv, t, long_k, short_k, L, H, limit, qty, width, be):
    return {
        "ticker": r.ticker, "sector": r.sector, "bias": r.bias, "bias_label": r.bias_label, "spot": spot,
        "exp": exp, "long": long_k, "short": short_k, "long_q": (L.bid, L.ask, int(L.openInterest or 0)),
        "short_q": (H.bid, H.ask, int(H.openInterest or 0)), "limit": limit, "natural": (L.ask or 0) - (H.bid or 0),
        "qty": qty, "cost": qty * limit * 100, "max_gain": qty * (width - limit) * 100,
        "rr": (width - limit) / limit, "be": be, "p_profit": p_above(spot, be, iv, t),
        "p_max": p_above(spot, short_k, iv, t), "iv": iv, "catalysts": getattr(r, "catalysts", ""),
        "event_before_exp": event_before(r, exp),
        "_L": L, "_H": H,
    }


def plan(pool, cfg, today, atm_for_expiry):
    """Tickets for the top Bull candidates; the first `act_on` passing the rule are marked act."""
    c = cfg["spreads"]
    bulls = pool[(pool.bias_label == "Bull") & (pool.score >= c.get("min_explode", 0))]
    bulls = bulls.sort_values("bias", ascending=False).head(c["candidates"])
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
        tk["explode"] = float(r.score)
        tickets.append(tk)
    n = 0
    for tk in sorted(tickets, key=lambda x: (-x["bias"], -x.get("p_profit", 0))):
        tk["act"] = not tk.get("fail") and n < c["act_on"]
        n += tk["act"]
    return tickets
