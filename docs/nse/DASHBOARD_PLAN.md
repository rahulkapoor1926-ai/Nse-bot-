# Paper-trading P&L dashboard: implementation plan

Status: **approved 2026-10-30 and built.** Page: `dashboard/index.html`; data: `nse_bot/dashboard.py`;
published by `.github/workflows/pages.yml`.
Live orders stay impossible: nothing below touches order code, the gate, risk
limits, sizing, the strategy or the 11 skills.

## 1. How it connects to real paper-trading data

The numbers come only from files the paper bot already writes. Nothing is
typed in by hand or made up.

| dashboard item | source today | what is added |
|---|---|---|
| Completed trades (entry, exit, P&L, times) | `paper/ledger.jsonl`, the hash-chained fill ledger | trades paired from fills; ledger integrity is checked and shown |
| Total P&L, win rate, total trades, equity curve | same ledger, net of all charges | computed from it |
| Daily P&L, daily loss limit ₹500, remaining risk, lockout | `risk/<date>.json` | read as is |
| Open positions: entry, qty, stop | ledger plus the exit plan stored with each fill | read as is |
| Open positions: **current price, unrealised P&L** | not saved anywhere yet | **new**: the paper loop writes `dashboard/live.json` after every cycle (latest bid, stop, unrealised P&L) |
| Bot status Running / Stopped / Error | not saved anywhere yet | **new**: the same file carries a heartbeat time and the last error. Running = heartbeat under 3 minutes old; Error = last cycle failed or the day is locked; otherwise Stopped |
| Capital ₹20,000, deployed, available | config + open positions | deployed = cost of open positions; available = 20,000 + realised P&L − deployed |

New code: one read-only module (`nse_bot/dashboard.py`) that builds
`dashboard/data.json` from the files above, plus about ten lines in the paper
loop to write the heartbeat/marks file. Both are covered by new tests. The
existing 156 + 315 tests must still pass.

Freshness: the workflow saves state to GitHub every 5 minutes during market
hours, instead of only at the end of each job. With GitHub's caching, the page
is roughly 5–10 minutes behind the bot. It is not tick-by-tick.

Until a strategy passes the 10,000-example gate (and the SAHI keys work), the
page will truthfully show ₹0 P&L and no trades.

## 2. How you open it on the iPhone

- A single page, served free by GitHub Pages at
  `https://rahulkapoor1926-ai.github.io/Nse-bot-/`.
- Open it in Safari and use Share → Add to Home Screen so it opens like an app.
- It reloads its data every 60 seconds. Every number is labelled SIMULATED.
  There are no trade buttons, and the page has no API keys.
- One tap from you to switch it on: repo Settings → Pages → Source: GitHub
  Actions.

## 3. Could public GitHub Pages expose private data?

Checked against what the bot actually writes.

**Never exposed, with or without the dashboard:**
- SAHI key ID, secret and tokens. They live only in GitHub Secrets and in memory
  during a run. The SAHI token is never written to disk or logs.
- Your real broker account: balance, holdings, positions, orders. The bot never
  reads them. Its SAHI token is limited to market data.
- Your name, PAN, phone or email. None of these are in the bot.

**Already public today, because the repository is public** (the dashboard only
makes it easier to read):
- The code and the strategy rules.
- The `paper-state` branch: simulated fills, simulated P&L, risk state and
  alerts (symbols, prices, times).
- Job logs in the Actions tab, which print the SIMULATED report.

**A problem found while checking, which I would fix in the same change:**
the current workflow would also commit SAHI's instrument list
(`instruments.csv`) and the recorded market data (`recordings/`) to the public
`paper-state` branch. Publishing broker and exchange data may break SAHI's data
terms. The fix is to keep those two in GitHub's private Actions cache, which is
not downloadable by the public, and keep only the simulated ledger, risk and
alert files on the public branch. Nothing has been published yet, because no run
has logged in to SAHI.

**If you want the results themselves private:** a private repository with
Pages needs a paid GitHub plan, and a private repository's free Actions minutes
(2,000 a month) do not cover about 7,900 minutes of market hours a month. So
truly private and free does not work with GitHub alone. A free alternative is a
private dashboard page here in Claude that I refresh when you ask. It would not
update by itself.

## 4. What would change, file by file

| file | change |
|---|---|
| `nse_bot/dashboard.py` | new, read-only builder |
| `nse_bot/cli.py` | paper loop writes the heartbeat/marks file; new `dashboard` command |
| `tests/test_dashboard.py` | new tests: pairing fills, P&L, win rate, status, corrupt ledger shown as an error |
| `dashboard/index.html` | the page (static, no keys) |
| `.github/workflows/paper-day.yml` | save every 5 minutes; keep the instrument list and recordings in the private cache |
| `.github/workflows/pages.yml` | new, publishes the page |

Untouched: `bot.py` decisions, `entry.py`, `exit.py`, `risk.py`, `budget.py`,
`gate.py`, `broker/`, the strategies, `upstream/` (the 11 original skills).
