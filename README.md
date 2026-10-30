# NSE options bot (paper trading, read-only data)

NIFTY and BANKNIFTY index options on NSE, adapted from the 11-skill
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
| `record` | records index/option bars plus bid/ask snapshots from SAHI |
| `build-dataset --out DIR --name N` | turns recordings into a dataset for `gate` |
| `paper --strategy FILE [--dataset DIR]` | paper-trading loop |
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
