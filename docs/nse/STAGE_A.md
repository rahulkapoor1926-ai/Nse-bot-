# Stage A: inventory, backup, architecture and gap analysis

Date: 2026-10-30 (Asia/Kolkata). Branch: `nse-options` in an isolated copy of
upstream `Jakeschincariol/day-trader-skill` at commit `3d0f4ef`.

## 1. What was done

| step | result |
|---|---|
| Clone upstream | 85 tracked files, 11 skills, 12 test modules, 315 upstream tests: **all pass** (`python3 -m unittest discover -s tests`) |
| Backup | `/mnt/project-files/nse-options-bot/backup/day-trader-skill-3d0f4ef….tar.gz` (full tree, no `.git`) |
| Protected-file manifest | `docs/nse/protected-sha256.txt`: SHA-256 of all 85 upstream files |
| File inventory | `docs/nse/INVENTORY.md` |
| Isolated copy | `nse-options-bot/`, branch `nse-options`, upstream remote renamed `upstream` (no push target) |
| trader-alerts-nse | **Not found.** Not in this workspace and no GitHub account is linked. Asked the user where it lives. |

## 2. Upstream inventory, skill by skill

All upstream tools are single-file, standard-library Python scripts that read
and write JSON under a `trader/` folder. Every one is hard-wired to the US.

| skill | upstream file | what it really does | US-specific parts |
|---|---|---|---|
| scanner | `trader-scanner/scan.py` (1083) | Nasdaq symbol directory, daily RSI/flag setups, 5-minute opening-range breakouts over Yahoo "spark" data | Nasdaq lists, Yahoo, NYSE hours 09:30–16:00 ET, US DST |
| news | `trader-news/news.py` (546) | Yahoo RSS headlines + SEC 8-K Atom feed, dedupe, ticker tagging | Yahoo/SEC, EDGAR CIK map, ET clock |
| entry | `trader-entry/entry.py` (960) | ORB / level-cross trigger evaluator, bar by bar, long only | stock shares, Yahoo bars, ET |
| exit | `trader-exit/exit.py` (735) | stop (OR-mid, ATR, %, price) and R-multiple target, flat-by time | cents rounding, ET, stocks |
| timemachine | `trader-timemachine/timemachine.py` (956) | **10,000 Monte Carlo resamples** of one stock's own 5-minute bars + replay of the rule on ~59 past sessions | the "10,000" is bootstrap resampling, not independent examples |
| risk | `trader-risk/check.py` (819) | PASS/BLOCK per order: max loss, daily loss, trade count, R:R, entry window, size, cash, PDT | daily loss = sum of *planned* max losses, not realised P&L; no persistent lockout; options disallowed |
| budget | `trader-budget/size.py` (881) | shares from max loss / stop distance; Alpaca bracket order JSON | shares not lots, USD, no charges |
| broker | `trader-broker/ticket.py` (720) | ticket rendering, exact-order "yes" parsing, fingerprinted decision log; never calls a broker itself | Alpaca MCP schema |
| alerts | `trader-alerts/alerts.py` (787) | NYSE clock, Yahoo/Stooq quotes, stop/target/time alerts with ring-once state | NYSE calendar, US quotes |
| analyst | `trader-analyst/facts.py` (548) | SEC XBRL company facts, quarterly margins/EPS | SEC only |
| pelosi | `trader-pelosi/ptr.py` (566) | US House PTR disclosure index and PDF parsing | US Congress only |

## 3. Facts verified for the Indian adaptation (sources read 2026-10-30)

| fact | value | source |
|---|---|---|
| SAHI API base hosts | REST `https://direct.sahi.com`, auth `https://apis.sahi.com`, scrip master `https://assets.sahi.com/external/instruments.csv` | official SDK `sahiapi` 0.3.0 on PyPI, `sahi/env.py`; docs.sahi.com |
| SAHI auth | `POST /api/v1/apikeys/token` with key id + secret (+ TOTP for TOTP keys); Bearer JWT; expires at the next 06:00 IST; key must be approved daily; no refresh tokens | docs.sahi.com/api/v1/reference/authentication |
| SAHI read-only data | `marketdata.read` scope: `/v1/marketdata/ltp`, `/quotes` (depth, OI, DPR), `/candles` (1m–1d, ≤2,500 per call, paise, epoch ms), `/optionchain`, `/optionchain/expiries` | docs.sahi.com/api/v1/reference/market-data |
| SAHI rate limits | quotes/LTP 1/s; candles 2/s, 250/min, 100k/day; option chain 2/s, 120/min; `429` + `Retry-After` | docs.sahi.com |
| Scrip master columns | `id, symbol, underlying_symbol, segment, instrument_type, option_type, expiry_date (DD-MM-YYYY), strike_price (₹), lot_size, tick_size, freeze_quantity, …`; rebuilt ~06:00 IST daily | docs.sahi.com/api/v1/reference/scrip-master |
| Lot sizes from Jan 2026 series | NIFTY 65, BANKNIFTY 30 (code reads `lot_size` from the scrip master; these are fallbacks for tests only) | sahi.com blog; NSE lot-size circular |
| Expiry days | NSE: NIFTY weekly on Tuesday; NIFTY and BANKNIFTY monthly on the last Tuesday; no BANKNIFTY weekly | NSE circular 108/2025 (via Fyers notice) |
| 2026 F&O holidays | 15 weekday holidays incl. 26 Jan, 3 Mar, 26 Mar, 31 Mar, 3 Apr, 14 Apr, 1 May, 28 May, 26 Jun, 14 Sep, 2 Oct, 20 Oct, 10 Nov, 24 Nov, 25 Dec; Muhurat session 8 Nov | NSE circular 212/2025 (NSE/FAOP/71777) |
| SAHI brokerage | ₹10 flat per executed order, index options | sahi.com/pricing |
| Statutory charges (options) | STT 0.15% of premium on sell (from 1 Apr 2026; was 0.10%); NSE transaction 0.0355299% of premium; SEBI 0.0001%; stamp 0.003% on buy; GST 18% on brokerage + exchange + SEBI | sahi.com/pricing; ICICI Direct on Budget 2026 |

Expiry dates on holidays are taken from the scrip master and the SAHI expiries
endpoint, never computed alone. The computed "previous trading day" rule is used
only to cross-check, and a mismatch fails closed.

## 4. Gap analysis and blockers

1. **trader-alerts-nse is missing.** Nothing can be reused from it until the user says where it is. The plan below rebuilds alerts on the upstream design; it will be diffed against trader-alerts-nse when available.
2. **SAHI is unreachable from this container.** The network policy refuses every `*.sahi.com` host (proxy `403` on CONNECT). The adapter is written against the official docs and SDK source and tested offline with recorded-shape fixtures. A live connection can only be verified on the user's machine. No credential is requested.
3. **Historical option data is the hard constraint for the 10,000-example gate.** SAHI candles address instruments by `instrument_id` from today's scrip master, and the docs describe no way to fetch expired contracts. Weekly NIFTY options expire every Tuesday, so most history is expired contracts. NSE's F&O bhavcopy gives daily OHLC/OI/settle per expired contract but no intraday bars and no bid/ask. Intraday option history with bid/ask needs a vendor dataset the user supplies. Until such data is loaded, the gate returns `INSUFFICIENT DATA` and rejects every candidate. That is the designed behaviour, not a bug.
4. **Upstream "10,000" cannot be reused as the gate.** Resampled paths are not independent examples. The new gate counts distinct (instrument, expiry, strike, timestamp) occurrences and separately counts non-overlapping examples and executed simulated trades.
5. **Upstream risk has no realised-P&L lockout.** It sums planned stop losses. The new risk module tracks realised + open mark-to-market loss, persists a lockout file atomically, and fails closed on a corrupt state file.
6. **Capital vs. limits.** ₹500 risk on one NIFTY lot (65) is a stop of about ₹7.7 premium points; on one BANKNIFTY lot (30) about ₹16.7. A round trip on a ₹150 NIFTY premium costs roughly ₹45–50 in brokerage, STT, exchange fees, GST and stamp duty before slippage. The ₹2,000 "target" is 10% of capital in a day; it is only a stop-trading-for-the-day threshold.
7. **Pelosi tracker.** There is no Indian counterpart that bears on NIFTY/BANKNIFTY index options. SEBI insider (PIT) disclosures and bulk/block deals are about single stocks. The module will report `NOT_APPLICABLE` and has no path into trading decisions.
8. **Live orders.** SAHI order endpoints need `orders.write` and a registered static IP. This phase adds no order endpoint: the broker adapter's order method raises `LiveTradingDisabled` unconditionally and a test asserts no order path exists.

## 5. Architecture

A new Python package `nse_bot/` sits beside the untouched `trader-*` folders.
Upstream logic is *ported* (ORB evaluator, stop/target rules, ticket
fingerprints, alert ring-once state, RSS dedupe), never edited in place.

```
nse_bot/
  __main__.py, cli.py      single entry point: python -m nse_bot <command>
  config.py                load + validate; unknown or missing keys fail closed
  clock.py                 Asia/Kolkata, NSE session 09:15–15:30, holidays file, expiry cross-check
  instruments.py           SAHI scrip master parser -> NIFTY/BANKNIFTY OPTIDX contracts
  costs.py                 brokerage + STT + exchange + SEBI + stamp + GST, dated rate tables
  quotes.py                Quote model; stale / crossed / illiquid / missing checks
  data/store.py            historical candle store with provenance hashes, de-duplication
  scanner.py               option-contract scan over configurable patterns
  news.py                  Indian RSS/event calendar with timestamps and source links
  entry.py                 rule-based candidates with reasons + invalidation
  exit.py                  stop, target, time exit, stale-data and gap handling
  backtest/                walk-forward splits, cost-aware fills, metrics, CIs, sensitivity, baseline
  gate.py                  10,000-occurrence validation gate -> APPROVED / REJECTED / INSUFFICIENT DATA
  risk.py                  per-trade, daily loss, exposure, trade count, persistent lockout
  budget.py                lots from capital, premium, stop distance and charges
  broker/base.py           adapter interface (read-only)
  broker/sahi.py           read-only SAHI market data client (httpx-free, urllib)
  broker/paper.py          paper fills from bid/ask + slippage, persistent ledger
  alerts.py                price, candidate, stop, target, stale-data, system-error
  analyst.py               macro/event context only when a rule asks for it
  pelosi.py                applicability assessment, NOT_APPLICABLE
  report.py                read-only performance report, labelled SIMULATED
  state.py                 atomic JSON writes, corruption detection
config/paper.example.json
tests/nse/                 unit, integration and safety tests (synthetic data only)
docs/nse/                  this file, inventory, deployment, recovery
```

Data flow for one decision: clock -> instruments -> quotes (quality) -> scanner
-> entry -> exit -> gate (backtest evidence) -> budget -> risk -> paper broker
-> ledger -> alerts/report. Any failing step returns a typed refusal; nothing
downstream runs.

## 6. File-change plan for Stage B

Create only (no upstream file is modified):

- `nse_bot/__init__.py, __main__.py, cli.py, config.py, clock.py, instruments.py, costs.py, quotes.py, state.py`
- `nse_bot/scanner.py, news.py, entry.py, exit.py, risk.py, budget.py, alerts.py, analyst.py, pelosi.py`
- `nse_bot/broker/__init__.py, base.py, paper.py`
- `nse_bot/data/nse_holidays_2026.json` (from NSE circular 212/2025)
- `config/paper.example.json`
- `tests/nse/` one test module per nse_bot module, plus `test_protected.py`
- `pytest.ini` scoped to `tests/nse` is **not** added, so upstream discovery stays as it was

Stage C adds `nse_bot/data/store.py`, `nse_bot/backtest/*`, `nse_bot/gate.py`;
Stage D adds `report.py` and safety tests; Stage E adds `nse_bot/broker/sahi.py`;
Stage F adds `docs/nse/DEPLOY.md` and `docs/nse/RECOVERY.md`.
