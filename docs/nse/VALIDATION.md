# Validation: evidence framework, backtest/live parity, data sources

Decided by jojo on 2026-11-02. This replaces the 10,000-example gate. Paper trading only; no risk
limit was changed.

## Evidence levels (never PASS)

Per strategy (orb15-atm, sweep-reversal and orb15-equity are each reported on their own):

| Completed trades | Label | What it allows |
|---|---|---|
| fewer than 30 | INSUFFICIENT EVIDENCE | nothing; no conclusion of any kind |
| 30 to 99 | INITIAL EVIDENCE | reported; never approves the standard book |
| 100 or more | STRONGER EVIDENCE | the historical gate may approve, if every check passes |

* Forward paper trades (`forward.py`, `python -m nse_bot forward-report`): net P&L, costs, win rate,
  profit factor, net expectancy and max drawdown per strategy, and again per trend label and per
  volatility label, each with its own evidence level.
* Historical gate (`gate.py`): genuine option bid/ask history is required (synthetic data is
  REJECTED; a dataset without option bid/ask is INSUFFICIENT EVIDENCE; index candles or option
  OHLC are never used as fill prices). Chronological train / validation / untouched test split;
  the test period is touched only after validation and sensitivity pass, and only when at least
  30 out-of-sample trades can be expected. APPROVED needs 100+ out-of-sample trades, a positive
  95% CI lower bound, beating the coin-flip baseline, and a test period used by at most 3
  strategies. Results are also reported per regime.
* Each gate record stores the code version, configuration hash and cost-schedule hash. A record
  from other code, config or costs is STALE (or NOT EVALUATED if it had insufficient evidence).

## Market regime (`regime.py`)

From daily bars of sessions before the trade day only, identical live and in every backtest:

* trend: efficiency ratio of the last 10 daily closes >= 0.40 is trending (up or down), else sideways;
* volatility: the previous session's range in the top 20% of the 20 sessions before it is
  high_volatility, else normal_volatility;
* fewer sessions than needed is `unknown`.

Recorded on every forward signal, every ledger entry and every backtest trade. It never blocks
or changes a trade.

## Backtest and live use the same rules

| Rule | Live | Backtest |
|---|---|---|
| Sizing | `budget.size_at_fills`: entry at ask + slippage, stop exit at stop - slippage, both legs' charges, loss <= Rs 500 per trade, max 1 lot; otherwise NO TRADE | same function (ORB and equity engines; the Sweep engine already used `budget.size` on its conservative stop) |
| Stop / target | 20% premium stop and 2.5R target planned on the real fill (ask + slippage) | same |
| ORB entry point | only in the window [signal known + entry_delay_bars bars, + one bar) (`patterns.entry_window`); before it WAITING, after it REJECTED | the quote snapshot at the start of that window |
| ORB strike | ATM from the signal bar's close | same |
| Sweep entry point | in the minute after the signal minute | the recorded quote one minute after the signal |
| Order flow | hard filter (`orderflow_confirmation: required`) | NOT SIMULATED: no historical futures depth exists. Every gate record says so in `limitations`; a backtest result does not validate the order-flow filter |

With FYERS charges (Rs 20 per order) the Rs 500 risk rule allows one NIFTY lot (65) only up to an
entry premium of about Rs 33.8 with the 20% stop. Pricier options are refused, live and in the
backtest; the stop and the limit are not changed to force a trade.

## Where genuine historical NIFTY options data can come from (checked 2026-11-02)

Nothing below was bought or subscribed to.

| Source | Expired contracts | Bid/ask | Usable for fills here | Notes |
|---|---|---|---|---|
| Own forward recording (`data/recorder.py`) | yes, kept as recorded | yes, best bid/ask at each bar close | yes | Free. The recorder reads SAHI today; a FYERS recorder is not built yet and would use part of the 5,000 calls/day budget. Data accumulates only from the day recording starts. |
| FYERS history API | no (live contracts only) | no | no | 1-minute is the finest resolution; OHLCV only. |
| [Dhan Expired Options Data API](https://dhanhq.co/docs/v2/expired-options-data/) | yes, up to 5 years, ATM +/-10 strikes near expiry | no (OHLC, volume, OI, IV, spot) | no | Useful for research on premiums, never as bid/ask fills. Whether it needs a paid data plan was not stated. |
| [NSE historical data](https://nsearchives.nseindia.com/content/press/Data_Details_F_n_O.pdf) (NSE Data & Analytics) | yes | trades and a few order-book snapshots a day | partly | Paid; snapshots are "discrete", so a spread at each 5-minute bar is not guaranteed. Price and coverage need a quote from NSE. |
| Paid tick vendors (e.g. TrueData, Global Datafeeds) | yes | tick-level bid/ask on some plans | yes, if bid/ask is included | Paid; not verified here. |

Until one of these supplies bid/ask history, every historical gate result for ORB and Sweep stays
INSUFFICIENT EVIDENCE, and evidence comes only from forward paper trades.
