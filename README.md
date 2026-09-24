# Health care IV agent

A daily email that ranks US health care stocks (biotech, pharma, medical devices,
health care services and large-cap health care) by **implied volatility**, and by
how strongly their options price a **big move soon**.

Read-only. It never trades. Email is the only channel: no banner and no sound.

## What it does

1. **Universe.** Every holding of the SPDR XBI (biotech), XPH (pharma), XHE
   (medical devices), XHS (health care services) and XLV (large-cap health care)
   ETFs, downloaded fresh each run, plus foreign and large-cap pharma listed in
   `config.json`. About 380 names; roughly 270 have usable options.
2. **Per ticker** (Yahoo Finance option chains, via `yfinance`):
   - ATM IV per expiry: the median Black-Scholes IV over the 3 strikes nearest spot,
     calls and puts, from bid/ask mids. Quotes with no bid, a spread wider than
     `max_spread_pct`, or an IV above 500% are dropped, and at least 2 clean quotes
     are required. Single ATM quotes are often stale.
   - **IV30**: ATM IV interpolated to 30 days in total variance.
   - **Term ratio**: front-expiry IV ÷ ~75-day IV. Above 1 means an event is priced
     before the front expiry.
   - **IV / HV20**: implied vs 20-day realized volatility.
   - **Volume / OI**, and the call share of volume.
   - **Implied move** to the front expiry (≤45 DTE only).
   - Next earnings date.
3. **Next-to-explode score** (0–100): weighted percentile rank across the universe
   of term ratio (30%), IV/HV (20%), vol/OI (20%), 1-day IV change (15%) and implied
   move (15%). A factor with no data yet, such as the 1-day change on day 1, is
   dropped and the weights are renormalised.

4. **Call/put deep dive** on the same near-term chains (≤45 DTE), using only
   today's volume (Yahoo keeps a contract's old volume until it trades again, so
   stale rows are zeroed):
   - put/call ratio of volume and of open interest
   - 1σ risk reversal: OTM call IV − OTM put IV
   - estimated net premium: a print at or above mid counts as bought. Bullish =
     calls bought + puts sold. Only each contract's last print is visible.
   - **Bias** −100..+100 = 30% volume P/C, 20% OI P/C, 20% skew, 30% net premium.
     **Bull** ≥ +25, **Bear** ≤ −25, otherwise **Base** (`bias_threshold`).
   - **Bear / Base / Bull prices** from the front expiry's implied distribution,
     with put IV on the downside and call IV on the upside: about 16% odds below
     Bear, 50% inside the Base range and 16% above Bull.
5. **Catalysts** (`catalysts.py`): pending events from the RTTNews FDA calendar
   (PDUFA dates, panels) and clinical-trial calendar (topline readouts), plus Yahoo
   earnings, cached for a day in `data/catalysts.json`. Company-guided windows
   ("Q4 2026", "2H 2026") are kept as windows. An event is flagged ⚠ when it can
   land before the front option expiry. `catalysts_manual.json` fills gaps for
   tickers the calendars don't cover.
6. **Volume filter:** only names that traded at least `min_email_volume` (500)
   option contracts today appear in the email lists.

**What the score means:** the options market expects a large move soon, which
usually means an FDA date, a trial readout or earnings. It does **not** predict
direction, and the premium already prices the move. The email's scorecard checks
the picks from 5 sessions ago against their implied move, to keep the score honest.

## Email

Weekdays at 16:30 ET (launchd `com.dhruv.healthiv`, StartCalendarInterval) to
darup67@gmail.com. It uses the Gmail app password in the Keychain
(`-a darup67@gmail.com -s flip-notifier-gmail`), the same one as flip-notifier
and zillow-agent.

- Subject: top IV name and the top "watch" name.
- Sector summary: median IV30 and the highest name per sector.
- 🚀 Next to explode: top 10, with the bias and the reasons behind each score.
- 🎯 Bull call spreads (`spreads.py`, config `spreads`): top 10 Bull names by bias (`candidates`).
  Buy the call nearest the price and sell the call nearest the 1σ bull level, on the
  first expiry 21–45 days out, at a limit of mid rounded up to $0.05. Fixed act-on
  rule: both legs bid, each leg under 50% of mid or $0.10 wide, OI ≥ order size,
  still Bull. The top 2 by bias (ties: higher P(profit)) split $10k. The rest
  are "watch only" with the reason. Tickets are saved to `data/spreads.csv`.
  Orders are entered by hand, never placed by the agent.
- 🔍 Deep dive card per explode pick: bear/base/bull prices, P/C ratios, skew,
  net premium and the busiest contracts.
- ⚡ Unusual options activity: contracts with ≥300 volume today above open interest and ≥$50k premium, any scanned name (config `uoa`).
- 🔥 Highest IV: top 15, plus deep-dive cards for names not already carded above.
- Scorecard: picks from 5 sessions ago vs their implied move, and whether a
  Bull/Bear bias called the direction (starts in week 2).
- IV rank appears after 20 days of saved history.

## Commands

```bash
.venv/bin/python agent.py                         # scan, save history, email
.venv/bin/python agent.py --dry                   # scan, write preview.html, no email or history
.venv/bin/python agent.py --dry --tickers MRNA,VRTX
.venv/bin/python agent.py --test-email
```

A full scan takes about 5 minutes (3 workers, backs off on Yahoo 429s).

## Setup from scratch

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python -r requirements.txt
cp com.dhruv.healthiv.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dhruv.healthiv.plist
```

## Files

| Path | What |
|---|---|
| `agent.py` | the whole agent |
| `spreads.py` | bull call spread tickets + act-on rule |
| `catalysts.py` | FDA / clinical-trial calendar scraper and matcher |
| `catalysts_manual.json` | hand-added events for tickers the calendars miss |
| `config.json` | universe ETFs, extra tickers, filters, score weights, email |
| `data/history.csv` | daily IV30 etc. per ticker (drives IV rank and 1d change) |
| `data/picks.csv` | daily top picks (drives the scorecard) |
| `data/snapshot-YYYY-MM-DD.csv` | full daily scan |
| `agent.out.log` / `agent.err.log` | launchd logs |

`data/` is gitignored. Losing it resets IV rank and the scorecard, nothing else.

## Planned

- **Jev for event-driven IV** (waiting on the API key). Jev judges text, so it
  would read catalyst news for the "next to explode" names and identify the
  event and its date: FDA/PDUFA, trial readout, earnings or M&A. It would go
  through the shared `~/jev-client`. The IV math and the score stay in code.
  It starts shadow-only: answers are logged, and the email is unchanged until
  the logs have been reviewed.
