# Daily NIFTY end-of-day report (no login, no trades)

Chosen by jojo on 2026-11-02: no broker credentials will be provided, so the bot
uses only NSE's free public end-of-day files. The scheduled SAHI paper runs are
paused (`paper-day.yml` keeps a manual trigger only).

## Data, verified

A read-only probe (`scripts/probe_eod.py`, workflow `eod-probe`) downloaded these from
GitHub's servers on 2026-11-02 (run 41391300516) with no login:

| file | URL | NIFTY content |
|---|---|---|
| F&O bhavcopy | `nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip` | about 1,700 rows a day: options (IDO) and futures (IDF) with daily OHLC, settlement, OI, OI change, volume, trades, lot size |
| index close | `nsearchives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv` | NIFTY daily OHLC |

The old `historical/DERIVATIVES/.../fo<DDMONYYYY>bhav.csv.zip` address returns 404.

Not available for free without a login: bid/ask, IV, Greeks, any intraday candle,
intraday futures volume and VWAP. These are listed as missing in every report and
never estimated.

## What the report contains

`python3 -m nse_bot --config config/paper.json eod-report --date YYYY-MM-DD`

- NIFTY daily OHLC and change; daily trend (close vs EMA20 and its 5-session slope)
- daily sweep: high above the previous day's high with the close back below it
  (bearish), or low below the previous day's low with the close back above it (bullish),
  with both candles as evidence
- nearest expiry after the day; ATM strike plus up to 2 ITM strikes for CE and PE
  (OTM never listed) with close, settlement, OI, OI change, volume and premium per lot
- put-call ratio by OI and the strikes with the most CE and PE OI
- current-month future: close, OI, volume, basis to the index
- `trades_simulated: 0`: without bid/ask a fill would be an estimate, so none is made

Fails closed: an unpublished file, wrong trade date, missing columns, a bad zip or an
inconsistent index bar stops the report. Missing history days are listed, not filled.

## Where to see it

The `eod-report` workflow runs at 19:30 IST on weekdays (retrying for an hour if NSE
publishes late), saves `eod/<date>.json`, `eod/<date>.md` and `eod/latest.json` to the
`paper-state` branch, and the dashboard shows it under the "Daily report" tab
(`?book=eod`). Raw NSE files stay in the private Actions cache.

## Limits

- Daily bars cannot confirm the intraday rules of sweep_reversal (BOS/CHoCH, FVG, VWAP,
  1m entry); the report says so.
- The 10,000-example gate is unchanged. These files cannot feed it, because the gate
  needs bid/ask for fills; the gate stays at INSUFFICIENT DATA.
- NSE's terms restrict automated collection except for content offered for download;
  these are download files fetched twice a day, and only a few derived rows are published.
