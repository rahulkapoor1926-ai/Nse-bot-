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
  dashboard         rebuilds state/dashboard/data.json for the web page (read-only)
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

from . import (analyst, clock, config, costs as costs_mod, dashboard, events as events_mod, gate, instruments,
               news, pelosi, report, risk)
from .alerts import Alerts
from .notify import Notifier
from .backtest import engine, equity_engine, metrics
from .data import store
from .errors import ConfigError, DataError, NseBotError

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
    book = risk.open_for(cfg, _now(a).date())
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
    """Market-data context for the session's single configured source (never mixed)."""
    cfg, cal, cm = _common(a.config)
    if cfg["data_source"] == "fyers":
        return _fyers_ctx(a, cfg, cal, cm)
    from .broker import sahi
    if not os.path.exists(a.master) or a.refresh_master:
        n = sahi.download_scrip_master(a.master)
        LOG.info("scrip master downloaded: %d bytes", n)
    m = instruments.load(a.master, cfg["underlyings"], list(cfg["index_names"].values()))
    if {"equity", "etf"} & set(cfg["instruments_enabled"]):
        m.bind_universe(instruments.load_universe(cfg["equity_universe_file"]))
    md = sahi.SahiReadOnly()
    return cfg, cal, cm, m, md


def _fyers_ctx(a, cfg, cal, cm):
    from .broker import fyers
    path = "state/instruments-fyers.csv" if a.master == "state/instruments.csv" else a.master
    if not os.path.exists(path) or a.refresh_master:
        info = fyers.build_master(path, cfg["underlyings"], cfg["index_names"],
                                  with_cash=bool({"equity", "etf"} & set(cfg["instruments_enabled"])))
        LOG.info("FYERS symbol master written: %s", info)
    m = instruments.load(path, cfg["underlyings"], list(cfg["index_names"].values()))
    if {"equity", "etf"} & set(cfg["instruments_enabled"]):
        try:
            m.bind_universe(instruments.load_universe(cfg["equity_universe_file"]))
        except DataError as e:
            # stocks/ETFs are then unavailable this session (no signals), never filled from SAHI
            LOG.warning("stocks/ETFs unavailable on FYERS this session: %s", e)
            print("stocks/ETFs unavailable on FYERS this session: %s" % e, file=sys.stderr)
    md = fyers.FyersReadOnly(per_minute=getattr(a, "max_calls_per_minute", 20),
                             per_day=getattr(a, "max_calls_per_day", 2400),
                             per_second=getattr(a, "max_calls_per_second", 5)).bind(m, fyers.load_symbols(path))
    print("market data: FYERS (read-only), %d instruments" % len(md.symbols))
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


def _hhmm(v):
    try:
        dt.datetime.strptime(v, "%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError("expected HH:MM, got %r" % v)
    return v


def _should_stop(a, cal, now):
    """Scheduled runs (for example GitHub Actions) end at --until or on a non-trading day."""
    if not cal.is_trading_day(now.date()):
        print("%s is not an NSE trading day; exiting" % now.date().isoformat())
        return True
    if a.until and now >= clock.at(now.date(), a.until):
        print("reached --until %s IST; exiting (state is saved after every cycle)" % a.until)
        return True
    return False


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
    first = True
    while True:
        now = clock.now_ist()
        if _should_stop(a, cal, now):
            _dashboard(cfg, now, "stopped", master=m)
            return
        nt = Notifier(cfg["state_dir"], now.date(), cfg)
        if first:
            print("P&L notifications: %s" % nt.channel)
            first = False
        book = risk.open_for(cfg, now.date())
        al = Alerts(cfg["state_dir"], now.date())
        ctx = bot.Context(cfg, cal, m, md, cm, broker, book, al, strats, evs, digest, nt)
        err = None
        try:
            res = bot.run_once(ctx, now)
            LOG.info(json.dumps(res, default=str)[:4000])
        except NseBotError as e:
            err = str(e)
            book.record_error(cfg, now)
            al.emit("system_error", "loop-%s" % now.strftime("%H%M%S"), str(e), now)
        closing = not cal.status(now)["open"] and now.time() > clock.SESSION_CLOSE
        _dashboard(cfg, now, "error" if err else "stopped" if (a.once or closing) else "running",
                   ctx.marks, err, m)
        if a.once:
            return
        if closing:
            print("session closed; exiting (the supervisor restarts the bot next morning)")
            return
        time.sleep(a.every)


def _dashboard(cfg, now, status, marks=None, error=None, master=None):
    """Heartbeat + dashboard/data.json. Reporting only: a failure here is logged and never stops trading."""
    try:
        dashboard.write_heartbeat(cfg["state_dir"], now, status, marks, error, master)
        dashboard.write(cfg["state_dir"], cfg, now)
    except Exception as e:                      # noqa: BLE001  (the dashboard must not affect the loop)
        LOG.error("dashboard update failed: %s", e)


def cmd_forward_report(a):
    from . import forward
    cfgs = [config.load(p) for p in [a.config] + a.also_config]
    books = [(os.path.splitext(os.path.basename(p))[0], c["state_dir"]) for p, c in zip([a.config] + a.also_config, cfgs)]
    out = a.out or os.path.join(cfgs[0]["state_dir"], "forward")
    doc, md = forward.write_report(books, cfgs[0]["capital_inr"], clock.now_ist(), out)
    print(md)
    print("written: %s" % os.path.join(out, "report.md"))


def cmd_notify_summary(a):
    from .broker.paper import Ledger
    now = clock.now_ist()
    day = dt.date.fromisoformat(a.date) if a.date else now.date()
    books = []
    for path in [a.config] + a.also_config:
        cfg = config.load(path)
        books.append((os.path.splitext(os.path.basename(path))[0], Ledger(cfg["state_dir"]).records()))
    names = {}
    if a.master:
        try:
            cfg0 = config.load(a.config)
            m = instruments.load(a.master, cfg0["underlyings"], cfg0.get("index_names", {}).values())
            for _, recs in books:
                for r in recs:
                    x = r.get("instrument_id") is not None and m.by_id(int(r["instrument_id"]))
                    if x:
                        names[str(r["instrument_id"])] = x.symbol
        except Exception as e:                  # noqa: BLE001  (names are cosmetic)
            LOG.warning("contract names unavailable: %s", e)
    first = config.load(a.config)
    Notifier(first["state_dir"], day, first).daily_summary(books, day, now, names)


def cmd_dashboard(a):
    cfg = config.load(a.config)
    doc = dashboard.write(cfg["state_dir"], cfg, _now(a))
    print(json.dumps({"bot": doc["bot"], "today": doc["today"], "totals": doc["totals"]}, indent=2))


def cmd_eod_report(a):
    from . import eod
    cfg, cal, cm = _common(a.config)
    day = dt.date.fromisoformat(a.date) if a.date else _now(a).date()
    rep = eod.build(day, cal, a.raw, a.sessions)
    path = eod.write(rep, a.out)
    print("\n".join(rep["summary"]))
    print("report: %s (no trades simulated: the free NSE files have no bid/ask)" % path)


def cmd_record(a):
    from .data import recorder
    cfg, cal, cm, m, md = _sahi_ctx(a)
    if a.sweep:
        a.interval = 1
    step = dt.timedelta(minutes=a.interval)
    print("recording %d-minute %s to %s (Ctrl-C to stop)" % (
        a.interval, "index/future bars + option snapshots" if a.sweep else "bars", a.out))
    while True:
        now = clock.now_ist()
        if _should_stop(a, cal, now):
            return
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
                if a.sweep:
                    n = recorder.record_sweep_minute(md, m, cfg, cal, bar_start, a.out)
                else:
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
    if a.kind == "sweep":
        doc = recorder.build_sweep(a.recordings, a.out, a.name, a.underlying)
        print(json.dumps(doc, indent=2))
        return
    fn = recorder.build_equity if a.kind == "equity" else recorder.build
    doc = fn(a.recordings, a.out, a.name, a.interval)
    print(json.dumps(doc, indent=2))


def cmd_fyers_record(a):
    """Read-only FYERS options recorder (data/fyers_recorder.py). Never places an order."""
    cfg, cal, cm = _common(a.config)
    if cfg["data_source"] != "fyers":
        raise ConfigError("fyers-record needs data_source 'fyers' (sources are never mixed)")
    if cfg.get("live_orders_enabled") is not False:
        raise ConfigError("live_orders_enabled must stay false")
    a.max_calls_per_minute = a.max_calls_per_minute or 4
    cfg, cal, cm, m, md = _fyers_ctx(a, cfg, cal, cm)
    from .data import fyers_recorder
    rec = fyers_recorder.Recorder(md, m, cal, cfg, a.out, log=lambda x: print(x, flush=True))
    res = rec.run(a.until or "15:31", smoke=a.smoke)
    day = clock.now_ist().date()
    summary = rec._summary(day) if os.path.isdir(os.path.join(a.out, day.isoformat())) else None
    print(json.dumps({"result": res, "summary": summary}, indent=2, default=str))
    return 0


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
        s.add_argument("--max-calls-per-minute", type=int, default=20,
                       help="FYERS only: this process's share of the 50/minute data limit")
        s.add_argument("--max-calls-per-second", type=int, default=5,
                       help="FYERS only: this process's share of the 5/second data limit")
        s.add_argument("--max-calls-per-day", type=int, default=2400,
                       help="FYERS only: this process's share of the 5,000/day data limit")
        if name == "paper":
            s.add_argument("--strategy", required=True, action="append",
                           help="repeat to run several strategies; tried in the order given")
            s.add_argument("--dataset", action="append", help="dataset each strategy was approved on, same order")
            s.add_argument("--every", type=int, default=15)
            s.add_argument("--once", action="store_true")
            s.add_argument("--sweep", action="store_true",
                           help="record 1m index + current-month future bars and option snapshots (sweep_reversal data)")
        if name in ("paper", "record"):
            s.add_argument("--until", type=_hhmm, help="IST time HH:MM at which to exit (for scheduled jobs)")
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
            s.add_argument("--sweep", action="store_true",
                           help="record 1m index + current-month future bars and option snapshots (sweep_reversal data)")
        s.set_defaults(fn=fn)
    s = sub.add_parser("fyers-record", help="record NIFTY ATM + 2 ITM CE/PE quotes from FYERS every minute "
                                             "(read-only)")
    s.add_argument("--master", default="state/instruments-fyers.csv")
    s.add_argument("--refresh-master", action="store_true")
    s.add_argument("--out", default="state/fyers-recordings")
    s.add_argument("--until", type=_hhmm, help="IST HH:MM to stop (default 15:31)")
    s.add_argument("--smoke", type=int, default=0,
                   help="record N snapshots right away, whatever the market state (connectivity test)")
    s.add_argument("--max-calls-per-minute", type=int, default=4)
    s.add_argument("--max-calls-per-second", type=int, default=2)
    s.add_argument("--max-calls-per-day", type=int, default=450,
                   help="this process's share of FYERS's 5,000/day (the paper bots use 2,250 each)")
    s.set_defaults(fn=cmd_fyers_record)
    s = sub.add_parser("eod-report", help="daily NIFTY report from NSE's free end-of-day files (no login, no trades)")
    s.add_argument("--date", help="trading day YYYY-MM-DD (default: today IST)")
    s.add_argument("--out", default="state/eod"); s.add_argument("--raw", default="state/eod-raw")
    s.add_argument("--sessions", type=int, default=40, help="daily bars of history for the trend")
    s.set_defaults(fn=cmd_eod_report)
    s = sub.add_parser("build-dataset")
    s.add_argument("--recordings", default="state/recordings"); s.add_argument("--out", required=True)
    s.add_argument("--name", required=True); s.add_argument("--interval", type=int, default=5)
    s.add_argument("--kind", choices=("options", "equity", "sweep"), default="options")
    s.add_argument("--underlying", default="NIFTY", help="for --kind sweep")
    s.set_defaults(fn=cmd_build_dataset)
    sub.add_parser("risk-status").set_defaults(fn=cmd_risk_status)
    s = sub.add_parser("forward-report", help="forward paper validation statistics (ORB and Sweep separately)")
    s.add_argument("--also-config", action="append", default=[], help="another book's config to include")
    s.add_argument("--out", help="output folder (default <state_dir>/forward)")
    s.set_defaults(fn=cmd_forward_report)
    s = sub.add_parser("notify-summary", help="send the daily paper P&L summary (reporting only)")
    s.add_argument("--also-config", action="append", default=[], help="another book's config to include")
    s.add_argument("--date", help="YYYY-MM-DD (default today IST)")
    s.add_argument("--master", help="symbol master CSV, for contract names")
    s.set_defaults(fn=cmd_notify_summary)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)
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
