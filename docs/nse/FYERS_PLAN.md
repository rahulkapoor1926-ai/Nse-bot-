# Switching market data from SAHI to FYERS: implementation plan

Status: **proposal, waiting for jojo's approval. No bot code has been changed.**

Requested on 2026-11-02. FYERS read-only access was verified the same day (GitHub
Actions run 41403687715): login, NIFTY spot, option chain with bid/ask/OI/volume/Greeks/IV,
futures chain, 1-minute candles for the index and the future, and option market depth all
returned live data.

## 1. What changes

| Piece | Change |
|---|---|
| `nse_bot/broker/fyers.py` (new) | Read-only FYERS market-data adapter implementing the same `MarketData` interface as SAHI (`quotes`, `candles`, `expiries`, `option_chain`). Only GET data calls plus the one token exchange. No order, position, funds or profile endpoint exists in the file; a test fails if one is added. Hosts come from FYERS's official SDK (`api-t1.fyers.in`). |
| `nse_bot/instruments.py` | Load FYERS's public symbol master (NSE F&O and index symbols) into the same `Master` structure, so strategies, contract selection and the gate see identical objects whatever the source. |
| `config` | New key `"data_source": "fyers"` (or `"sahi"`). One source per session; required, no default. |
| `nse_bot/cli.py` | `paper`, `record` and `fyers-check` pick the adapter from `data_source`. |
| `.github/workflows/paper-day.yml` | Uses FYERS secrets when `data_source` is fyers. Stays manual-only until you approve automatic runs. |
| Strategy validation | Rejects any strategy that could pick OTM: ORB `strike_offset` must be 0 (ATM) or negative (ITM); sweep_reversal already never selects OTM. |
| Options universe | NIFTY only for options (BANKNIFTY removed from `underlyings`). Stocks/ETF strategy unchanged. |

Unchanged: the 11 original skills, ORB and sweep_reversal logic, the 10,000-example gate,
budget, risk book and lockout, the paper broker, the dashboard and the end-of-day report.

## 2. Your limits, and where each is enforced

| Limit | Where | Status |
|---|---|---|
| Rs 20,000 virtual capital | `capital_inr` in config | unchanged |
| Max 1 NIFTY options lot | `max_lots_per_trade: 1`, NIFTY only | already 1; NIFTY-only is new |
| Daily max loss Rs 500 | `max_daily_loss_inr: 500`, persistent lockout | unchanged |
| No averaging down | `max_open_positions: 1` and one entry per signal | unchanged; a test proves a second buy on an open contract is refused |
| No forced trades | every entry needs a strategy signal plus gate, budget and risk PASS | unchanged |
| ATM/ITM only | contract selection + new validation rule | tightened |
| No live orders | no order code in the FYERS adapter; config refuses live mode | unchanged |

## 3. Freshness: fixing the spot timestamp

The live check showed the quote's `tt` field is the start of the day (05:30 IST), not the
last update. So `tt` will never be used as a freshness signal, and candle freshness will not
be taken as quote freshness. Instead, every quote used for a decision must pass all of:

1. **Last-trade time** from FYERS market depth (`ltt`) for that exact symbol is within
   `max_quote_age_seconds` (10 s) of now. For the NIFTY index, freshness comes from the
   index quote's own last-trade time if FYERS returns one; if it does not, spot is treated as
   unverified and no entry is allowed.
2. **Change check**: the quote is fetched twice at least 2 seconds apart and the
   receive times are recorded; a quote whose timestamp does not advance while the market
   is open counts as stale.
3. **Cross-checks**: the spot from the quote, the option chain's underlying price and the
   latest closed 1-minute candle must agree within 0.2%; the futures price must be within
   1% of spot; bid > 0, ask > bid, spread within `max_spread_pct`.
4. If any check fails or a field is missing, the decision is BLOCKED with the reason and
   no entry is made. Exits on open positions use the recorded bid only; a missing bid
   delays the exit and is flagged, never treated as filled.

The first live session will record which timestamp fields FYERS actually fills, and the
report will say so. If a field the plan relies on turns out to be empty, I will tell you
before changing the approach.

## 4. One source at a time (SAHI kept as fallback)

- The SAHI adapter stays in the code and its tests stay green.
- A session uses exactly one source, named in config and written into every quote, fill,
  ledger row, decision report, recording and dashboard file (`"source": "fyers"`).
- No automatic fallback mid-session: if FYERS fails, the bot stops opening trades
  (fail closed) and says so on the dashboard. Switching to SAHI is a config change and a
  new session.
- Recordings and gate datasets are tagged by source; a dataset mixing sources is refused.

## 5. Daily FYERS login

FYERS tokens expire at 06:30 IST every day, so each trading day you log in on FYERS's page
(as you did today), put the new code in the `FYERS_AUTH_CODE` secret, and start the run.
The workflow exchanges the code for a token at start-up and keeps it only in memory, masked.
If the code is missing or already used, the run stops before trading and says why.
(A refresh-token option exists that avoids the daily login, but it needs your FYERS PIN as a
secret; not part of this plan.)

Rate limits on the free plan are 5 calls/second, 50/minute, 5,000/day. The bot will use a
built-in limiter (about 12 calls a minute with a 30-second loop, roughly 4,500 a day across
the session) and treat a rate-limit response as missing data.

## 6. Tests before anything runs

- Unit tests with FYERS-shaped responses: adapter parsing, unit handling, symbol mapping,
  stale/missing/inconsistent data blocking entries, cross-source refusal, rate-limit
  handling, no order endpoints, credentials masked.
- Existing suites unchanged: 243 NSE tests and 315 original-skill tests must pass.
- **Live paper test** (needs a fresh FYERS login that day): a short manual paper session
  on GitHub Actions during market hours with real FYERS data, then a report showing
  every evaluation, every block reason, data freshness measurements and the dashboard.

Expected result of the live test: data flows end to end, but **no paper trade will be
entered**, because the 10,000-example gate still has no real recorded dataset and returns
INSUFFICIENT DATA. That is the existing safety gate working, and this plan does not weaken it.
FYERS's 1 year of 1-minute history may let us build a real dataset for the gate later; that
would be a separate proposal.

## 7. What needs your approval

1. This plan.
2. After the tests and the live paper test pass: whether to turn on automatic daily runs.
