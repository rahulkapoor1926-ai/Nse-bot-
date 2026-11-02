# Minimum 1:2 net reward:risk (ORB and sweep_reversal)

Requested by jojo on 2026-11-02. Paper trading only.

**Rule:** a trade is taken only if, for every Rs 1 it can lose, it can make at least Rs 2 net.
Both sides are measured in rupees after all costs (`nse_bot/rr.py`):

- net risk = (entry fill - stop exit fill) x qty + entry charges + exit charges at the stop
- net reward = (target exit fill - entry fill) x qty - entry charges - exit charges at the target
- entry fill = ask + slippage (the spread is paid); exits are on the bid - slippage
- charges = brokerage, STT, exchange, SEBI, stamp, GST, from the same cost model as the ledger

Below 2.0 means **NO TRADE**, with the reason in the decision report. `min_reward_risk` is 2.0 in
config, and config validation refuses anything lower.

| Where | What |
|---|---|
| `risk.check` (both strategies, live paper) | blocks unless the plan's net ratio >= 2.0; a missing figure blocks |
| `bot.py` ORB entry | measures the plan: stop = the ORB premium stop, target = the ORB target |
| `sweep_live.py` | measures the plan on the **conservative** stop (IV drop, theta, gap allowance, slippage) |
| `contract_select.select` (sweep) | pre-filter on the gross ratio, now also 2.0 |
| backtests (`engine`, `equity_engine`, `sweep_engine`) | same rule; a NO TRADE signal is counted as `net_rr_no_trade` and is **not** a gate example, so the 10,000-example gate is not inflated |

**Stops are never widened.** The stop is fixed when the plan is made and is stored with the
position; nothing changes it afterwards (a test scans the code for any assignment to a stop).
The 1:2 rule only measures the plan; it never moves a stop or target to make a trade fit.

**Stops on market structure:**
- sweep_reversal: the underlying stop is just beyond the sweep extreme; the premium stop is that
  level projected through the option's Greeks.
- ORB: the structure stop is the opening-range midpoint on NIFTY (a close beyond it exits the trade,
  as before). The 20% premium stop stays as the hard cap, and the 1:2 check uses that cap as the
  risk, because it is the worst planned loss and needs no Greek estimate (historical data for the
  backtest has no Greeks).

**Consequences to know:**
- ORB `target_r` raised from 2 to 2.5 (jojo approved, 2026-11-02): 2.0 before costs could never be 1:2 net.
- A 20% premium stop on one NIFTY lot (65) above a premium of about Rs 38 risks more than the
  Rs 500 daily limit, so sizing already blocks most ORB trades. Unchanged.

Unchanged: Rs 20,000 capital, Rs 2,000 daily profit stop, Rs 500 daily loss limit, one lot, ATM/ITM only,
the 11 original skills and the validation gate.
