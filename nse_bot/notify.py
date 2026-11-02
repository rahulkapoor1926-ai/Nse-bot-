"""P&L notifications for PAPER trades: opened, live P&L, closed and a daily summary.

Reporting only. Nothing here places, changes or blocks an order, and every public
method swallows its own errors: a failed notification can never affect trading.

Prices come only from what the bot already verified:
  - opened / closed: the paper fill recorded in the hash-chained ledger
    (built from a FYERS bid/ask that passed the freshness checks);
  - live updates: the bid that exit.evaluate accepted (fresh, timestamped, from the
    session's data source). If that bid is stale or missing, the update says so and
    shows NO P&L. Nothing is estimated except the exit charges, which are labelled.

Channel: a private Telegram bot (free; the Telegram app pushes to an iPhone). It is used
only when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in the environment (GitHub
Actions secrets). The token is never printed or logged. Without them, or if a send fails,
messages still go to stdout and <state_dir>/notify/notify.jsonl.

Throttle state (last update per position) is kept in <state_dir>/notify/<day>.json so a
restart does not resend everything.
"""

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.request

from . import clock, state

LOG = logging.getLogger("nse_bot.notify")
TELEGRAM = "https://api.telegram.org"
TRAILING = "none (both strategies use a fixed stop)"
MAX_FAILURES = 3


def telegram_transport(token, chat_id, text, timeout=10):
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request("%s/bot%s/sendMessage" % (TELEGRAM, token), data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read().decode() or "{}")
    if not doc.get("ok"):
        raise RuntimeError("telegram refused the message: %s" % str(doc.get("description"))[:120])


def _iso(s):
    return clock.to_ist(dt.datetime.fromisoformat(s))


def _rs(x):
    return "Rs %s%s" % ("-" if x < 0 else "", "{:,.2f}".format(abs(x)))


def _signed(x):
    return ("+" if x >= 0 else "-") + _rs(abs(x))


def _contract(c):
    if getattr(c, "option_type", None):
        return "%s %s %.0f %s, expiry %s" % (c.symbol, c.underlying, c.strike, c.option_type, c.expiry.isoformat())
    return "%s (%s)" % (c.symbol, getattr(c, "kind", "equity"))


def _hhmmss(t):
    return clock.to_ist(t).strftime("%H:%M:%S IST") if t else "no timestamp"


def _costs(parts, share=1.0):
    keys = ("brokerage", "stt", "exchange", "sebi", "ipft", "stamp", "dp", "gst")
    return ", ".join("%s %.2f" % (k, parts.get(k, 0.0) * share) for k in keys if parts.get(k))


class Notifier(object):
    def __init__(self, state_dir, day, cfg=None, env=os.environ, transport=telegram_transport, out=print):
        cfg = cfg or {}
        self.dir = os.path.join(state_dir, "notify")
        self.path = os.path.join(self.dir, "%s.json" % day.isoformat())
        self.every = int(cfg.get("notify_update_minutes", 5)) * 60
        self.source = cfg.get("data_source") or "unknown"
        self.label = cfg.get("notify_label") or ""    # e.g. a rehearsal banner on every message
        self.token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
        self.transport, self.out = transport, out
        try:
            self.st = state.read_json(self.path, missing={}) or {}
        except Exception:                       # noqa: BLE001  (resending is safer than silence)
            self.st = {}
        self.st.setdefault("last", {})
        self.st.setdefault("failures", 0)

    @property
    def channel(self):
        return "telegram" if self.token and self.chat else "log only"

    # ---- delivery -------------------------------------------------------------
    def send(self, kind, text, now):
        """Never raises. Returns True when Telegram accepted the message."""
        ok = False
        err = None
        if self.label:
            text = "%s\n%s" % (self.label, text)
        try:
            if self.channel == "telegram" and self.st["failures"] < MAX_FAILURES:
                try:
                    self.transport(self.token, self.chat, text)
                    ok = True
                    self.st["failures"] = 0
                except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                    self.st["failures"] += 1
                    err = self._scrub(e)
                    LOG.error("notification not delivered (%s): %s", kind, err)
            self.out("[NOTIFY %s] %s" % (kind, text))
            state.append_jsonl(os.path.join(self.dir, "notify.jsonl"), {
                "kind": kind, "time": clock.to_ist(now).isoformat(), "text": text,
                "channel": self.channel, "delivered": ok, "error": err})
            self._save()
        except Exception as e:                  # noqa: BLE001  (notifications must not affect trading)
            LOG.error("notification failed (%s): %s", kind, self._scrub(e))
        return ok

    def _scrub(self, e):
        msg = "%s: %s" % (type(e).__name__, e)
        return msg.replace(self.token, "***") if self.token else msg

    def _save(self):
        state.write_json(self.path, self.st)

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:                  # noqa: BLE001
            LOG.error("notification skipped: %s", self._scrub(e))
            return None

    # ---- events ---------------------------------------------------------------
    def opened(self, c, res, plan, now, strategy):
        return self._guard(self._opened, c, res, plan, now, strategy)

    def _opened(self, c, res, plan, now, strategy):
        ch = res["charges"]
        lines = [
            "PAPER TRADE OPENED (simulated, no real order)",
            _contract(c),
            "Strategy: %s | data: %s" % (strategy, self.source),
            "Entry: %s x %d (filled at ask + slippage)" % (_rs(res["price"]), res["filled_qty"]),
            "Initial stop-loss: %s | Target: %s" % (_rs(plan["stop"]), _rs(plan["target"])),
            "Trailing stop: %s" % TRAILING,
            "Entry charges: %s (%s)" % (_rs(ch["total"]), _costs(ch)),
            "Time: %s" % _hhmmss(now),
        ]
        if res["status"] == "PARTIAL":
            lines.append("Partial fill: %d not filled" % res["unfilled_qty"])
        self.st["last"].pop(str(c.instrument_id), None)
        return self.send("opened", "\n".join(lines), now)

    def update(self, c, pos, quote, decision, mark, now):
        return self._guard(self._update, c, pos, quote, decision, mark, now)

    def _due(self, key, now):
        last = self.st["last"].get(key)
        if last is None:
            return True
        return (clock.to_ist(now) - _iso(last)).total_seconds() >= self.every

    def _update(self, c, pos, quote, decision, mark, now):
        iid = str(c.instrument_id)
        plan = pos["meta"]["plan"]
        verified = (decision["action"] != "NO_PRICE" and quote is not None and mark.get("bid") is not None
                    and (quote.source or "") == self.source)
        key = iid if verified else iid + ":stale"
        if not self._due(key, now):
            return None
        self.st["last"][key] = clock.to_ist(now).isoformat()
        avg = pos["cost"] / pos["qty"]
        head = ["PAPER P&L UPDATE", _contract(c), "Entry: %s x %d" % (_rs(avg), pos["qty"])]
        stops = ["Stop-loss: %s (fixed) | Target: %s" % (_rs(plan["stop"]), _rs(plan["target"])),
                 "Trailing stop: %s" % TRAILING]
        if not verified:
            why = decision["reason"] if decision["action"] == "NO_PRICE" else \
                "quote source %r is not the session source %r" % (getattr(quote, "source", None), self.source)
            return self.send("stale", "\n".join(head + [
                "PRICE NOT VERIFIED: %s" % why,
                "P&L not shown: no fresh %s price. The bot will not exit on this data and keeps checking." % self.source,
            ] + stops + ["Time: %s" % _hhmmss(now)]), now)
        paid = pos["cost"] + pos["charges"]
        u = mark["unrealized"]
        return self.send("update", "\n".join(head + [
            "Current bid: %s (%s, quote time %s)" % (_rs(mark["bid"]), self.source, _hhmmss(quote.ts)),
            "Unrealized P&L: %s (%+.1f%% of %s paid)" % (_signed(u), 100.0 * u / paid if paid else 0.0, _rs(paid)),
            "  after entry charges and estimated exit charges",
        ] + stops + ["Time: %s" % _hhmmss(now)]), now)

    def closed(self, c, before, res, entry_fill, reason, realized_now, now):
        return self._guard(self._closed, c, before, res, entry_fill, reason, realized_now, now)

    def _closed(self, c, before, res, entry_fill, reason, realized_now, now):
        q = res["filled_qty"]
        avg = before["cost"] / before["qty"]
        share = q / float(before["qty"])
        gross = (res["price"] - avg) * q
        entry_ch = before["charges"] * share
        exit_ch = res["charges"]["total"]
        entry_parts = (entry_fill or {}).get("charges") or {}
        lines = [
            "PAPER TRADE CLOSED (simulated, no real order)",
            _contract(c),
            "Reason: %s" % reason,
            "Entry: %s | Exit: %s (bid - slippage) | Qty: %d" % (_rs(avg), _rs(res["price"]), q),
            "Gross P&L: %s" % _signed(gross),
            "Entry charges: %s%s" % (_rs(entry_ch), " (%s)" % _costs(entry_parts, share) if entry_parts else ""),
            "Exit charges: %s (%s)" % (_rs(exit_ch), _costs(res["charges"])),
            "Total costs: %s (estimated from the published charge schedule)" % _rs(entry_ch + exit_ch),
            "NET P&L: %s" % _signed(realized_now),
            "Time: %s" % _hhmmss(now),
        ]
        if res["status"] == "PARTIAL":
            lines.append("PARTIAL EXIT: %d STILL OPEN" % res["unfilled_qty"])
        self.st["last"].pop(str(c.instrument_id), None)
        self.st["last"].pop(str(c.instrument_id) + ":stale", None)
        return self.send("closed", "\n".join(lines), now)

    def daily_summary(self, ledgers, day, now, names=None):
        return self._guard(self._summary, ledgers, day, now, names or {})

    def _summary(self, ledgers, day, now, names):
        trades, still_open = [], 0
        for label, records in ledgers:
            t, o = round_trips(records, day)
            trades += [dict(x, book=label) for x in t]
            still_open += o
        wins = [t for t in trades if t["net"] > 0]
        losses = [t for t in trades if t["net"] <= 0]
        net = sum(t["net"] for t in trades)
        lines = ["PAPER DAILY SUMMARY %s (simulated)" % day.isoformat(),
                 "Trades closed: %d | Winning: %d | Losing: %d" % (len(trades), len(wins), len(losses)),
                 "Gross P&L: %s | Costs: %s" % (_signed(sum(t["gross"] for t in trades)),
                                                 _rs(sum(t["costs"] for t in trades))),
                 "NET P&L: %s" % _signed(net)]
        for t in trades:
            lines.append("- %s %s: %s -> %s x %d, net %s" % (
                t["book"], names.get(t["instrument_id"], t["instrument_id"]), _rs(t["entry"]), _rs(t["exit"]),
                t["qty"], _signed(t["net"])))
        if still_open:
            lines.append("STILL OPEN: %d position(s); their P&L is not counted" % still_open)
        if not trades and not still_open:
            lines.append("No paper trades today.")
        return self.send("summary", "\n".join(lines), now)


def round_trips(records, day):
    """Closed trades on `day` from ledger fills, using the same arithmetic as Ledger.positions().
    Returns ([{instrument_id, entry, exit, qty, gross, costs, net}], open_positions)."""
    pos, out = {}, []
    for r in records:
        if r.get("event") != "fill":
            continue
        iid = str(r["instrument_id"])
        p = pos.setdefault(iid, {"qty": 0, "cost": 0.0, "charges": 0.0})
        if r["side"] == "BUY":
            p["qty"] += r["qty"]
            p["cost"] += r["price"] * r["qty"]
            p["charges"] += r["charges"]["total"]
            continue
        avg = p["cost"] / p["qty"] if p["qty"] else 0.0
        share = r["qty"] / p["qty"] if p["qty"] else 0.0
        entry_ch = p["charges"] * share
        gross = (r["price"] - avg) * r["qty"]
        costs = entry_ch + r["charges"]["total"]
        if _iso(r["time"]).date() == day:
            out.append({"instrument_id": iid, "entry": avg, "exit": r["price"], "qty": r["qty"],
                        "gross": round(gross, 2), "costs": round(costs, 2), "net": round(gross - costs, 2)})
        p["cost"] -= avg * r["qty"]
        p["charges"] -= entry_ch
        p["qty"] -= r["qty"]
    return out, sum(1 for p in pos.values() if p["qty"] > 0)


class Silent(object):
    """Used when no notifier is wired in (tests, older callers)."""

    def opened(self, *a):
        return None

    update = closed = daily_summary = opened
