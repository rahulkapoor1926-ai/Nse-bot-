"""sweep_reversal: liquidity sweep -> reclaim -> confirmation -> BOS/CHoCH -> FVG/OB entry.

One evaluation = one Trade Decision Report (see decisions.py). The report lists
every rule as pass / fail / not_reached with the candles that prove it. The
first failing rule decides: SKIP. A setup that passes every rule is only a
candidate; the validation gate, budget and risk checks still decide entry.

Inputs (all closed bars, aware IST times; nothing after `now` may be passed):
  u1   underlying 1m bars, previous session(s) + today      (t, o, h, l, c[, v])
  u1h  underlying 1h bars (history)
  u1d  underlying daily bars of earlier sessions
  f1   current-month index future 1m bars WITH volume, today + earlier sessions
  get_chain  callable returning the parsed option chain (contract_select.parse_chain),
             called only when the setup has passed every underlying rule
Definitions are documented in docs/nse/LIQUIDITY_SWEEP_PLAN.md.
"""

import datetime as dt

from . import clock, contract_select, structure as st

PATTERN = "sweep_reversal"
PARAM_KEYS = ("swing_n", "equal_tol_pct", "sweep_min_pen_pct", "reclaim_bars", "confirm_within_bars",
              "confirm_body_pct", "fvg_min_pct", "vol_expansion_x", "vol_lookback_bars", "rvol_min",
              "rvol_sessions", "rsi_n", "ema_n", "h1_slope_bars", "entry_window_minutes",
              "stop_buffer_pct", "max_itm_strikes", "delta_min", "delta_max", "delta_target",
              "max_theta_pct_per_day", "iv_shock_points", "gap_allowance_pct",
              "fut_min_coverage_pct", "fut_max_gap_minutes", "signal_max_age_minutes")

RULES = ("data_underlying", "data_future", "liquidity_levels", "sweep", "confirmation", "structure",
         "zone", "vwap", "volume_expansion", "relative_volume", "momentum", "tf_daily", "tf_1h", "tf_15m",
         "entry_1m", "target_level", "contract")


def validate(strategy):
    from .errors import DataError
    miss = [k for k in ("name", "instrument", "pattern", "params", "interval_minutes") if k not in strategy]
    miss += ["params." + k for k in PARAM_KEYS if k not in strategy.get("params", {})]
    if miss:
        raise DataError("sweep_reversal strategy is missing %s" % ", ".join(miss))
    if strategy["instrument"] != "index_option":
        raise DataError("sweep_reversal trades index options only")
    if strategy["interval_minutes"] != 5:
        raise DataError("sweep_reversal uses 5-minute structure (interval_minutes 5)")
    return strategy


def _ref(series, b):
    return {"series": series, "t": b[0].isoformat(), "o": b[1], "h": b[2], "l": b[3], "c": b[4],
            "v": b[5] if len(b) > 5 else None}


class Report(object):
    def __init__(self, strategy, underlying, now):
        self.doc = {"strategy": strategy["name"], "pattern": PATTERN, "underlying": underlying,
                    "evaluated_at": now.isoformat(), "decision": "SKIP", "skip_reason": None,
                    "bias": None, "why": None, "rules": [], "evidence": [], "label": "SIMULATED (paper)"}
        self.failed = False

    def rule(self, name, ok, detail, evidence=()):
        if self.failed:
            return False
        self.doc["rules"].append({"rule": name, "status": "pass" if ok else "fail", "detail": detail})
        self.doc["evidence"].extend(evidence)
        if not ok:
            self.failed = True
            self.doc["skip_reason"] = "%s: %s" % (name, detail)
        return ok

    def finish(self):
        done = {r["rule"] for r in self.doc["rules"]}
        for name in RULES:
            if name not in done:
                self.doc["rules"].append({"rule": name, "status": "not_reached", "detail": None})
        return self.doc


def futures_quality(f_today, u_today, p):
    """Fail closed unless the future's 1m data covers the session with real volume."""
    if not f_today:
        return False, "no 1m bars for the index future today"
    if any(len(b) < 6 or b[5] is None or b[5] < 0 for b in f_today):
        return False, "future bars without volume"
    if sum(b[5] for b in f_today) <= 0:
        return False, "future volume is zero all session"
    ft = {b[0] for b in f_today}
    want = [b[0] for b in u_today]
    if want:
        cov = 100.0 * sum(1 for t in want if t in ft) / len(want)
        if cov < p["fut_min_coverage_pct"]:
            return False, "future covers %.1f%% of index minutes (< %s%%)" % (cov, p["fut_min_coverage_pct"])
    ts = sorted(ft)
    gaps = [(b - a).total_seconds() / 60 - 1 for a, b in zip(ts, ts[1:])]
    if gaps and max(gaps) > p["fut_max_gap_minutes"]:
        return False, "future has a gap of %d minutes" % max(gaps)
    for b in f_today:
        if not (b[3] <= min(b[1], b[4]) and b[2] >= max(b[1], b[4]) and b[3] > 0):
            return False, "inconsistent future bar at %s" % b[0].isoformat()
    return True, "%d bars, %d contracts traded today" % (len(f_today), sum(b[5] for b in f_today))


def _levels(u1, today, p):
    """Liquidity levels known before today's bars: PDH/PDL, 15m swings, equal highs/lows."""
    prev = [b for b in u1 if b[0].date() < today]
    if not prev:
        return []
    pday = max(b[0].date() for b in prev)
    pbars = [b for b in prev if b[0].date() == pday]
    hi = max(pbars, key=lambda b: b[2])
    lo = min(pbars, key=lambda b: b[3])
    out = [{"type": "PDH", "side": "buy", "price": hi[2], "known_at": clock.at(today, "09:15"), "ref": _ref("1m", hi)},
           {"type": "PDL", "side": "sell", "price": lo[3], "known_at": clock.at(today, "09:15"), "ref": _ref("1m", lo)}]
    b15 = st.resample(u1, 15)
    sw = st.swings(b15, p["swing_n"], 15)
    for s in sw:
        b = b15[s["i"]]
        out.append({"type": "swing_" + s["kind"], "side": "buy" if s["kind"] == "high" else "sell",
                    "price": s["price"], "known_at": s["known_at"], "ref": _ref("15m", b)})
    for kind, side in (("high", "buy"), ("low", "sell")):
        for e in st.equal_levels(sw, kind, p["equal_tol_pct"]):
            out.append({"type": e["kind"], "side": side, "price": e["price"], "known_at": e["known_at"],
                        "ref": {"series": "15m", "members": [t.isoformat() for t in e["members"]]}})
    return out


def evaluate(strategy, underlying, now, u1, u1h, u1d, f1, get_chain=None, master=None, cfg=None):
    p = strategy["params"]
    now = clock.to_ist(now)
    today = now.date()
    R = Report(strategy, underlying, now)
    u1 = st.closed(u1, now, 1)
    f1 = st.closed(f1, now, 1)
    u_today = [b for b in u1 if b[0].date() == today]
    f_today = [b for b in f1 if b[0].date() == today]
    if not R.rule("data_underlying", len(u_today) >= 15 and any(b[0].date() < today for b in u1),
                  "%d index 1m bars today; previous session %s" % (
                      len(u_today), "present" if any(b[0].date() < today for b in u1) else "missing")):
        return R.finish()
    ok, why = futures_quality(f_today, u_today, p)
    if not R.rule("data_future", ok, why):
        return R.finish()

    levels = _levels(u1, today, p)
    R.rule("liquidity_levels", bool(levels), "%d levels (PDH/PDL, 15m swings, equal highs/lows)" % len(levels))
    R.doc["levels"] = [{k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in l.items()}
                       for l in levels]
    if R.failed:
        return R.finish()

    b5_all = st.resample(u1, 5)
    first_today = next((i for i, b in enumerate(b5_all) if b[0].date() == today), None)
    if first_today is None:
        R.rule("sweep", False, "no complete 5m bar today yet")
        return R.finish()
    best, accepted = None, []
    for lv in levels:
        start = next((i for i in range(first_today, len(b5_all)) if b5_all[i][0] >= lv["known_at"]), None)
        if start is None:
            continue
        side = lv["side"]
        if (side == "sell" and lv["price"] >= b5_all[start][1]) or (side == "buy" and lv["price"] <= b5_all[start][1]):
            continue                    # level was already on the wrong side when it became known
        res = st.find_sweep(b5_all, lv["price"], side, p["sweep_min_pen_pct"], p["reclaim_bars"], start)
        if res is None:
            continue
        if res["status"] == "accepted":
            accepted.append("%s %.2f broken and held at %s" % (lv["type"], lv["price"],
                                                               b5_all[res["sweep_i"]][0].strftime("%H:%M")))
            continue
        res["lv"] = lv
        if best is None or res["reclaim_i"] > best["reclaim_i"]:
            best = res
    if best is None:
        R.rule("sweep", False, "no liquidity sweep with a reclaim today" +
               ("; accepted breaks (not sweeps): " + "; ".join(accepted) if accepted else ""))
        return R.finish()
    side, lv = best["lv"]["side"], best["lv"]
    bias = "bullish" if side == "sell" else "bearish"
    R.doc["bias"] = bias
    sb, rb, xb = b5_all[best["sweep_i"]], b5_all[best["reclaim_i"]], b5_all[best["extreme_i"]]
    R.doc["liquidity"] = {"swept": "%s liquidity" % ("sell-side" if side == "sell" else "buy-side"),
                          "level_type": lv["type"], "level": lv["price"], "level_ref": lv["ref"],
                          "sweep_bar": sb[0].isoformat(), "sweep_extreme": best["extreme"],
                          "reclaim_bar": rb[0].isoformat(), "reclaim_close": rb[4]}
    R.rule("sweep", True, "%s %.2f swept to %.2f at %s, reclaimed by the %s close %.2f" % (
        lv["type"], lv["price"], best["extreme"], sb[0].strftime("%H:%M"), rb[0].strftime("%H:%M"), rb[4]),
        [_ref("5m", sb), _ref("5m", xb), _ref("5m", rb)])

    k = st.confirmation(b5_all, best["sweep_i"], best["reclaim_i"], side, p["confirm_within_bars"],
                        p["confirm_body_pct"])
    if not R.rule("confirmation", k is not None,
                  "no 5m close beyond the sweep bar's %s with a %s%%+ body within %d bars" % (
                      "high" if side == "sell" else "low", p["confirm_body_pct"], p["confirm_within_bars"])
                  if k is None else "confirmation candle %s close %.2f" % (b5_all[k][0].strftime("%H:%M"), b5_all[k][4]),
                  [] if k is None else [_ref("5m", b5_all[k])]):
        return R.finish()
    cb = b5_all[k]
    R.doc["confirmation_candle"] = _ref("5m", cb)

    sw5 = st.swings(b5_all, p["swing_n"], 5)
    brk = st.structure_break(b5_all, sw5, best["sweep_i"], k, side, 5)
    if not R.rule("structure", brk is not None, "no BOS/CHoCH between the sweep and the confirmation"
                  if brk is None else "%s: close %.2f at %s beyond swing %.2f (%s) after a %s structure" % (
                      brk["type"], b5_all[brk["break_i"]][4], b5_all[brk["break_i"]][0].strftime("%H:%M"),
                      brk["swing"]["price"], brk["swing"]["t"].strftime("%H:%M"), brk["prior_trend"]),
                  [] if brk is None else [_ref("5m", b5_all[brk["break_i"]]), _ref("5m", b5_all[brk["swing"]["i"]])]):
        return R.finish()
    R.doc["structure"] = {"type": brk["type"], "prior_trend": brk["prior_trend"], "swing_price": brk["swing"]["price"],
                          "swing_bar": brk["swing"]["t"].isoformat(), "break_bar": b5_all[brk["break_i"]][0].isoformat()}

    fvgs = st.fair_value_gaps(b5_all, best["sweep_i"], brk["break_i"] + 1, side, p["fvg_min_pct"])
    ob = st.order_block(b5_all, best["sweep_i"], brk["break_i"], side)
    zone = None
    if fvgs:
        g = fvgs[-1]
        zone = {"kind": "FVG", "bottom": g["bottom"], "top": g["top"], "bars": [t.isoformat() for t in g["bars"]],
                "known_at": st.bar_end(b5_all[g["i"] + 2], 5)}
        ev = [_ref("5m", b5_all[g["i"] + j]) for j in range(3)]
    elif ob:
        # an order block is only defined once the displacement bar has closed
        zone = {"kind": "order_block", "bottom": ob["bottom"], "top": ob["top"], "bars": [ob["bar"].isoformat()],
                "known_at": st.bar_end(b5_all[brk["break_i"]], 5)}
        ev = [_ref("5m", b5_all[ob["i"]])]
    if not R.rule("zone", zone is not None, "no fair value gap (>= %s%%) or order block in the move" % p["fvg_min_pct"]
                  if zone is None else "%s %.2f-%.2f" % (zone["kind"], zone["bottom"], zone["top"]),
                  ev if zone else []):
        return R.finish()
    R.doc["zone"] = dict(zone, known_at=zone["known_at"].isoformat())
    R.doc["other_zone"] = ({"kind": "order_block", "bottom": ob["bottom"], "top": ob["top"],
                            "bars": [ob["bar"].isoformat()]} if fvgs and ob else None)

    vw = st.vwap(f_today)
    f_last = f_today[-1]
    vw_ok = vw is not None and (f_last[4] > vw if bias == "bullish" else f_last[4] < vw)
    R.doc["vwap"] = {"future_vwap": None if vw is None else round(vw, 2), "future_close": f_last[4],
                     "at": f_last[0].isoformat()}
    if not R.rule("vwap", vw_ok, "VWAP unavailable" if vw is None else "future close %.2f %s VWAP %.2f" % (
            f_last[4], "above" if f_last[4] > vw else "below", vw), [_ref("fut1m", f_last)]):
        return R.finish()

    f5 = st.resample(f1, 5)
    f5_by_t = {b[0]: i for i, b in enumerate(f5)}
    disp_t = b5_all[brk["break_i"]][0]
    j = f5_by_t.get(disp_t)
    vol_ok, vdet = False, "no complete 5m future bar at the displacement time %s" % disp_t.strftime("%H:%M")
    if j is not None and j >= p["vol_lookback_bars"]:
        prior = [b[5] for b in f5[j - p["vol_lookback_bars"]:j]]
        if None not in prior and f5[j][5] is not None:
            avg = sum(prior) / len(prior)
            ratio = f5[j][5] / avg if avg > 0 else 0.0
            vol_ok = ratio >= p["vol_expansion_x"]
            vdet = "displacement bar volume %d = %.2fx the %d-bar average %.0f (need %sx)" % (
                f5[j][5], ratio, p["vol_lookback_bars"], avg, p["vol_expansion_x"])
    elif j is not None:
        vdet = "fewer than %d earlier 5m future bars" % p["vol_lookback_bars"]
    R.doc["volume"] = vdet
    if not R.rule("volume_expansion", vol_ok, vdet, [_ref("fut5m", f5[j])] if j is not None else []):
        return R.finish()

    sessions = sorted({b[0].date() for b in f1 if b[0].date() < today})[-p["rvol_sessions"]:]
    cut = (now - clock.at(today, "09:15"))
    today_vol = sum(b[5] for b in f_today)
    hist = []
    for d in sessions:
        lim = clock.at(d, "09:15") + cut
        bars = [b for b in f1 if b[0].date() == d and st.bar_end(b, 1) <= lim]
        if bars and all(b[5] is not None for b in bars):
            hist.append(sum(b[5] for b in bars))
    if len(hist) < p["rvol_sessions"]:
        R.rule("relative_volume", False, "only %d of %d earlier sessions of future volume available"
               % (len(hist), p["rvol_sessions"]))
        return R.finish()
    avg_hist = sum(hist) / len(hist)
    rvol = today_vol / avg_hist if avg_hist > 0 else 0.0
    R.doc["relative_volume"] = round(rvol, 3)
    if not R.rule("relative_volume", rvol >= p["rvol_min"], "today's future volume to %s is %.2fx the %d-session "
                  "average for the same window (need %sx)" % (now.strftime("%H:%M"), rvol, len(hist), p["rvol_min"])):
        return R.finish()

    r = st.rsi([b[4] for b in b5_all], p["rsi_n"])
    R.doc["rsi_5m"] = None if r is None else round(r, 2)
    if not R.rule("momentum", r is not None and (r > 50 if bias == "bullish" else r < 50),
                  "RSI(%d) on 5m unavailable" % p["rsi_n"] if r is None else "RSI(%d) 5m = %.1f" % (p["rsi_n"], r)):
        return R.finish()

    daily = [b for b in u1d if b[0].date() < today]
    e = st.ema([b[4] for b in daily], p["ema_n"])
    d_ok = e is not None and (daily[-1][4] > e[-1] if bias == "bullish" else daily[-1][4] < e[-1])
    R.doc["tf_daily"] = None if e is None else {"close": daily[-1][4], "ema": round(e[-1], 2)}
    if not R.rule("tf_daily", d_ok, "fewer than %d daily bars" % p["ema_n"] if e is None else
                  "daily close %.2f vs EMA%d %.2f" % (daily[-1][4], p["ema_n"], e[-1]),
                  [] if e is None else [_ref("1d", daily[-1])]):
        return R.finish()

    hours = st.closed(u1h, now, 60)
    eh = st.ema([b[4] for b in hours], p["ema_n"])
    h_ok = False
    if eh is not None and len(eh) > p["h1_slope_bars"]:
        a, b = eh[-1 - p["h1_slope_bars"]], eh[-1]
        h_ok = b > a if bias == "bullish" else b < a
        hdet = "1h EMA%d %.2f -> %.2f over %d bars" % (p["ema_n"], a, b, p["h1_slope_bars"])
    else:
        hdet = "not enough closed 1h bars for EMA%d slope" % p["ema_n"]
    R.doc["tf_1h"] = hdet
    if not R.rule("tf_1h", h_ok, hdet, [_ref("1h", hours[-1])] if hours else []):
        return R.finish()

    b15 = [b for b in st.resample(u1, 15) if b[0] >= sb[0]]
    if bias == "bullish":
        broke = [b for b in b15 if b[3] < best["extreme"]]
    else:
        broke = [b for b in b15 if b[2] > best["extreme"]]
    if not R.rule("tf_15m", not broke, "15m made a new %s beyond the sweep extreme at %s" % (
            "low" if bias == "bullish" else "high", broke[0][0].strftime("%H:%M")) if broke else
            "15m held the sweep extreme %.2f (%d bars since the sweep)" % (best["extreme"], len(b15)),
            [_ref("15m", broke[0])] if broke else []):
        return R.finish()

    cend = st.bar_end(cb, 5)
    window_end = cend + dt.timedelta(minutes=p["entry_window_minutes"])
    look_from = max(cend, zone["known_at"])          # the 1m trigger must come after the zone exists
    u_stop = best["extreme"] * (1 - p["stop_buffer_pct"] / 100.0) if bias == "bullish" \
        else best["extreme"] * (1 + p["stop_buffer_pct"] / 100.0)
    trig = None
    for b in u_today:
        if b[0] < look_from or st.bar_end(b, 1) > window_end:
            continue
        if (bias == "bullish" and b[3] <= u_stop) or (bias == "bearish" and b[2] >= u_stop):
            break                       # stop level traded before an entry: setup is void
        if bias == "bullish" and b[3] <= zone["top"] and b[4] > b[1] and b[4] > zone["bottom"]:
            trig = b
            break
        if bias == "bearish" and b[2] >= zone["bottom"] and b[4] < b[1] and b[4] < zone["top"]:
            trig = b
            break
    if trig is None:
        if now < window_end:
            R.rule("entry_1m", False, "waiting: no 1m retrace into the %s with a %s close yet (window to %s)" % (
                zone["kind"], "bullish" if bias == "bullish" else "bearish", window_end.strftime("%H:%M")))
        else:
            R.rule("entry_1m", False, "no 1m entry trigger within %d minutes of the confirmation" % p["entry_window_minutes"])
        return R.finish()
    known = st.bar_end(trig, 1)
    age = (now - known).total_seconds() / 60.0
    if not R.rule("entry_1m", age <= p["signal_max_age_minutes"],
                  "1m trigger %s close %.2f, %.1f min old" % (trig[0].strftime("%H:%M"), trig[4], age),
                  [_ref("1m", trig)]):
        return R.finish()
    spot = trig[4]

    opp = [l for l in levels if l["known_at"] <= known and
           ((bias == "bullish" and l["side"] == "buy" and l["price"] > spot) or
            (bias == "bearish" and l["side"] == "sell" and l["price"] < spot))]
    tgt = (min(opp, key=lambda l: l["price"]) if bias == "bullish" else max(opp, key=lambda l: l["price"])) if opp else None
    if not R.rule("target_level", tgt is not None, "no opposing liquidity level for a target" if tgt is None else
                  "target = %s %.2f; underlying stop %.2f (%.2f%% beyond the sweep extreme)" % (
                      tgt["type"], tgt["price"], u_stop, p["stop_buffer_pct"])):
        return R.finish()
    R.doc["underlying_plan"] = {"spot": spot, "stop": round(u_stop, 2), "target": tgt["price"],
                                "target_type": tgt["type"],
                                "reward_risk_points": round(abs(tgt["price"] - spot) / abs(spot - u_stop), 3)}
    R.doc["signal_known_at"] = known.isoformat()

    if get_chain is None:
        R.rule("contract", False, "no option chain source")
        return R.finish()
    try:
        chain = get_chain()
    except Exception as e:                               # noqa: BLE001  (reported, never a pass)
        R.rule("contract", False, "option chain unavailable: %s" % e)
        return R.finish()
    chosen, cands = contract_select.select(chain, master, bias, spot, u_stop, tgt["price"], now, cfg, p)
    R.doc["contract_candidates"] = [{k: v for k, v in c.items() if k != "contract"} for c in cands]
    if not R.rule("contract", chosen is not None, "no eligible ATM/ITM %s (see candidates)" % (
            "CE" if bias == "bullish" else "PE") if chosen is None else "%s %s strike %.0f, delta %.2f, IV %.1f, spread %.2f%%"
            % (chosen["symbol"], chosen["moneyness"], chosen["strike"], chosen["delta"], chosen["iv"], chosen["spread_pct"])):
        return R.finish()
    R.doc["contract"] = {k: v for k, v in chosen.items() if k != "contract"}
    R.doc["chain_spot"] = chain.get("spot")
    R.doc["decision"] = "SETUP"
    R.doc["why"] = ("%s: %s %.2f swept at %s and reclaimed; %s confirmed; entry from the %s on the 1m close at %s; "
                    "%s %s, target %s" % (bias, lv["type"], lv["price"], sb[0].strftime("%H:%M"), brk["type"],
                                          zone["kind"], trig[0].strftime("%H:%M"), chosen["moneyness"],
                                          chosen["symbol"], tgt["type"]))
    out = R.finish()
    out["_selected"] = chosen
    out["_u_stop"] = u_stop
    return out
