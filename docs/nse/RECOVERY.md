# Recovery guide

The bot fails closed: when it cannot trust its state or data it stops entering
trades and alerts. These are the situations and what to do.

## Corrupted risk state (`<risk_dir>/risk/<date>.json`)

Since 2026-11-02 ORB and Sweep share ONE risk book: both configs name the same
`risk_dir` (`state` for the standard books, `state/forward` for the forward
books), while each keeps its own ledger in its `state_dir`. The file holds the
combined entries, stop-loss exits, realised and open P&L, open lots and the
lockout. Every update takes the lock file `<risk_dir>/risk/.lock` and re-reads
the file first; if the lock cannot be taken within 20 seconds the entry is
BLOCKED ("risk state unavailable") and the next cycle tries again.

Symptom: `risk-status` shows `locked: true` with "state file corrupt", every
order is BLOCKED for both strategies.

1. Leave the file in place; the bot does not overwrite it.
2. Inspect it. If you can reconstruct the day from both ledgers
   (`state/paper/ledger.jsonl` and `state/sweep/paper/ledger.jsonl`: fills and
   times), stop both processes and write a corrected file with the same fields.
3. If not, keep the day locked. A new file is started automatically on the next
   trading day.

## Corrupted or tampered paper ledger (`state/paper/ledger.jsonl`)

Symptom: `report` shows `integrity: CORRUPT ... hash chain mismatch`; `paper`
refuses to start.

1. Stop the bot. Copy the ledger aside.
2. The record number in the message is the first bad line. Lines before it are
   intact. Truncate to the last good line only if you are sure no later fill
   happened; otherwise start a new ledger folder and record the open position
   by hand in your notes.
3. Never edit a line's price or quantity to "fix" it; the chain will reject it.

## Expired or rejected SAHI token

- Tokens expire at 06:00 IST every day. The client re-mints on HTTP 401.
- A 403 when minting means the key is not approved for today: approve it in the
  SAHI dashboard, then restart.
- If a secret may have leaked, revoke the key in the dashboard (this kills every
  token minted from it) and create a new one with only `marketdata.read`.

## Missing or stale data

- A stale, crossed, missing or illiquid quote blocks entries and raises a
  `stale_data` alert. Exits are not assumed; the position stays open until a
  fresh bid arrives.
- Repeated failures (`max_consecutive_errors`) lock the day.
- If the scrip master is missing or malformed, `--refresh-master` downloads a
  new one; the bot refuses to run on a master with mixed lot sizes.

## API failures and rate limits

- HTTP 429 with a short `Retry-After` is retried once; longer waits are reported
  as errors instead of blocking the loop.
- Network errors are reported per call and count toward the error lockout.
- An open position you cannot price is reported every cycle as `stale_data`.
  In paper mode nothing else is at risk; in any future live mode you would close
  it manually in the SAHI app.

## Missing holiday list for a new year

`CalendarError: no NSE holiday list for 2027`: add the year to
`nse_bot/data/nse_holidays.json` from the official NSE F&O holiday circular
(published in December), with its source link. Until then the bot refuses to run.

## Cost schedule out of date

If SAHI or the exchange changes charges, add a new schedule with its
`effective_from` date and sources to `nse_bot/data/costs_india.json`. Old
schedules stay so backtests are charged the rates of their own dates.
