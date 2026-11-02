# Options-desk context for NIFTY options (paper only, context only)

Requested by jojo on 2026-11-02. Recorded with every ORB candidate (`options_desk`) and every sweep
decision (`execution.options_desk`). It **never blocks or creates a trade**; order flow, the volume
profile context, the 1:2 net rule and all risk limits are unchanged.

## Data audit (FYERS /options-chain-v3, live, GitHub run 41420747976)

All 62 legs (31 strikes around ATM, nearest expiry) carried `oi`, `prev_oi`, `oich`, `volume`,
`bid`, `ask`, `ltp` and Greeks `delta`, `gamma`, `theta`, `vega`, `iv`. The response also gives chain
totals `callOi` / `putOi` and India VIX. India VIX daily history: 247 sessions for a year.
There is **no historical IV per option**. OI is in units (ATM CE OI 17,600,310 = 270,774 lots of 65).
FYERS rounds gamma to 3 decimals.

| # | Item | Source | Status |
|---|---|---|---|
| 1 | OI and change in OI | `oi`, `prev_oi` per leg | real |
| 2 | Put/call ratio (OI, change, volume) | sums over the returned strikes; FYERS chain totals shown alongside | real |
| 3 | Strike OI concentration | top 3 strikes per side with share and change | real |
| 4 | ATM IV and skew | chain IVs; skew = OTM put IV - OTM call IV, 2 and 4 strikes out | real |
| 5 | IV percentile | **India VIX** percentile vs its last year (needs >= 200 sessions). Not the option's own IV history, which FYERS does not provide | real, labelled |
| 6 | Delta/gamma/theta/vega exposure | OI x Greek per side, from the holders' side (every long has a short) | real inputs |
| 7 | GEX | sum(call gamma x OI) - sum(put gamma x OI), x spot^2 x 1% = Rs per 1% move | **ESTIMATE** |
| 8 | Expiry and time decay | hours to FYERS's expiry time, expiry-day flag, ATM theta as % of premium | real |
| 9 | Dealer positioning | long/short gamma from the GEX sign only | **ESTIMATE** |

**GEX assumption (stated on every result):** dealers are long the calls and short the puts that
customers trade. Indian option flow is retail-heavy, so this may be wrong. Window = the strikes FYERS
returned. Neither GEX nor dealer positioning says anything about direction.

**No direction from OI.** OI figures are reported as numbers; the largest OI strike near spot appears
only as "a level to watch".

## How it is read

ORB: estimated dealer short gamma (hedging may extend moves) = supportive; long gamma = caution.
Sweep: the reverse. Expiry day, ATM theta >= 15% of premium per day, or India VIX in the top 20% of
its year = caution; VIX in the bottom 20% = supportive. Verdict by count. Any data problem gives NOT
AVAILABLE and changes nothing. Not part of the backtests (it is not a filter).

## Live evaluation, 2 Nov 15:01 IST (run 41421349080)

Spot 23310, nearest expiry 3 Nov 15:40. PCR (OI) 0.95 on the returned strikes; PCR of today's OI
change 1.61. Largest CE OI 23300, largest PE OI 23200. ATM IV 16.0; put skew +0.65 (2 strikes) and
+1.54 (4 strikes). India VIX 15.57 = 71st percentile of its year. GEX estimate +Rs 1.19 lakh crore per
1% move: estimated dealer long gamma. ATM theta 48.7% of premium per day (day before expiry).
Context: ORB CAUTION either way (long gamma, heavy decay); sweep NEUTRAL.
