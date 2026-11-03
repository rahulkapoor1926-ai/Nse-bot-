"""FYERS live quote stream for the dashboard (read-only, display only; never used for orders).

Uses the official FYERS SDK data socket (fyers-apiv3, FyersDataSocket, "SymbolUpdate"): the socket
address comes from that SDK, it is not written here. Symbols = NIFTY spot plus the ATM/ITM CE/PE
contracts that the 1-minute FYERS recorder currently tracks (re-read every minute, so the set follows
the ATM strike).

Every tick is kept per symbol with the exchange feed time (exch_feed_time / last_traded_time) and
the time this process received it. The latest state is written atomically to
<out>/<day>/live_quotes.json about once a second. The dashboard reads it and marks each quote stale
by its own age; it never fills a gap with an estimate.

The paper bots do NOT read this file: their position management and P&L stay on their own
REST quotes, unchanged.
"""
import csv
import datetime as dt
import json
import os
import threading
import time

from . import clock

FILE = "live_quotes.json"
RECORD_FILE = "fyers_options_1m.csv"
SPOT = "NSE:NIFTY50-INDEX"
KEEP = ("ltp", "bid_price", "ask_price", "bid_size", "ask_size", "vol_traded_today", "OI",
        "last_traded_time", "exch_feed_time", "open_price", "high_price", "low_price", "prev_close_price")


def recorder_symbols(recordings, day):
    """Symbols of the recorder's latest 1-minute snapshot (index first), or [SPOT] when none yet."""
    path = os.path.join(recordings, day.isoformat(), RECORD_FILE)
    if not os.path.exists(path):
        return [SPOT]
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return [SPOT]
    last = rows[-1]["ts"]
    syms = [r["symbol"] for r in rows if r["ts"] == last and r.get("symbol")]
    return [SPOT] + [s for s in syms if s != SPOT]


class TickStore:
    """Thread-safe latest tick per symbol."""

    def __init__(self, now=clock.now_ist):
        self.now = now
        self.lock = threading.Lock()
        self.quotes = {}
        self.events = []          # connect / error / close, newest last (no token, no secrets)
        self.connected = False
        self.ticks = 0

    def on_message(self, msg):
        if not isinstance(msg, dict):
            return
        sym = msg.get("symbol")
        if not sym or ("ltp" not in msg and "bid_price" not in msg):
            if msg.get("type") in ("cn", "ful", "sub", "unsub") or msg.get("s"):
                self.event("message", {k: msg.get(k) for k in ("type", "code", "message", "s") if k in msg})
            return
        with self.lock:
            q = self.quotes.setdefault(sym, {})
            for k in KEEP:
                if msg.get(k) is not None:
                    q[k] = msg[k]
            q["received_at"] = self.now().isoformat(timespec="seconds")
            self.ticks += 1

    def event(self, kind, detail=None):
        with self.lock:
            self.events = (self.events + [{"time": self.now().isoformat(timespec="seconds"), "kind": kind,
                                           "detail": detail}])[-20:]
            if kind == "connected":
                self.connected = True
            elif kind in ("closed", "error"):
                self.connected = kind != "closed" and self.connected

    def doc(self, subscribed):
        with self.lock:
            return {"source": "FYERS WebSocket (official SDK FyersDataSocket, SymbolUpdate)",
                    "written_at": self.now().isoformat(timespec="seconds"), "connected": self.connected,
                    "ticks_received": self.ticks, "subscribed": list(subscribed),
                    "quotes": json.loads(json.dumps(self.quotes)), "events": list(self.events)}


def write_atomic(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def run(recordings, out, until, socket_factory, now=clock.now_ist, sleep=time.sleep, log=print):
    """Stream until `until` (IST datetime). socket_factory(store) returns an object with connect(),
    subscribe(symbols=[...], data_type=...), unsubscribe(symbols=[...]) and close_connection()."""
    store = TickStore(now)
    sock = socket_factory(store)
    sock.connect()
    subscribed, last_pick = [], 0.0
    path = os.path.join(out, now().date().isoformat(), FILE)
    while now() < until:
        t = time.monotonic()
        if t - last_pick >= 60 or not subscribed:
            want = recorder_symbols(recordings, now().date())
            new = [s for s in want if s not in subscribed]
            old = [s for s in subscribed if s not in want and s != SPOT]
            if old:
                sock.unsubscribe(symbols=old, data_type="SymbolUpdate")
            if new:
                sock.subscribe(symbols=new, data_type="SymbolUpdate")
                log("live feed: subscribed %d symbol(s), now %d" % (len(new), len(want)))
            subscribed, last_pick = want, t
        write_atomic(path, store.doc(subscribed))
        sleep(1)
    try:
        sock.close_connection()
    except Exception:                                   # noqa: BLE001  closing is best effort
        pass
    store.event("closed", "session end")
    write_atomic(path, store.doc(subscribed))
    return store


def sdk_socket(store, log_dir):
    """The official FYERS SDK data socket. Credentials come only from the environment."""
    from fyers_apiv3.FyersWebsocket import data_ws
    app, token = os.environ.get("FYERS_APP_ID", "").strip(), os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
    if not app or not token:
        raise SystemExit("live feed: FYERS_APP_ID / FYERS_ACCESS_TOKEN not set in the environment")
    os.makedirs(log_dir, exist_ok=True)

    def err(m):
        store.event("error", {k: m.get(k) for k in ("code", "message", "s")} if isinstance(m, dict) else str(m)[:200])
    return data_ws.FyersDataSocket(
        access_token="%s:%s" % (app, token), log_path=log_dir.rstrip("/") + "/", litemode=False,
        write_to_file=False, reconnect=True, on_message=store.on_message, on_error=err,
        on_connect=lambda: store.event("connected"), on_close=lambda m: store.event("closed", str(m)[:200]))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--recordings", default="state/fyers-recordings")
    ap.add_argument("--out", default="state/live-feed")
    ap.add_argument("--until", default="15:31")
    a = ap.parse_args(argv)
    until = clock.at(clock.now_ist().date(), a.until)
    run(a.recordings, a.out, until, lambda s: sdk_socket(s, os.path.join(a.out, "sdk-log")))


if __name__ == "__main__":
    main()
