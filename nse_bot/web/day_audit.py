"""Day verdict for the dashboard: the read-only replay (cli day-audit, real FYERS bars, unchanged rules)
set against what the bots themselves logged. Decides nothing for trading; it explains a day.

Verdicts, worst first:
  DATA FAILURE               the day's bars or the option premium could not be read
  STRATEGY ENGINE FAILURE    the replay found an executable setup and the bot logged nothing about it
  ENTRY EXECUTION FAILURE    an executable setup that the bot saw but did not enter (with the bot's reason)
  TRADED                     the bots entered
  NO VALID SETUP             no signal, or a signal that no approved risk rule allows (e.g. Rs 500 per trade)
"""

RANK = ["DATA FAILURE", "STRATEGY ENGINE FAILURE", "ENTRY EXECUTION FAILURE", "TRADED", "NO VALID SETUP"]


def verdict(audit, bot_signals, risk):
    """audit: day-audit JSON or None; bot_signals: View.signals(mode); risk: View.risk(mode)."""
    if not audit:
        return {"verdict": "NOT AUDITED YET", "findings": [],
                "why": "no replay of today's FYERS bars in this deployment yet"}
    out = []
    entries = int((risk or {}).get("entries") or 0)
    data = audit.get("data") or {}
    if not data.get("index_1m_bars_today"):
        out.append({"strategy": "both", "verdict": "DATA FAILURE", "rule": "data",
                    "reason": "FYERS served no 1-minute NIFTY bars for %s" % audit.get("day")})
    orb = audit.get("ORB") or {}
    bot_orb = (bot_signals or {}).get("orb") or []
    if data.get("index_1m_bars_today") and not orb.get("signals"):
        r = orb.get("opening_range") or {}
        out.append({"strategy": "ORB", "verdict": "NO VALID SETUP", "rule": "orb breakout",
                    "reason": "no 5-minute close above %s or below %s (opening range %s-%s) up to %s" % (
                        r.get("ce_trigger_close_above"), r.get("pe_trigger_close_below"), r.get("low"),
                        r.get("high"), str(data.get("last_bar"))[11:16])})
    for s in orb.get("signals") or []:
        p = s.get("entry_window_premium") or {}
        c = (s.get("contract") or {}).get("symbol")
        base = {"strategy": "ORB", "time": s.get("signal_time"), "contract": c,
                "entry_window": s.get("entry_window")}
        if p.get("lots_under_rs500_rule") is None:
            out.append(dict(base, verdict="DATA FAILURE", rule="option premium",
                            reason=p.get("why") or "no option price in the entry window"))
        elif p["lots_under_rs500_rule"] < 1:
            out.append(dict(base, verdict="NO VALID SETUP", rule="max_risk_per_trade_inr",
                            reason="valid %s signal, but at premium %.2f (LTP %s) one lot loses more than Rs 500 at "
                                   "the 20%% stop: %s" % (s.get("option_type"), p["open_ltp"], str(p["bar"])[11:16],
                                                          "; ".join((p.get("sizing") or {}).get("reasons") or []))))
        elif entries:
            out.append(dict(base, verdict="TRADED", rule="-", reason="the bots entered %d trade(s)" % entries))
        else:
            seen = [b for b in bot_orb if b.get("contract") == c] or bot_orb
            if not seen:
                out.append(dict(base, verdict="STRATEGY ENGINE FAILURE", rule="engine",
                                reason="executable setup (one lot fits at %.2f) but the ORB bot logged nothing"
                                       % p["open_ltp"]))
            else:
                b = seen[0]
                out.append(dict(base, verdict="ENTRY EXECUTION FAILURE", rule="entry",
                                reason="executable setup, bot %s at %s: %s" % (b.get("status"), str(b.get("time"))[11:19],
                                                                               b.get("reasons"))))
    sw = audit.get("Sweep") or {}
    if data.get("index_1m_bars_today"):
        if sw.get("not_evaluable"):
            out.append({"strategy": "Sweep", "verdict": "DATA FAILURE", "rule": "data_future",
                        "reason": "no index-future 1-minute bars"})
        elif not sw.get("signals"):
            d = (sw.get("day_audit") or [{}])[0]
            failed = [k for k, v in (d.get("rules") or {}).items() if v == "fail"]
            out.append({"strategy": "Sweep", "verdict": "NO VALID SETUP", "rule": failed[0] if failed else "-",
                        "time": d.get("evaluated_at"), "reason": d.get("skip_reason") or "no sweep setup"})
        else:
            for s in sw["signals"]:
                out.append({"strategy": "Sweep", "time": s.get("signal_time"),
                            "verdict": "TRADED" if entries else "ENTRY EXECUTION FAILURE", "rule": "entry",
                            "reason": "replay found a sweep setup; check the Sweep decisions above"})
    worst = min((f["verdict"] for f in out), key=RANK.index) if out else "NO VALID SETUP"
    return {"verdict": worst, "findings": out, "audited_at": audit.get("generated"),
            "data": data, "orb_opening_range": orb.get("opening_range"), "orb_rules": orb.get("rules"),
            "note": "Replay of real FYERS 1-minute bars through the unchanged rules (read-only). The ORB premium is "
                    "the LTP of the first 1-minute option bar of the entry window; FYERS history has no bid/ask."}
