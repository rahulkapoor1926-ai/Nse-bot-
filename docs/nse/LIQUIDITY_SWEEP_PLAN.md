# Liquidity-sweep strategy with trade decision reports: proposed logic and tests

Status: **approved by jojo with 8 corrections and implemented (paper only, live orders disabled).**
See section 8 for what was built, the test results and what is still incomplete.

This adds a **new, separate strategy** (`sweep_reversal`) next to the existing
opening-range strategy. The existing strategy, the 11 original skills, every
risk control (₹500 daily loss, lockout, sizing, quote checks), the
10,000-example validation gate and the live-order block stay exactly as they
are. The new strategy goes through the same gate → budget → risk → paper broker
chain, so until it passes the gate it can only **alert and log**, never enter.

Every rule below is mechanical and uses **closed candles only**. A rule either
passes with the candle(s) that prove it, or fails with the reason. Nothing is
inferred, estimated or filled in.

---

## 0. What the data can and cannot support (checked against SAHI's docs)

| need | SAHI provides | consequence |
|---|---|---|
| 1m, 5m, 15m, 1h, daily candles | yes: `1m, 3m, 5m, 15m, 30m, 1h, 1d` | all five timeframes available |
| Delta, gamma, theta, vega, IV, OI, volume, bid/ask per option | yes, in the option chain (`greeks`, `implied_volatility`, `open_interest`, `volume_today`, `top_bid/ask`) | used as reported by SAHI; never computed or guessed. A missing value = contract skipped |
| NIFTY **index** volume | **no** (an index has no traded volume) | volume, relative volume and VWAP come from the **current-month NIFTY future**. Assumption to verify on the first live run: the future is in SAHI's scrip master with 1m candles and volume. If not, the volume and VWAP rules fail and every setup is skipped (fail closed) |
| VWAP | no VWAP field | computed from the future's 1m candles: Σ(typical price × volume) / Σ volume since 09:15 |
| Historical option bid/ask for backtests | no | the gate stays **INSUFFICIENT DATA** for this options strategy until real option history exists (same as today) |

**Plain warning about money:** with ₹20,000 and a ₹500 risk limit, one NIFTY
lot (65) allows about 7 premium points of loss. An ATM option (delta about 0.5)
moves about 7 points for 14 NIFTY points. Stops placed beyond a sweep low are
often 20–40 NIFTY points away, so **many valid setups will be skipped as "too
risky for the budget"**. That is the risk rules working, and the reports will
say so. Options are taken only when one lot fits; otherwise nothing is traded.

---

## 1. Definitions (all on closed candles)

Settings in brackets are proposed defaults, kept in the strategy file so the
sensitivity test (±20%) can vary them.

**Swing high / low.** Bar *i* is a swing high if its high is greater than the
highs of the [2] bars before and the [2] bars after it. It becomes known only
when bar *i+2* closes (no look-ahead). Swing lows are the mirror.

**Equal highs / lows.** Two or more swing highs (or lows) within [0.03%] of each
other (about 7 points on NIFTY). Their highest high (lowest low) is the level.

**Liquidity levels.**
- Buy-side (above price): previous-day high (PDH), 15m swing highs of today and
  the previous session, equal highs.
- Sell-side (below price): previous-day low (PDL), 15m swing lows, equal lows.

**Sweep (stop hunt / false breakout), on 5m.** For a sell-side level L
(bullish case): a 5m bar trades below L by at least [0.02%] **and** a 5m close
back above L happens within [2] bars (the reclaim). If price instead closes
below L for more than 2 bars, the break was accepted: **no sweep, no trade**.
Recorded: level type and price, sweep bar time, sweep extreme (lowest low),
reclaim bar time and close. The bearish case is the mirror on buy-side levels.

**Rejection and confirmation candle, on 5m.** After the reclaim, within [6]
bars, a 5m candle closes above the high of the sweep bar with a body of at
least [50%] of its range. That candle is the confirmation candle.

**Market structure, on 5m.** Trend from the last two swing highs and lows:
higher highs and higher lows = up; lower highs and lower lows = down.
- **CHoCH (bullish):** structure was down, then a 5m close above the most recent
  swing high.
- **BOS (bullish):** structure was up, then a 5m close above the most recent
  swing high.
One of them must occur after the sweep, at or before the confirmation candle.

**Fair value gap (FVG).** Three 5m candles where the third candle's low is above
the first candle's high (bullish), with the gap at least [0.02%]. It must be
inside the move from the sweep to the BOS/CHoCH.

**Order block (OB).** The last down-closing 5m candle before the move that made
the BOS/CHoCH (bullish). Zone = that candle's low to high.

At least one FVG **or** OB must exist; it is the entry zone.

**Support / resistance.** The nearest opposing liquidity level (for a bullish
trade: the next buy-side level above) is the first target and the resistance.

**VWAP.** For a bullish trade, the last 5m close is above session VWAP
(future-based); for a bearish trade, below.

**Volume expansion.** The displacement candle (the one making the BOS/CHoCH)
has future volume of at least [1.5x] the average of the previous [20] 5m bars.

**Relative volume.** Today's cumulative future volume up to this time is at
least [1.0x] the average over the same time window of the previous [10]
sessions. If 10 sessions of history are not available, the rule fails.

**Momentum.** 5m RSI(14) above 50 for bullish, below 50 for bearish.

## 2. Multi-timeframe agreement

| timeframe | rule | bullish needs |
|---|---|---|
| Daily | last close vs 20-day EMA | close above EMA (bearish: below) |
| 1H | 20-period EMA slope over the last 3 closed hours | rising (bearish: falling) |
| 15M | structure and the swept level | level comes from the 15m/daily liquidity list; 15m structure not making new lower lows after the sweep |
| 5M | sweep, reclaim, confirmation, BOS/CHoCH, FVG/OB, VWAP, volume, RSI | all pass |
| 1M | entry timing | price retraces into the FVG/OB zone and a 1m candle closes back in the trade direction, within [30] minutes of the confirmation candle |

Daily and 1H must both agree. A neutral or opposite reading is a skip, with the
reason recorded. (Stricter means fewer trades; this is adjustable.)

## 3. Options contract selection (after the NIFTY setup passes)

1. Bullish → CE, bearish → PE. Long options only; nothing is sold.
2. Nearest weekly expiry that the existing rules allow (no new entries on expiry
   day after 14:45; existing no-entry windows unchanged).
3. Candidate strikes: **ATM** (strike nearest spot) and **up to [2] ITM**
   strikes (CE: below spot; PE: above spot). **Any OTM strike is refused**, and a
   test checks this for every strike in a chain.
4. Each candidate must pass: SAHI-reported delta between [0.45] and [0.80]
   (absolute); IV present; spread at most the existing `max_spread_pct`; top
   bid/ask size at least the existing minimum; volume and OI at least the
   existing minimums; theta/premium at most [8%] per day. Vega is recorded.
5. Of the survivors, pick the first that fits one lot within ₹500 risk and the
   outlay cap, ranked by narrowest spread %, then highest OI, then delta closest
   to [0.55]. The report lists every candidate and why it won or lost.

**Stop and target.** Underlying stop = beyond the sweep extreme by [0.02%].
Premium stop = entry premium − delta × (spot − underlying stop), rounded to the
tick. Target = nearest opposing liquidity level, translated the same way, and
must give at least the existing `min_reward_risk` (1.5). The existing exit logic
then manages the trade on the option's bid, and the underlying stop is also an
invalidation.

## 4. Trade Decision Report (saved for every evaluation, taken or skipped)

One JSON line per evaluation in `state/decisions/<date>.jsonl`, labelled
SIMULATED. The full reports contain SAHI market data (option quotes, Greeks,
candles), so they are kept in the **private** Actions cache, like the
recordings, not on the public branch. A one-line summary per decision (TAKE or
SKIP, reason, contract, entry/stop/target of a taken trade) goes to the public
branch for the dashboard. I can read the full reports and explain any trade
here when you ask. Fields:

- `decision`: TAKE or SKIP; `skip_reason` (the first rule that failed)
- `bias`: bullish / bearish; `why`: one plain sentence built from the rules
- `liquidity`: level type (PDH, PDL, swing, equal highs/lows), level price,
  sweep bar time, sweep extreme, reclaim bar time and close
- `confirmation_candle`: time, OHLC
- `structure`: BOS or CHoCH, the swing broken (time, price), the breaking bar
- `zone`: FVG and/or OB with the three (or one) candles' times and prices
- `vwap`, `volume_expansion`, `relative_volume`, `rsi`: value, threshold, pass/fail
- `timeframes`: daily, 1h, 15m, 5m, 1m result each, with the bars used
- `contract`: symbol, strike, expiry, CE/PE, ATM/ITM, delta, gamma, theta,
  vega, IV, OI, volume, bid, ask, spread %; plus every rejected candidate and why
- `spot`, `entry_premium`, `premium_stop`, `premium_target`, underlying stop and
  target, reward:risk, lots, ₹ risk including charges
- `rules`: every rule with pass / fail / not reached
- `raw_refs`: instrument ids, interval, the exact candle timestamps used, and
  the request window of each data fetch
- `times`: signal known at, evaluated at, data received at (IST)

**Anti-fabrication rule:** before a report is saved, code checks that every
candle it cites exists in the fetched data with the same timestamp and prices.
If any check fails, the report is saved as SKIP ("evidence mismatch") and no
trade is allowed.

## 5. No-trade rule

If the sweep, the reclaim, the confirmation candle, BOS/CHoCH, an FVG/OB, VWAP,
volume, RSI, timeframe agreement, an eligible contract, the gate or the budget
fails, nothing is entered and the report says which rule failed first. Missing
data is always a skip, never a pass.

## 6. Tests (written before the code)

All with hand-built candles where the right answer is known in advance.

1. Swing high/low found only after 2 later bars close (no look-ahead).
2. Equal highs within 0.03% grouped; 0.05% apart not grouped.
3. Sweep below PDL with reclaim in 1 bar → sweep; with closes below for 3 bars
   → "accepted break", no trade.
4. Wick that does not reach 0.02% beyond the level → no sweep.
5. Confirmation candle: body 60% closing above sweep bar high → pass; body 30%
   → fail; after 7 bars → fail.
6. CHoCH in a downtrend vs BOS in an uptrend, both directions.
7. FVG found with the right bounds; a gap of 0.01% → not counted.
8. Order block = last opposite candle before displacement.
9. VWAP equals a hand calculation; index-only data (no future) → rule fails.
10. Volume expansion and relative volume with fewer than 10 sessions → fail.
11. Timeframe agreement table: every combination of daily/1h up/down/neutral.
12. 1m entry within 30 minutes → entry; after 30 minutes → skip.
13. Contract selection: CE for bullish, PE for bearish; **OTM never selected for
    any chain**; delta, IV or Greeks missing → contract skipped, not estimated.
14. Best contract chosen by the stated ranking; ties broken the same way every time.
15. Premium stop/target from delta; reward:risk below 1.5 → skip.
16. Budget: a sweep with a 40-point stop → skip "one lot exceeds ₹500".
17. Every rule failing alone produces SKIP with that rule as the reason.
18. Anti-fabrication: a report citing a candle that is not in the data is
    turned into SKIP.
19. Full chain: a passing synthetic setup produces a TAKE report, but the gate
    (synthetic data) still blocks entry, as today.
20. All existing tests (160 + 315 original-skill tests) still pass;
    `LiveOrders` still raises; no order endpoint anywhere.

## 7. What changes, file by file (after approval)

| file | change |
|---|---|
| `nse_bot/structure.py` (new) | swings, equal levels, sweeps, BOS/CHoCH, FVG, OB, VWAP, RSI, EMA |
| `nse_bot/sweep.py` (new) | the strategy: multi-timeframe evaluation → report |
| `nse_bot/contract_select.py` (new) | ATM/ITM selection from the option chain with Greeks |
| `nse_bot/decisions.py` (new) | report schema, evidence check, `decisions/<date>.jsonl` |
| `nse_bot/bot.py` | call the new strategy when its strategy file is given; existing ORB path unchanged |
| `nse_bot/patterns.py` | register `sweep_reversal` so the gate and config accept it |
| `config/strategies/sweep.example.json` (new) | the defaults above |
| `tests/test_structure.py`, `test_sweep.py`, `test_contract_select.py`, `test_decisions.py` (new) | the tests above |
| backtest engine | multi-timeframe replay from 1m data for research; the gate result stays INSUFFICIENT DATA without real option bid/ask |

Not changed: the ORB strategy, `entry.py`/`exit.py`/`risk.py`/`budget.py`
behaviour, `gate.py` thresholds, `broker/`, `upstream/` (the 11 skills).


## 8. Implementation status (after approval)

Built:

| Correction | Where |
|---|---|
| 1. Stop/target use Delta, Gamma, Vega and Theta; always labelled estimates | `contract_select.project`, `plan_for` (sizing uses a worse stop: IV shock, theta to flat_by, gap allowance, slippage) |
| 2. Current-month NIFTY future checked before VWAP/volume/RVOL; fail closed | `sweep.futures_quality`, rule `data_future`; `instruments.Master.current_future` |
| 3. Exits at recorded bids minus slippage, never at the stop price | `backtest/sweep_engine._simulate` (`stop_gap` when the bid gapped through) |
| 4. ORB and sweep_reversal in separate books | `paper-day.yml` runs a second paper process with `state_dir=state/sweep`; dashboard `?book=sweep`; `report` shows `by_strategy` |
| 5. No look-ahead, no survivorship bias | closed bars only, zone `known_at`, 1h/daily rebuilt from earlier 1m bars; contracts from recorded snapshots |
| 6. Gate unchanged at 10,000; synthetic never counts | `gate.py` dispatch only; tests assert REJECTED for synthetic, INSUFFICIENT DATA for small real data |
| 7. Gaps, missing quotes, wide spreads, IV changes, slippage | `tests/test_sweep_replay.py`, `tests/test_sweep.py` |
| 8. Original skills and risk controls preserved | upstream 315 tests unchanged; same gate -> budget -> risk -> paper broker chain |

Data recorder: `record --sweep --out state/sweep-recordings` stores the 1m index bar, the 1m
current-month future bar and the option-chain snapshot every minute; `build-dataset --kind sweep
--recordings state/sweep-recordings --underlying NIFTY` turns them into a hashed, non-synthetic dataset.

Privacy: full Trade Decision Reports (candles, quotes, Greeks) are written to `decisions/` and stay
in the private Actions cache. Only one-line summaries (`dashboard/decisions-<day>.jsonl`) are public.

Incomplete or unverified:

- SAHI field units for theta/vega (assumed paise) and the futures `instrument_type` (assumed
  `FUTIDX`) are unverified until the first run with working SAHI secrets.
- The gate will report INSUFFICIENT DATA until enough real sweep recordings exist, so every
  sweep setup in paper trading is recorded as BLOCKED. This is intended.
- With a Rs 500 per-trade risk limit, many NIFTY option setups size to zero lots after the
  conservative stop; those are recorded as BLOCKED with the reason.
- Each book has its own virtual Rs 20,000 and Rs 500 daily loss limit. Combined, the two paper
  books could lose up to Rs 1,000 in a day; this does not apply to live trading, which stays off.
- The strict 1h trend rule rejects many sweeps after gap-down opens. Not changed without approval.
- The replay engine is O(n^2) per day; fine for months of data, slow for years.
