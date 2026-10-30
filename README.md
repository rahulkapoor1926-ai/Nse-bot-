# NSE trading bot (paper trading, read-only data)

NSE stocks and ETFs, plus NIFTY and BANKNIFTY index options when an option
trade is suitable, adapted from the 11-skill
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) pack.
The original US pack is kept verbatim under `upstream/` and is not modified.

**This phase cannot place a real order.** Market data is read-only from SAHI;
every fill is simulated and labelled SIMULATED. Nothing here is financial
advice, and no backtest or test result is evidence of future profit.

## Quick start

Python 3.9+; standard library only.

```bash
cp config/paper.example.json config/paper.json           # edit limits if you need to
python3 -m nse_bot --config config/paper.json validate-config
scripts/test_all.sh                                       # NSE suite + untouched upstream suite
```

Run every command from the repository root (config paths are relative to it).

| command | what it does |
|---|---|
| `validate-config` | checks the config; any missing or unknown key is refused |
| `clock` | NSE status in IST (holidays from the NSE circular) |
| `instruments --master FILE` | parses a SAHI scrip master; lists expiries and lot sizes |
| `costs --buy P --sell P --qty N` | brokerage, STT, exchange, SEBI, stamp duty, GST for a round trip |
| `backtest --dataset DIR --strategy FILE` | whole-dataset simulation (exploration only) |
| `gate --dataset DIR --strategy FILE` | the 10,000-example validation gate with train/validation/test |
| `sahi-check` | mints a read-only token and fetches sample data, without trading |
| `fetch-history --start D --end D --out DIR` | stock/ETF dataset from SAHI historical candles (estimated spread) |
| `record` | records index, option, stock and ETF bars plus bid/ask snapshots from SAHI |
| `build-dataset --out DIR --name N [--kind equity]` | turns recordings into a dataset for `gate` |
| `paper --strategy FILE [--dataset DIR] [--until HH:MM] ...` | paper-trading loop; repeat `--strategy` to run stocks first, then options; `--until` ends a scheduled run |
| `risk-status`, `report` | today's lockout state; read-only SIMULATED report |
| `news --feeds FILE` | headlines from your feeds plus scheduled events |
| `pelosi` | why the US Pelosi tracker has no Indian counterpart here |

Global flags go before the command: `python3 -m nse_bot --config config/paper.json --now 2026-10-30T10:00 clock`.

## SAHI credentials

Create a SAHI API key with only the `marketdata.read` scope, approve it for the
day in the SAHI dashboard, then export it in your shell. Never put it in a file
in this repository.

```bash
export SAHI_KEY_ID=...        # not secret, but keep it out of git anyway
export SAHI_KEY_SECRET=...    # secret: environment only
python3 -m nse_bot --config config/paper.json sahi-check
```

The client asks SAHI for a token scoped to `marketdata.read` and refuses a
token with any other scope, so it cannot place orders even by mistake.

## Stocks, ETFs and options

`instruments_enabled` in the config chooses equity, etf and/or index_options.
Stocks and ETFs come from `config/universe.example.json` (copy and edit it);
every symbol must exist in SAHI's scrip master or the bot refuses to start.
Stock trades are long-only intraday, sized in whole shares from cash (no
leverage), and charged at SAHI's equity-intraday rates.

Options are used only when suitable: the option strategy must have passed its
own validation gate, at least one lot must fit the Rs 500 risk and outlay
budget, and the quote must pass the liquidity checks. Give the stock strategy
first and the option strategy second; with one open position allowed, the first
trade that passes every check is the only one taken.

Stock/ETF history can come from SAHI's candles (`fetch-history`). Those have no
bid/ask, so fills use an assumed spread (default 0.10%, minimum 0.05%) and are
labelled; you allowed this for stocks and ETFs only (`gate_allow_estimated_spread`).
Options always need recorded bid/ask.

## How a trade is decided

clock → instruments → quote quality → scanner → entry candidate → exit plan →
**validation gate** → budget (lots) → risk (limits + lockout) → paper broker →
ledger, alerts, report. Any refusal stops the chain and says why.

The gate is the hard part: a strategy needs at least 10,000 valid independent
historical examples, on real option data with bid/ask, before any entry is
allowed. Without such data every candidate is refused with INSUFFICIENT DATA.
See `docs/nse/AUDIT.md` for what that means in practice.

## Documents

- `docs/nse/STAGE_A.md` inventory, gap analysis and architecture
- `docs/nse/AUDIT.md` final audit: what works, test counts, blockers
- `docs/nse/DEPLOY.md` running it on a computer or server
- `docs/nse/RECOVERY.md` corrupted state, expired tokens, missing data, API failures
- `docs/nse/IPHONE_HOSTING.md` free hosting on GitHub Actions, run from an iPhone
