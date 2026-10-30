"""Single entry point: python -m nse_bot <command> [options]

  validate-config   check a config file; exits non-zero on any problem
  clock             NSE market status in IST
  instruments       parse a SAHI scrip master file and list NIFTY/BANKNIFTY expiries
  costs             charges for one round trip
  backtest          run a strategy over a dataset (SIMULATED)
  gate              run the 10,000-example validation gate
  paper             paper-trading loop on read-only SAHI data (needs SAHI_KEY_ID/SAHI_KEY_SECRET);
                    --strategy may repeat (e.g. a stock strategy, then an index-option one)
  sahi-check        verify the read-only SAHI connection without trading
  record            record index/option bars with bid/ask snapshots from SAHI (builds real history)
  build-dataset     combine recorded sessions into a dataset with a manifest (--kind options|equity)
  fetch-history     stock/ETF dataset from SAHI historical candles (estimated spread, no bid/ask)
  risk-status       today's risk state and lockout
  report            read-only performance report (SIMULATED)
  news              headlines from your feeds file + upcoming scheduled events
  pelosi            the Pelosi-tracker applicability assessment

Exit codes: 0 ok, 2 refused/invalid (config, data, gate not approved), 3 unexpected error.
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time

from . import (analyst, clock, config, costs as costs_mod, events as events_mod, gate, instruments,
               news, pelosi, report, risk)
from .alerts import Alerts
from .backtest import engine, equity_engine, metrics
from .data import store
from .errors import ConfigError, NseBotError

LOG = logging.getLogger("nse_bot")


def _setup_logging(state_dir):
    os.makedirs(os.path.join(state_dir, "logs"), exist_ok=True)

    class J(logging.Formatter):
        def format(self, r):
            return json.dumps({"t": clock.now_ist().isoformat(), "level": r.levelname, "msg": r.getMessage()})

    h = logging.FileHandler(os.path.join(state_dir, "logs", "nse_bot.jsonl"))
    h.setFormatter(J())
    LOG.addHandler(h)
    LOG.setLevel(logging.INFO)


def _now(a):
    if getattr(a, "now", None):
        t = dt.datetime.fromisoformat(a.now)
        return t.replace(tzinfo=clock.IST) if t.tzinfo is None else clock.to_ist(t)
    return clock.now_ist()


def _strategy(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _common(cfgpath):
    cfg = config.load(cfgpath)
    cal = clock.Calendar.load(cfg["holidays_file"])
    cm = costs_mod.CostModel.load(cfg["costs_file"])
    return cfg, cal, cm


def cmd_validate(a):
    config.load(a.config)
    print("OK: %s is valid (paper mode, live orders disabled)" % a.config)


def cmd_clock(a):
    cfg = config.load(a.config)
    print(json.dumps(clock.Calendar.load(cfg["holidays_file"]).status(_now(a)), indent=2))


def cmd_instruments(a):
    cfg = config.load(a.config)
    m = instruments.load(a.master, cfg["underlyings"], list(cfg["index_names"].values()))
    cal = clock.Calendar.load(cfg["holidays_file"])
    today = _now(a).date()
    for u in cfg["underlyings"]:
        ex = m.expiries(u, today)[:6]
        print("%s: %d contracts; next expiries %s" % (u, sum(1 for c in m.contracts if c.underlying == u),
              ", ".join("%s%s" % (e, "" if cal.check_expiry(u, e) else " (FAILS calendar cross-check)") for e in ex)))
        if ex:
            print("   lot size %s" % sorted({c.lot_size for c in m.chain(u, ex[0])}))
    print("indices: %s" % m.indices)
    if m.rejected:
        print("rejected rows: %d (first: %s)" % (len(m.rejected), m.rejected[0]))


def cmd_costs(a):
    cfg, cal, cm = _common(a.config)
    day = dt.date.fromisoformat(a.date) if a.date else _now(a).date()
    print(json.dumps(cm.round_trip(a.buy, a.sell, a.qty, day), indent=2))


def _load_ds(path):
    """Option or stock/ETF dataset, chosen by the manifest's kind."""
    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
            kind = json.load(f).get("kind")
    except (OSError, ValueError):
        kind = None
    return store.load_equity(path) if kind == "equity" else store.load(path)


def cmd_backtest(a):
    cfg, cal, cm = _common(a.config)
    ds = _load_ds(a.dataset)
    strat = _strategy(a.strategy)
    eng = equity_engine if strat.get("instrument") == "equity" else engine
    r = eng.run(ds, strat, cfg, cm, cal)
    out = {"label": report.BANNER, "synthetic_data": ds.synthetic,
           "fill_model": getattr(ds, "fill_model", "quotes"), "counts": r["counts"],
           "invalid": r["invalid"], "unresolved": r["unresolved"], "metrics": metrics.compute(r["trades"]),
           "by_underlying": metrics.by(r["trades"], "underlying"), "by_regime": metrics.by(r["trades"], "regime"),
           "note": "Whole-dataset run for exploration. Use `gate` for train/validation/test evaluation."}
    print(json.dumps(out, indent=2, default=str))


def cmd_gate(a):
    cfg, cal, cm = _common(a.config)
    ds = _load_ds(a.dataset)
    doc = gate.evaluate(ds, _strategy(a.strategy), cfg, cm, cal, cfg["state_dir"])
    print(json.dumps(doc, indent=2, default=str))
    return 0 if doc["status"] == "APPROVED" else 2


def cmd_risk_status(a):
    cfg = config.load(a.config)
    book = risk.RiskBook.open(cfg["state_dir"], _now(a).date())
    print(json.dumps(dict(book.doc, remaining_loss_inr=book.remaining_loss(cfg), warning=risk.WARNING), indent=2))


def cmd_report(a):
    cfg = config.load(a.config)
    print(report.render(report.build(cfg["state_dir"])))


def cmd_news(a):
    cfg = config.load(a.config)
    now = _now(a)
    feeds = news.load_feeds(a.feeds) if a.feeds else []
    items, errors = news.collect(feeds, now, a.hours)
    evs = events_mod.load(cfg["events_file"])
    for it in items:
        when = it["published"].strftime("%d %b %H:%M IST") if it["published"] else "time unknown"
        print("%s  [%s] %s\n    %s  %s" % (when, it["source"], it["title"], ",".join(it["tags"]), it["link"]))
    for e in events_mod.upcoming(evs, now, 72):
        print("EVENT %s  %s (%s)  %s" % (e["time"].strftime("%d %b %H:%M IST"), e["title"], e["impact"], e["source_url"]))
    for e in errors:
        print("feed error: %s" % e, file=sys.stderr)


def cmd_pelosi(a):
    print(json.dumps(pelosi.assess(), indent=2))


def _sahi_ctx(a):
    from .broker import sahi
    cfg, cal, cm = _common(a.config)
    if not os.path.exists(a.master) or a.refresh_master:
        n = sahi.download_scrip_master(a.master)
        LOG.info("scrip master downloaded: %d bytes", n)
    m = instruments.load(a.master, cfg["underlyings"], list(cfg["index_names"].values()))
    if {"equity", "etf"} & set(cfg["instruments_enabled"]):
        m.bind_universe(instruments.load_universe(cfg["equity_universe_file"]))
    md = sahi.SahiReadOnly()
    return cfg, cal, cm, m, md


def cmd_sahi_check(a):
    cfg, cal, cm, m, md = _sahi_ctx(a)
    info = md.mint()
    print("token minted: scope=%s expires_at=%s (token not shown)" % (info["scope"], info["expires_at"]))
    for u in cfg["underlyings"]:
        iid = m.index_id(cfg["index_names"][u])
        print("%s index instrument_id %d; SAHI expiries: %s" % (u, iid, [str(x) for x in md.expiries(iid)[:4]]))
        ex = m.expiries(u, _now(a).date())
        if ex:
            c = m.chain(u, ex[0])[len(m.chain(u, ex[0])) // 2]
            q = md.quotes([c.instrument_id]).get(c.instrument_id)
            print("  sample %s: bid %s ask %s ts %s problems %s" % (
                c.symbol, q and q.bid, q and q.ask, q and q.ts,
                q and __import__("nse_bot.quotes", fromlist=["check"]).check(q, _now(a), cfg, c.lot_size)))
    from .quotes import check_for
    for st in list(m.stocks.values())[:3]:
        q = md.quotes([st.instrument_id]).get(st.instrument_id)
        print("  %s id %d: bid %s ask %s problems %s" % (st.symbol, st.instrument_id, q and q.bid, q and q.ask,
                                                         q and check_for(st, q, _now(a), cfg)))
    print("read-only connection verified from this machine")


def cmd_paper(a):
    from . import bot
    from .broker.paper import PaperBroker
    cfg, cal, cm, m, md = _sahi_ctx(a)
    strats = [_strategy(p) for p in a.strategy]
    evs = events_mod.load(cfg["events_file"])
    datasets = a.dataset or []
    if datasets and len(datasets) != len(strats):
        raise ConfigError("give one --dataset per --strategy (same order), or none")
    digest = {s["name"]: _load_ds(d).digest for s, d in zip(strats, datasets)} or None
    broker = PaperBroker(cfg["state_dir"], cm, cfg["slippage_ticks"])
    broker.ledger.verify()
    print(report.BANNER)
    while True:
        now = clock.now_ist()
        book = risk.RiskBook.open(cfg["state_dir"], now.date())
        al = Alerts(cfg["state_dir"], now.date())
        ctx = bot.Context(cfg, cal, m, md, cm, broker, book, al, strats, evs, digest)
        try:
            res = bot.run_once(ctx, now)
            LOG.info(json.dumps(res, default=str)[:4000])
        except NseBotError as e:
            book.record_error(cfg, now)
            al.emit("system_error", "loop-%s" % now.strftime("%H%M%S"), str(e), now)
        if a.once:
            return
        if not cal.status(now)["open"] and now.time() > clock.SESSION_CLOSE:
            print("session closed; exiting (the supervisor restarts the bot next morning)")
            return
        time.sleep(a.every)


def cmd_record(a):
    from .data import recorder
    cfg, cal, cm, m, md = _sahi_ctx(a)
    step = dt.timedelta(minutes=a.interval)
    print("recording %d-minute bars to %s (Ctrl-C to stop)" % (a.interval, a.out))
    while True:
        now = clock.now_ist()
        st = cal.status(now)
        if not st["open"]:
            if now.time() > clock.SESSION_CLOSE or a.once:
                return
            time.sleep(30)
            continue
        opened = clock.at(now.date(), "09:15")
        k = int((now - opened) / step)
        bar_start = opened + (k - 1) * step
        if bar_start >= opened:
            try:
                n = recorder.record_bar(md, m, cfg, bar_start, a.interval, a.out)
                LOG.info("recorded %s: %d rows", bar_start.isoformat(), n)
            except NseBotError as e:
                LOG.error("record failed for %s: %s", bar_start.isoformat(), e)
                print("record failed: %s" % e, file=sys.stderr)
        if a.once:
            return
        nxt = opened + (k + 1) * step + dt.timedelta(seconds=a.delay)
        time.sleep(max(1.0, (nxt - clock.now_ist()).total_seconds()))


def cmd_build_dataset(a):
    from .data import recorder
    fn = recorder.build_equity if a.kind == "equity" else recorder.build
    doc = fn(a.recordings, a.out, a.name, a.interval)
    print(json.dumps(doc, indent=2))


def cmd_fetch_history(a):
    from .data import history
    cfg, cal, cm, m, md = _sahi_ctx(a)
    if not m.stocks:
        raise ConfigError("no stocks/ETFs enabled: check instruments_enabled and the universe file")
    res = history.fetch_equity_history(md, m.stocks.values(), cal, dt.date.fromisoformat(a.start),
                                       dt.date.fromisoformat(a.end), a.interval, a.out, a.spread_pct)
    print(json.dumps(res, indent=2))
    return 2 if res["errors"] else 0


def build_parser():
    p = argparse.ArgumentParser(prog="python -m nse_bot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/paper.json")
    p.add_argument("--now", help="ISO time (IST if no offset); for replay and tests")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate-config").set_defaults(fn=cmd_validate)
    sub.add_parser("clock").set_defaults(fn=cmd_clock)
    s = sub.add_parser("instruments"); s.add_argument("--master", required=True); s.set_defaults(fn=cmd_instruments)
    s = sub.add_parser("costs")
    s.add_argument("--buy", type=float, required=True); s.add_argument("--sell", type=float, required=True)
    s.add_argument("--qty", type=int, required=True); s.add_argument("--date"); s.set_defaults(fn=cmd_costs)
    for name, fn in (("backtest", cmd_backtest), ("gate", cmd_gate)):
        s = sub.add_parser(name); s.add_argument("--dataset", required=True)
        s.add_argument("--strategy", required=True); s.set_defaults(fn=fn)
    for name, fn in (("paper", cmd_paper), ("sahi-check", cmd_sahi_check), ("record", cmd_record),
                     ("fetch-history", cmd_fetch_history)):
        s = sub.add_parser(name)
        s.add_argument("--master", default="state/instruments.csv")
        s.add_argument("--refresh-master", action="store_true")
        if name == "paper":
            s.add_argument("--strategy", required=True, action="append",
                           help="repeat to run several strategies; tried in the order given")
            s.add_argument("--dataset", action="append", help="dataset each strategy was approved on, same order")
            s.add_argument("--every", type=int, default=15)
            s.add_argument("--once", action="store_true")
        if name == "fetch-history":
            s.add_argument("--start", required=True); s.add_argument("--end", required=True)
            s.add_argument("--interval", type=int, default=5); s.add_argument("--out", required=True)
            s.add_argument("--spread-pct", type=float, default=0.10,
                           help="assumed full bid/ask spread in percent (minimum 0.05)")
        if name == "record":
            s.add_argument("--out", default="state/recordings")
            s.add_argument("--interval", type=int, default=5)
            s.add_argument("--delay", type=int, default=5, help="seconds after bar close before recording")
            s.add_argument("--once", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("build-dataset")
    s.add_argument("--recordings", default="state/recordings"); s.add_argument("--out", required=True)
    s.add_argument("--name", required=True); s.add_argument("--interval", type=int, default=5)
    s.add_argument("--kind", choices=("options", "equity"), default="options")
    s.set_defaults(fn=cmd_build_dataset)
    sub.add_parser("risk-status").set_defaults(fn=cmd_risk_status)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    s = sub.add_parser("news"); s.add_argument("--feeds"); s.add_argument("--hours", type=int, default=24)
    s.set_defaults(fn=cmd_news)
    sub.add_parser("pelosi").set_defaults(fn=cmd_pelosi)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd not in ("validate-config", "pelosi"):
            try:
                _setup_logging(config.load(a.config)["state_dir"])
            except NseBotError:
                pass
        rc = a.fn(a)
        return rc or 0
    except NseBotError as e:
        print("REFUSED: %s" % e, file=sys.stderr)
        LOG.error("refused: %s", e)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:                                # unexpected: report, do not hide
        LOG.exception("unexpected error")
        print("ERROR (unexpected): %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 3
