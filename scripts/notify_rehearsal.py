"""End-to-end rehearsal of the P&L notifications with LIVE FYERS prices and a THROWAWAY ledger.

What it does (paper only; no order endpoint exists anywhere in the bot):
  1. Logs in to FYERS (read-only) and picks today's NIFTY ATM CE from the real symbol master.
  2. Opens a SIMULATED position in a temporary paper ledger (a fresh temp directory, never
     state/ or the paper-state branch) at the live FYERS ask, only if the quote passes the
     bot's normal freshness/spread checks.
  3. Runs the bot's real exit loop (bot.manage_positions) every 30 s on live FYERS quotes, with
     the time exit set a few minutes ahead, so the real close path fires.
  4. Once during the run it feeds the loop a deliberately aged copy of the live quote, to show a
     stale price produces "PRICE NOT VERIFIED" and no P&L.
  5. Sends the daily summary for the throwaway ledger, then checks the notification log:
     exactly one open, one close, one summary; updates at least 5 minutes apart; no duplicate
     texts; every update's bid from FYERS and fresh; nothing misleading on stale data.

The validation gate, strategies and risk limits are not touched: this script never calls the
entry logic. Every message carries a REHEARSAL banner so it cannot be mistaken for a trade.
"""

import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nse_bot import bot, clock, config, costs as costs_mod, exit as exit_mod, instruments, quotes, risk  # noqa: E402
from nse_bot.alerts import Alerts  # noqa: E402
from nse_bot.broker import fyers  # noqa: E402
from nse_bot.broker.paper import PaperBroker  # noqa: E402
from nse_bot.notify import Notifier  # noqa: E402

BANNER = "REHEARSAL (throwaway ledger, NOT a trade; live FYERS prices)"
STALE_AT_MINUTE = 2


class AgedOnce(object):
    """Wraps the FYERS adapter; one call returns the live quote with its time pushed back 60 s."""

    def __init__(self, md):
        self.md, self.armed, self.used = md, False, False

    def quotes(self, ids):
        out = self.md.quotes(ids)
        if self.armed and not self.used:
            self.used = True
            for k, q in out.items():
                if q is not None and q.ts is not None:
                    out[k] = dataclasses.replace(q, ts=q.ts - dt.timedelta(seconds=60),
                                                 extra=dict(q.extra, received_at=None))
        return out

    def __getattr__(self, name):
        return getattr(self.md, name)


def main(minutes=11):
    cfg = config.load("config/paper.example.json")
    if cfg["data_source"] != "fyers":
        print("config data_source is not fyers; refusing")
        return 2
    work = tempfile.mkdtemp(prefix="rehearsal-")
    cfg = dict(cfg, state_dir=work, notify_label=BANNER)
    cal = clock.Calendar.load(cfg["holidays_file"])
    cm = costs_mod.CostModel.load(cfg["costs_file"])
    now = clock.now_ist()
    if not cal.status(now)["open"]:
        print("market is closed (%s); live prices are needed, nothing done" % now.isoformat())
        return 2
    path = os.path.join(work, "instruments-fyers.csv")
    fyers.build_master(path, cfg["underlyings"], cfg["index_names"], with_cash=False)
    m = instruments.load(path, cfg["underlyings"], list(cfg["index_names"].values()))
    live = fyers.FyersReadOnly(per_minute=20, per_second=2, per_day=300).bind(m, fyers.load_symbols(path))
    live.login()
    md = AgedOnce(live)
    spot_id = m.index_id(cfg["index_names"]["NIFTY"])
    spot = live.quotes([spot_id])[spot_id]
    if spot is None or spot.ltp is None:
        print("no NIFTY spot from FYERS; nothing done")
        return 1
    expiry = m.expiries("NIFTY", now.date())[0]
    c = m.pick("NIFTY", expiry, "CE", spot.ltp, 0)
    print("spot %.2f; contract %s" % (spot.ltp, c))

    broker = PaperBroker(work, cm, cfg["slippage_ticks"])
    strategy = json.load(open("config/strategies/orb15.example.json"))
    for attempt in range(6):
        now = clock.now_ist()
        q = live.quotes([c.instrument_id])[c.instrument_id]
        problems = quotes.check_for(c, q, now, cfg)
        if not problems:
            break
        print("entry quote not usable yet: %s" % problems)
        time.sleep(10)
    else:
        print("no usable FYERS quote for %s; rehearsal not started (no fill on bad data)" % c.symbol)
        return 1
    plan = exit_mod.plan(q.ask, c, strategy, cfg, now.date())
    flat = clock.to_ist(now) + dt.timedelta(minutes=minutes)
    plan["flat_by"] = flat.strftime("%H:%M")
    res = broker.buy(c, 1, q, now, meta={"plan": plan, "strategy": "rehearsal", "data_source": "fyers",
                                         "invalidate_below": None, "invalidate_above": None})
    if res["status"] == "FAILED":
        print("simulated fill failed: %s" % res["reason"])
        return 1
    Notifier(work, now.date(), cfg).opened(c, res, plan, now, "rehearsal (no strategy signal)")

    start = clock.now_ist()
    while True:
        now = clock.now_ist()
        md.armed = (now - start).total_seconds() >= STALE_AT_MINUTE * 60
        ctx = bot.Context(cfg, cal, m, md, cm, broker, risk.RiskBook.open(work, now.date()),
                          Alerts(work, now.date()), strategy, notifier=Notifier(work, now.date(), cfg))
        ev = bot.manage_positions(ctx, now, {})
        print(now.strftime("%H:%M:%S"), ev, ctx.marks)
        if broker.ledger.positions()[str(c.instrument_id)]["qty"] == 0:
            break
        if (now - start).total_seconds() > (minutes + 5) * 60:
            print("position did not close within the window (no fresh bid?); left open in the throwaway ledger")
            break
        time.sleep(30)
    end = clock.now_ist()
    Notifier(work, end.date(), cfg).daily_summary([("rehearsal", broker.ledger.records())], end.date(), end,
                                                  {str(c.instrument_id): c.symbol})
    return verify(work, c)


def verify(work, c):
    log = [json.loads(x) for x in open(os.path.join(work, "notify", "notify.jsonl")).read().splitlines()]
    kinds = [x["kind"] for x in log]
    ups = [x for x in log if x["kind"] == "update"]
    stale = [x for x in log if x["kind"] == "stale"]
    times = [clock.to_ist(dt.datetime.fromisoformat(x["time"])) for x in ups]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    texts = [x["text"] for x in log]
    checks = [
        ("1 trade-open message", kinds.count("opened") == 1),
        ("live P&L updates sent (%d)" % len(ups), len(ups) >= 2),
        ("updates at least 5 min apart (gaps %s s)" % [int(g) for g in gaps], all(g >= 300 for g in gaps)),
        ("updates use a fresh FYERS bid", all("(fyers, quote time" in x["text"] and "Unrealized P&L" in x["text"]
                                              for x in ups)),
        ("stale price reported without P&L (%d)" % len(stale),
         len(stale) >= 1 and all("PRICE NOT VERIFIED" in x["text"] and "Unrealized" not in x["text"] for x in stale)),
        ("1 trade-close message with charges and net", kinds.count("closed") == 1 and all(
            k in [x for x in log if x["kind"] == "closed"][0]["text"]
            for k in ("Gross P&L", "Entry charges", "Exit charges", "Total costs", "NET P&L"))),
        ("1 daily summary", kinds.count("summary") == 1),
        ("no duplicate messages", len(texts) == len(set(texts))),
        ("every message delivered to Telegram", all(x["delivered"] for x in log)),
        ("every message carries the REHEARSAL banner", all(x["text"].startswith(BANNER) for x in log)),
    ]
    print("\n== notification checks (%s)" % c.symbol)
    for name, ok in checks:
        print("%-5s %s" % ("PASS" if ok else "FAIL", name))
    print("ledger used: %s (temporary; discarded with the runner)" % work)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main(int(os.environ.get("REHEARSAL_MINUTES", "11"))))
