# Volume profile and auction context (paper only, context only)

Requested by jojo on 2026-11-02. Recorded with every ORB candidate (`auction_context`) and every
sweep decision (`execution.auction_context`). It **never blocks or creates a trade**; the order-flow
confirmation, the 1:2 net rule and all risk limits are unchanged.

## What FYERS provides (measured live, GitHub run 41418924295)

- No volume-at-price or market-profile endpoint (none in the official SDK either).
- `/history` refuses second resolutions: 5S, 10S, 15S, 30S all return HTTP 422 "Invalid input".
- Finest volume data: 1-minute candles of the NIFTY future (median bar range about 11 points).
  About 7 sessions of 1-minute history came back for the last 10 days. The index has no volume.

## So every profile is APPROXIMATE, and says so

Each 1-minute NIFTY futures bar's whole volume is placed at its typical price (H+L+C)/3, in
10-point bins (about one bar's range). Nothing is spread inside a bar and no tick distribution is
invented. Every profile result carries the label `APPROXIMATE: built from 1-minute NIFTY futures
candles ...`. Profile levels are NIFTY future prices.

| Item | How | Exact or approximate |
|---|---|---|
| Session profile, POC | developing profile of today's closed futures bars; POC = busiest bin | approximate |
| VAH / VAL | expand from the POC one bin at a time to 70% of volume | approximate |
| HVN / LVN | peaks / troughs of the 3-bin smoothed profile (>= 1.5x / <= 0.5x mean bin) | approximate |
| Value-area migration | today's value vs yesterday's: higher, lower, or overlapping and leaning | approximate |
| Acceptance / rejection | 15 closes beyond yesterday's VAH/VAL = accepted; traded beyond within 30 min but closed back inside = rejected | approximate levels, exact closes |
| Initial Balance | first 60 minutes' high/low of the NIFTY index, and whether it broke | **exact** |
| Previous session, previous week | same profile on yesterday's and last calendar week's bars | approximate |

## How it is read (notes, then a verdict)

ORB: supportive if price is accepted outside yesterday's value in the trade's direction, the IB broke
in the trade's direction, value migrates with the trade, or a low-volume area lies just ahead;
caution if the breakout is still inside value, the move was rejected, the IB broke the other way,
value migrates against, or a high-volume level sits within 0.15% ahead.
Sweep: supportive if the sweep was rejected back into yesterday's value (responsive reversal);
caution if price is accepted beyond value against the reversal. Verdict: SUPPORTIVE / NEUTRAL /
CAUTION by count. Any data problem gives NOT AVAILABLE and changes nothing.

**Not backtested:** the gate's backtests do not use it (it is not a filter).

## Live evaluation, 2 Nov 14:52 IST (run 41419487421)

Today POC 23330, value 23320-23410; yesterday POC 23100, value 23030-23160; last week POC 22760,
value 22580-22890. Value migrated higher; price accepted above yesterday's value; IB 23211.8-23330.7
broken upward. A long ORB would have been SUPPORTIVE (with a caution: high-volume level at 23390
just ahead); a short ORB CAUTION.
