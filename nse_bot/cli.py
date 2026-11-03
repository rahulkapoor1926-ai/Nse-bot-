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
import collections
import datetime as dt
import json
import logging
import os
import sys
import time

from . import (analyst, clock, config, costs as costs_mod, dashboard, events as events_mod, gate, instruments,
               news, pelosi, report, risk, state)
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
    from . import scope
    strats = [_strategy(p) for p in a.strategy]
    bad = [s.get("name") for s in strats if not scope.strategy_allowed(s)]
    if bad:                                  # checked before any data connection is made
        raise ConfigError("refusing to run %s: only NIFTY index-option strategies may run (equity/ETF disabled)"
                          % ", ".join(map(str, bad)))
    cfg, cal, cm, m, md = _sahi_ctx(a)
    evs = events_mod.load(cfg["events_file"])
    datasets = a.dataset or []
    if datasets and len(datasets) != len(strats):
        raise ConfigError("give one --dataset per --strategy (same order), or none")
    digest = {s["name"]: _load_ds(d).digest for s, d in zip(strats, datasets)} or None
    broker = PaperBroker(cfg["state_dir"], cm, cfg["slippage_ticks"])
    broker.ledger.verify()
    from . import control
    controls = control.Poller.from_env()      # dashboard pause / emergency halt (entries only)
    print(report.BANNER)
    print("dashboard controls: %s" % ("on" if controls else "not configured"))
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
        if controls is not None:
            used = controls.apply(book, now)
            if used.get("entries_paused") or used.get("halt_day") == now.date().isoformat() or not used.get("read_ok"):
                LOG.info("dashboard controls: %s", json.dumps(used, default=str))
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


def _option_audit(md, signals, strategy, cal, limit):
    """For each ORB signal: the contract the bot would have aimed at and FYERS's actual answer for its
    1-minute history that day. Read-only GET /history; nothing is filled or priced."""
    from .backtest import signal_study as SS
    rows, n = [], 0
    for s in sorted(signals, key=lambda x: x["day"], reverse=True):        # newest first: most likely to exist
        k = SS._atm(s["trigger_close"]) + 50 * int(strategy.get("strike_offset", 0)) * (1 if s["option_type"] == "CE" else -1)
        c = SS.option_symbol(dt.date.fromisoformat(s["day"]), s["option_type"], k, cal)
        row = dict(c, day=s["day"], signal_time=s["signal_time"])
        if n >= limit:
            rows.append(dict(row, fyers="not requested (audit limit %d)" % limit, candles_1m=None))
            continue
        n += 1
        fake_id = -n
        md.symbols[fake_id] = c["symbol"]
        day = dt.date.fromisoformat(s["day"])
        try:
            got = md.candles(fake_id, "1m", clock.at(day, "09:15"), clock.at(day, "15:30"))
            row.update(fyers="served %d one-minute LTP candles (no bid/ask in /history)" % len(got),
                       candles_1m=len(got))
        except NseBotError as e:
            row.update(fyers="refused: %s" % str(e)[:120], candles_1m=0)
        finally:
            md.symbols.pop(fake_id, None)
        rows.append(row)
    served = sum(1 for r in rows if r.get("candles_1m"))
    return {"requested": n, "contracts_with_1m_ltp": served,
            "contracts_with_historical_bid_ask": 0,
            "note": ("FYERS /history returns OHLC of traded prices (LTP) only, never bid/ask; an option backtest "
                     "needs executable bid/ask, so no contract here yields a valid option trade."),
            "rows": rows}


def cmd_signal_study(a):
    """1-year ORB / Sweep SIGNAL study on real FYERS NIFTY 1m index + future bars (no option fills, no P&L)."""
    from .backtest import signal_study as SS
    cfg, cal, cm = _common(a.config)
    if cfg["data_source"] != "fyers":
        raise ConfigError("signal-study reads FYERS history: data_source must be 'fyers'")
    a.max_calls_per_minute = a.max_calls_per_minute or 20
    cfg, cal, cm, m, md = _fyers_ctx(a, cfg, cal, cm)
    end = clock.now_ist()
    start = clock.at((end - dt.timedelta(days=a.days)).date(), "09:15")
    audit = []

    def fetch(iid_, size, frm, to, label):
        """90-day requests, each logged with FYERS's answer; a refused window is reported, never filled."""
        bars = []

        def window(t0, t1):
            try:
                got = md.candles(iid_, size, t0, t1)
                audit.append({"series": label, "size": size, "from": t0.date().isoformat(),
                              "to": t1.date().isoformat(), "bars": len(got), "result": "ok"})
                bars.extend(got)
                return True
            except NseBotError as e:
                if "calls for the day" in str(e) or t1 - t0 <= dt.timedelta(days=2):
                    audit.append({"series": label, "size": size, "from": t0.date().isoformat(),
                                  "to": t1.date().isoformat(), "bars": 0, "result": str(e)[:160]})
                    return False
                # Refused (FYERS keeps a limited intraday history): the later half may still be served.
                # If it is, the boundary lies in the earlier half; if not, the earlier half is older still.
                mid = t0 + (t1 - t0) / 2
                if window(mid, t1):
                    window(t0, mid)
                else:
                    audit.append({"series": label, "size": size, "from": t0.date().isoformat(),
                                  "to": mid.date().isoformat(), "bars": 0,
                                  "result": "not requested: a later window was refused too (%s)" % str(e)[:100]})
                return False
        t0 = frm
        while t0 < to:
            t1 = min(to, t0 + dt.timedelta(days=90))
            window(t0, t1)
            t0 = t1
        mins = {"1m": 1, "5m": 5}[size]
        seen, out = set(), []
        for b in sorted(bars, key=lambda b: b[0]):
            if b[0] not in seen and b[0] + dt.timedelta(minutes=mins) <= end:
                seen.add(b[0])
                out.append(b)
        return out
    iid = m.index_id(cfg["index_names"]["NIFTY"])
    first_day = (end - dt.timedelta(days=max(a.days, a.older_days or 0))).date()
    daily = md.candles(iid, "1d", clock.at(dt.date(first_day.year, 1, 1), "00:00"), end)
    audit.append({"series": "NIFTY index", "size": "1d", "from": dt.date(first_day.year, 1, 1).isoformat(),
                  "to": end.date().isoformat(), "bars": len(daily), "result": "ok"})
    u1 = fetch(iid, "1m", start, end, "NIFTY index")
    fut = m.current_future("NIFTY", end.date())
    f1, fut_note = [], None
    if fut is None:
        fut_note = "no current NIFTY future in the symbol master"
    else:
        f1 = fetch(fut.instrument_id, "1m", start, end, "NIFTY future %s" % md.symbols.get(fut.instrument_id))
        if not f1:
            fut_note = "FYERS served no 1-minute future bars for this window"
    # Older than the 1-minute history: FYERS's own 5-minute bars, if it serves them (ORB runs on 5m bars).
    u5 = []
    first_1m = clock.at(u1[0][0].date(), "00:00") if u1 else end
    older = clock.at((end - dt.timedelta(days=a.older_days)).date(), "09:15") if a.older_days else None
    if older and older < first_1m:
        u5 = fetch(iid, "5m", older, first_1m, "NIFTY index (older, 5m)")
    per_day = collections.Counter(b[0].date() for b in u1)
    for b in u5:                                   # a 5-minute bar stands for 5 one-minute bars
        per_day[b[0].date()] += 5
    with open(cfg["holidays_file"], encoding="utf-8") as fh:
        file_years = json.load(fh)["years"]
    cal, cal_added = SS.observed_calendar(file_years, [b[0].date() for b in daily], per_day, end.date())
    strategies = {name: _strategy(path) for name, path in (("orb", a.orb_strategy), ("sweep", a.sweep_strategy))}

    def cover(bars):
        d = sorted({b[0].date() for b in bars})
        return {"bars": len(bars), "sessions": len(d), "first": d[0].isoformat() if d else None,
                "last": d[-1].isoformat() if d else None}
    doc = {"generated": end.isoformat(), "source": "FYERS /history, read-only", "note": SS.NOT_SIMULATED,
           "requested": {"from": (older or start).date().isoformat(), "to": end.date().isoformat()},
           "coverage": {"index": cover(u1), "index_5m_older": cover(u5),
                        "future": dict(cover(f1), symbol=md.symbols.get(fut.instrument_id) if fut else None,
                                       note=fut_note)},
           "fetch_audit": audit, "options_pnl": SS.OPTIONS_PNL,
           "calendar_added": cal_added, "index_daily": cover(daily)}
    o = SS.orb(u1, strategies["orb"], cfg, cal)
    if u5:
        o5 = SS.orb(u5, strategies["orb"], cfg, cal, native_minutes=5)
        o = {"signals": o5["signals"] + o["signals"], "sessions": o5["sessions"] + o["sessions"],
             "skipped": dict(collections.Counter(o5["skipped"]) + collections.Counter(o["skipped"])),
             "sessions_by_data": {"5m": o5["sessions"], "1m": o["sessions"]}}
    doc["ORB"] = dict(o, summary=SS.summarise(o["signals"], o["sessions"]))
    if f1:
        s = SS.sweep_reversal(u1, f1, strategies["sweep"], cfg, cal)
        doc["Sweep"] = dict(s, summary=SS.summarise(s["signals"], s["sessions"]))
    else:
        doc["Sweep"] = {"signals": [], "summary": None, "not_evaluable": {"no index-future 1m history": cover(u1)["sessions"]}}
    oc = SS.index_outcomes(u1 + u5, doc["ORB"]["signals"], strategies["orb"], cfg, cal)
    doc["ORB_index_outcomes"] = {"summary": SS.summarise_outcomes(oc), "rows": oc,
                                 "model": SS.index_model(oc, cfg, label="RAW SIGNAL OUTCOMES (no portfolio limits)"),
                                 "portfolio": SS.portfolio(oc, cfg), "daily_limits": SS.daily_limit_check(oc, cfg)}
    try:
        from . import scanner
        ex = scanner.choose_expiry(m, cal, "NIFTY", end.date(), cfg["min_days_to_expiry"])
        atm = m.pick("NIFTY", ex, "CE", u1[-1][4], 0)
        doc["ORB_index_outcomes"]["risk_fit"] = SS.per_trade_risk_fit(cfg, cm, atm, end.date(),
                                                                      strategies["orb"]["stop_pct"])
    except Exception as e:                                                  # noqa: BLE001
        doc["ORB_index_outcomes"]["risk_fit"] = {"error": str(e)[:160]}
    doc["option_audit"] = _option_audit(md, doc["ORB"]["signals"], strategies["orb"], cal, a.option_audit)
    if not a.option_audit and a.reuse_option_audit and os.path.exists(a.reuse_option_audit):
        try:
            with open(a.reuse_option_audit, encoding="utf-8") as fh:
                prev = json.load(fh).get("option_audit")
            if prev and prev.get("requested"):
                doc["option_audit"] = dict(prev, reused_from=json.load(open(a.reuse_option_audit)).get("generated"))
        except (OSError, ValueError):
            pass
    doc["charts"] = SS.charts(u1, doc["ORB"]["signals"] + doc["Sweep"]["signals"])
    doc["charts"].update(SS.charts(u5, doc["ORB"]["signals"], native_minutes=5))
    doc["api_calls"] = md.calls_made
    os.makedirs(a.out, exist_ok=True)
    state.write_json(os.path.join(a.out, "study.json"), doc)
    oa = doc["option_audit"]
    print("OPTION_AUDIT %s" % json.dumps({k: oa[k] for k in ("requested", "contracts_with_1m_ltp",
                                                            "contracts_with_historical_bid_ask")}))
    for r in oa["rows"][:300]:
        print("  %s %s %s exp %s (%s): %s" % (r["day"], r["option_type"], r["symbol"], r["expiry"], r["expiry_kind"], r["fyers"]))
    io = doc["ORB_index_outcomes"]
    print("INDEX_OUTCOMES %s" % json.dumps(io["summary"], default=str))
    print("INDEX_MODEL %s" % json.dumps({k: v for k, v in io["model"].items() if k != "equity_curve_r"}, default=str))
    print("PORTFOLIO %s" % json.dumps({k: v for k, v in io["portfolio"].items() if k != "rejected_rows"} | {
        "model": {k: v for k, v in io["portfolio"]["model"].items() if k not in ("equity_curve_r", "by_month")}}, default=str))
    print("DAILY_LIMITS %s" % json.dumps(io["daily_limits"]))
    print("RISK_FIT %s" % json.dumps(io.get("risk_fit"), default=str))
    brief = {k: doc[k] for k in ("requested", "index_daily", "calendar_added", "coverage", "api_calls", "note", "options_pnl", "fetch_audit")}
    for fam in ("ORB", "Sweep"):
        x = doc[fam]
        brief[fam] = {"summary": x.get("summary"), "not_evaluable": x.get("not_evaluable"),
                      "funnel_last_failed_rule": x.get("funnel_last_failed_rule"),
                      "furthest_rule_failed": x.get("furthest_rule_failed"), "skipped": x.get("skipped")}
    print(json.dumps(brief, indent=2, default=str))
    return 0



def cmd_day_audit(a):
    """Why a day had (or had no) paper trade: replay the day's real FYERS 1-minute NIFTY bars through the
    unchanged ORB and Sweep rules, and for an ORB setup look up the real option premium in its entry window
    and whether one lot fits the Rs 500 risk rule. Read-only: nothing is entered, filled or changed."""
    from . import budget, patterns, scanner, structure as st, sweep
    from .backtest import signal_study as SS
    cfg, cal, cm = _common(a.config)
    if cfg["data_source"] != "fyers":
        raise ConfigError("day-audit reads FYERS history: data_source must be 'fyers'")
    cfg, cal, cm, m, md = _fyers_ctx(a, cfg, cal, cm)
    now = clock.now_ist()
    day = dt.date.fromisoformat(a.date) if a.date else now.date()
    end = min(now, clock.at(day, "15:30"))
    iid = m.index_id(cfg["index_names"]["NIFTY"])
    daily = md.candles(iid, "1d", clock.at(dt.date(day.year, 1, 1), "00:00"), end)
    lo = clock.at(day - dt.timedelta(days=14), "09:15")
    done = lambda bars: [b for b in bars if b[0] + dt.timedelta(minutes=1) <= end]     # noqa: E731
    u1 = done(md.candles(iid, "1m", lo, end))
    fut = m.current_future("NIFTY", day)
    f1 = done(md.candles(fut.instrument_id, "1m", lo, end)) if fut else []
    with open(cfg["holidays_file"], encoding="utf-8") as fh:
        file_years = json.load(fh)["years"]
    per_day = collections.Counter(b[0].date() for b in u1)
    cal, _ = SS.observed_calendar(file_years, [b[0].date() for b in daily], per_day, day)
    today = [b for b in u1 if b[0].date() == day]
    orb_s, sw_s = _strategy(a.orb_strategy), _strategy(a.sweep_strategy)
    doc = {"day": day.isoformat(), "generated": now.isoformat(timespec="seconds"),
           "source": "FYERS /history 1-minute bars, read-only; rules unchanged",
           "data": {"index_1m_bars_today": len(today),
                    "first_bar": today[0][0].isoformat() if today else None,
                    "last_bar": today[-1][0].isoformat() if today else None,
                    "future": md.symbols.get(fut.instrument_id) if fut else None,
                    "future_1m_bars_today": sum(1 for b in f1 if b[0].date() == day)}}
    # ORB: the opening range, the first breakout, its entry window, and the option in that window
    bars5 = st.resample(today, orb_s["interval_minutes"])
    rng = patterns.opening_range(bars5, orb_s["params"]["or_minutes"], orb_s["interval_minutes"])
    buf = orb_s["params"].get("buffer_pct", 0.0)
    o = {"rules": {"or_minutes": orb_s["params"]["or_minutes"], "buffer_pct": buf,
                   "interval_minutes": orb_s["interval_minutes"], "no_entries_before": cfg["no_entries_before"],
                   "no_entries_after": cfg["no_entries_after"], "max_signals_per_day": orb_s.get("max_signals_per_day")},
         "opening_range": None if rng is None else {"high": rng[0], "low": rng[1],
                                                    "ce_trigger_close_above": round(rng[0] * (1 + buf / 100.0), 2),
                                                    "pe_trigger_close_below": round(rng[1] * (1 - buf / 100.0), 2)}}
    sigs = [x for x in SS.orb(u1, orb_s, cfg, cal)["signals"] if x["day"] == day.isoformat()]
    o["signals"] = sigs
    if not sigs:
        o["verdict"] = "NO VALID SETUP" if today else "DATA FAILURE"
    for sg in sigs:
        try:
            ex = scanner.choose_expiry(m, cal, "NIFTY", day, cfg["min_days_to_expiry"])
            c = m.pick("NIFTY", ex, sg["option_type"], sg["trigger_close"], int(orb_s.get("strike_offset", 0)))
            sym = md.symbols.get(c.instrument_id)
            w0 = dt.datetime.fromisoformat(sg["entry_window"][0])
            ob = md.candles(c.instrument_id, "1m", clock.at(day, "09:15"), end)
            at = next((b for b in ob if b[0] >= w0), None)
            sg["contract"] = {"symbol": sym, "expiry": ex.isoformat(), "lot_size": c.lot_size,
                              "strike_offset": int(orb_s.get("strike_offset", 0))}
            if at is None:
                sg["entry_window_premium"] = {"status": "NO DATA", "why": "FYERS served no option bar in the window"}
                continue
            ltp = at[1]
            stop = round(ltp * (1 - orb_s["stop_pct"] / 100.0), 2)
            sz = budget.size_at_fills(cfg, cm, c, ltp, stop, day, cfg["max_daily_loss_inr"])
            sg["entry_window_premium"] = {
                "bar": at[0].isoformat(), "open_ltp": ltp, "high": at[2], "low": at[3], "close": at[4],
                "note": "LTP of the first 1-minute option bar in the entry window (FYERS /history has no bid/ask; "
                        "the bot enters on the live ask, so this is an approximation used ONLY for the risk check)",
                "stop_20pct": stop, "lots_under_rs500_rule": sz["lots"], "sizing": sz}
        except NseBotError as e:
            sg["entry_window_premium"] = {"status": "ERROR", "why": str(e)[:200]}
    doc["ORB"] = o
    sw = SS.sweep_reversal(u1, f1, sw_s, cfg, cal) if f1 else {"signals": [], "day_audit": []}
    doc["Sweep"] = {"signals": [x for x in sw["signals"] if x["day"] == day.isoformat()],
                    "day_audit": [x for x in sw.get("day_audit") or [] if x["day"] == day.isoformat()],
                    "rule_order": list(sweep.RULES),
                    "not_evaluable": sw.get("not_evaluable") if not f1 else None}
    doc["api_calls"] = md.calls_made
    os.makedirs(a.out, exist_ok=True)
    state.write_json(os.path.join(a.out, "%s.json" % day.isoformat()), doc)
    print("DAY_AUDIT %s" % json.dumps(doc, default=str))
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
    s = sub.add_parser("signal-study", help="1-year ORB/Sweep signal study on real FYERS NIFTY 1m bars "
                                             "(no option fills, no P&L)")
    s.add_argument("--master", default="state/instruments-fyers.csv")
    s.add_argument("--refresh-master", action="store_true")
    s.add_argument("--out", default="state/signal-study")
    s.add_argument("--days", type=int, default=365)
    s.add_argument("--older-days", type=int, default=0,
                   help="also try FYERS 5-minute index bars back this many days, before the 1-minute history (ORB only)")
    s.add_argument("--option-audit", type=int, default=0,
                   help="check FYERS history for the option contract of up to this many ORB signals (one call each)")
    s.add_argument("--reuse-option-audit", help="keep the option contract check from this earlier study.json "
                                                "when --option-audit is 0 (saves FYERS calls)")
    s.add_argument("--orb-strategy", default="config/strategies/orb15.example.json")
    s.add_argument("--sweep-strategy", default="config/strategies/sweep.example.json")
    s.add_argument("--max-calls-per-minute", type=int, default=20)
    s.add_argument("--max-calls-per-second", type=int, default=2)
    s.add_argument("--max-calls-per-day", type=int, default=60)
    s.set_defaults(fn=cmd_signal_study)
    s = sub.add_parser("day-audit", help="replay one day's real FYERS bars through the unchanged ORB/Sweep rules "
                                          "(read-only; why there was or was no paper trade)")
    s.add_argument("--date"); s.add_argument("--out", default="state/day-audit")
    s.add_argument("--master", default="state/instruments-fyers.csv"); s.add_argument("--refresh-master", action="store_true")
    s.add_argument("--orb-strategy", default="config/strategies/orb15.example.json")
    s.add_argument("--sweep-strategy", default="config/strategies/sweep.example.json")
    s.add_argument("--max-calls-per-minute", type=int, default=10)
    s.add_argument("--max-calls-per-second", type=int, default=2)
    s.add_argument("--max-calls-per-day", type=int, default=20)
    s.set_defaults(fn=cmd_day_audit)
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
