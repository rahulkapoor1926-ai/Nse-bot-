# Order-flow confirmation (paper only)

Requested by jojo on 2026-11-02. Order flow **confirms or vetoes** an ORB or sweep_reversal entry
that already passed every other check. It never creates an entry.

## What FYERS provides (measured live, GitHub runs 41417570845 and 41418337165)

REST `/data/depth` returns 5 bid and 5 ask levels (price, quantity, number of orders), whole-book
`totalbuyqty` / `totalsellqty`, last trade price, size (`ltq`) and time, and cumulative day volume.
It does **not** say whether a trade hit the bid or lifted the offer, and between snapshots 3 s apart
up to ~200,000 units traded, so trades cannot be told apart. The NIFTY index has no depth or volume.
FYERS's WebSocket and tick-by-tick feeds were not tested.

| Metric | Status | Why |
|---|---|---|
| Candle buy / sell volume | NOT AVAILABLE | needs the trade side of every trade |
| True bid-ask delta | NOT AVAILABLE | same |
| Cumulative volume delta (CVD) | NOT AVAILABLE | needs true delta |
| Price-delta divergence | NOT AVAILABLE | needs true delta |
| Buying / selling absorption | NOT AVAILABLE | needs aggressive volume at a price that holds |
| Bid-ask volume imbalance | AVAILABLE | NIFTY future depth: 5-level and whole-book |
| Volume exhaustion | AVAILABLE | NIFTY future 1-minute candles (real traded volume) |

No delta is ever computed from candle volume or price direction (a test checks the code).

## The confirmation (`nse_bot/orderflow.py`)

On the current-month NIFTY future, at entry time:

- 5-level book imbalance (bid qty - ask qty) / (bid qty + ask qty) must favour the trade by at least
  0.10 (CE: bid-heavy, PE: ask-heavy).
- Whole-book imbalance more than 0.25 against the trade vetoes it.
- ORB: a breakout that makes a new high (low) on less than 60% of the previous 20 minutes' average
  futures volume is exhausted and is vetoed.
- Sweep: exhaustion of the swept move is reported as supporting the reversal; not required.
- Depth older than `max_quote_age_seconds` (10 s), missing depth or missing volume = NOT AVAILABLE,
  which blocks the entry. Only closed 1-minute bars are used.

Config `orderflow_confirmation`: `"required"` (example config) or `"off"`. The result is stored on
the ORB candidate and in the sweep decision report (`execution.orderflow`).

**Not backtested:** recorded datasets have no order-book history, so the 10,000-example gate
validates the strategies without this filter. The filter can only remove trades, never add them.

Live evaluation, 2 Nov 14:40 IST: 5-level imbalance +0.27 (bid-heavy), whole book +0.004, no
exhaustion; a long (CE) entry would have been CONFIRMED, a short (PE) entry NOT CONFIRMED.
