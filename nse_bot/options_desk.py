"""Options-desk context for NIFTY option entries (ORB and sweep_reversal). CONTEXT ONLY: recorded with
each candidate and decision; never blocks or creates a trade, and never reads direction from OI alone.

Data audit, FYERS /options-chain-v3 live on 2026-11-02 (GitHub run 41420747976, scripts/probe_options_desk.py):
every one of 62 legs (31 strikes around ATM) had oi, prev_oi, oich, volume, bid, ask, ltp and Greeks
delta, gamma, theta, vega, iv; the response also carries chain totals callOi / putOi and India VIX.
India VIX daily history: 247 sessions for one year. No historical IV per option exists.

Units: FYERS OI and volume are in units (shares): ATM CE OI 17,600,310 = 270,774 lots of 65.
Greeks are per unit: delta per 1 point, gamma per 1 point (FYERS rounds gamma to 3 decimals, so far
strikes show 0.000 and drop out of gamma sums), theta in Rs per day, vega in Rs per 1 IV point.

What each figure is (and is not):
  OI, change in OI, PCR, concentration  reported as numbers. OI alone does not say who is long or
                                        short, so no direction is drawn from it.
  IV, skew                              from the chain's IVs; skew = OTM put IV - OTM call IV the same
                                        number of strikes from ATM.
  IV percentile                         India VIX percentile over the last year (needs >= 200 sessions).
                                        It is the 30-day index IV, NOT the option's own IV history.
  Greek exposures                       open interest x Greek, summed from the HOLDERS' side. Every
                                        long has a short, so these show where risk sits, not a net bet.
  GEX                                   ESTIMATE under the usual convention that dealers are long the
                                        calls and short the puts customers trade:
                                        GEX = sum(call gamma x OI) - sum(put gamma x OI), x spot^2 x 1%,
                                        = Rs change in dealer delta per 1% move. Indian option flow is
                                        retail-heavy, so this assumption may be wrong; window = the
                                        strikes FYERS returned (about +/-15 around ATM).
  Dealer positioning                    ESTIMATE derived from the sign of GEX only.
  Expiry / time decay                   days to expiry from FYERS's expiry time; ATM theta as % of premium.
"""

import datetime as dt

from . import clock

GEX_ASSUMPTION = ("ESTIMATE: assumes dealers are long the calls and short the puts that customers trade; "
                  "FYERS gamma is rounded to 3 decimals; window = strikes returned by FYERS")
DEFAULTS = {"skew_steps": (2, 4), "vix_min_sessions": 200, "vix_high_pct": 80, "vix_low_pct": 20,
            "theta_high_pct": 15.0, "wall_near_pct": 0.5}


def _sum(legs, typ, f):
    return sum(f(l) for l in legs if l["option_type"] == typ)


def analyse(chain, now, vix_history=None, p=None):
    p = dict(DEFAULTS, **(p or {}))
    spot = chain.get("spot")
    legs = [l for l in chain.get("legs") or [] if l.get("strike") is not None]
    if not spot or not legs:
        return {"status": "NOT AVAILABLE", "reason": "option chain has no spot or legs"}
    out = {"status": "OK", "spot": spot, "expiry": chain.get("expiry"), "strikes": len({l["strike"] for l in legs})}
    have_oi = all(l.get("oi") is not None for l in legs)
    have_prev = all(l.get("prev_oi") is not None for l in legs)

    # 1-3: OI, change, PCR, concentration ---------------------------------------------------
    if have_oi:
        coi, poi = _sum(legs, "CE", lambda l: l["oi"]), _sum(legs, "PE", lambda l: l["oi"])
        oi = {"call_oi": coi, "put_oi": poi, "pcr_oi": round(poi / float(coi), 3) if coi else None,
              "window": "strikes returned by FYERS", "fyers_chain_totals": chain.get("totals")}
        if have_prev:
            dc = _sum(legs, "CE", lambda l: l["oi"] - l["prev_oi"])
            dp = _sum(legs, "PE", lambda l: l["oi"] - l["prev_oi"])
            oi.update({"call_oi_change": dc, "put_oi_change": dp,
                       "pcr_change": round(dp / float(dc), 3) if dc > 0 and dp > 0 else None})
        else:
            oi["change"] = "NOT AVAILABLE: no previous-day OI in the chain"
        cv = _sum(legs, "CE", lambda l: l.get("volume") or 0)
        pv = _sum(legs, "PE", lambda l: l.get("volume") or 0)
        oi["pcr_volume"] = round(pv / float(cv), 3) if cv else None
        conc = {}
        for typ, tot in (("CE", coi), ("PE", poi)):
            top = sorted((l for l in legs if l["option_type"] == typ), key=lambda l: -l["oi"])[:3]
            conc[typ] = [{"strike": l["strike"], "oi": l["oi"], "share": round(l["oi"] / float(tot), 3) if tot else None,
                          "change": (l["oi"] - l["prev_oi"]) if have_prev else None} for l in top]
        oi["concentration"] = conc
        oi["note"] = "OI alone does not show who is long or short; no direction is inferred from it"
        out["open_interest"] = oi
    else:
        out["open_interest"] = {"status": "NOT AVAILABLE", "reason": "OI missing on some legs"}

    # 4-5: IV, skew, IV percentile ----------------------------------------------------------
    strikes = sorted({l["strike"] for l in legs})
    atm = min(strikes, key=lambda k: (abs(k - spot), k))
    by = {(l["option_type"], l["strike"]): l for l in legs}
    atm_ivs = [by[(t, atm)]["iv"] for t in ("CE", "PE") if (t, atm) in by and by[(t, atm)].get("iv")]
    iv = {"atm_strike": atm, "atm_iv": round(sum(atm_ivs) / len(atm_ivs), 2) if atm_ivs else None, "skew": {}}
    i = strikes.index(atm)
    for n in p["skew_steps"]:
        if i - n >= 0 and i + n < len(strikes):
            put, call = by.get(("PE", strikes[i - n])), by.get(("CE", strikes[i + n]))
            if put and call and put.get("iv") and call.get("iv"):
                iv["skew"]["%d strikes" % n] = {"otm_put_iv": put["iv"], "otm_call_iv": call["iv"],
                                                "put_minus_call": round(put["iv"] - call["iv"], 2)}
    vix_now = chain.get("india_vix")
    hist = [c for d, c in (vix_history or []) if d < now.date()][-252:]
    if vix_now and len(hist) >= p["vix_min_sessions"]:
        iv["iv_percentile"] = {"value": round(100.0 * sum(1 for c in hist if c < vix_now) / len(hist), 1),
                               "basis": "India VIX vs its last %d daily closes (index 30-day IV, not this "
                                        "option's own IV history)" % len(hist), "india_vix": vix_now}
    else:
        iv["iv_percentile"] = {"value": None, "status": "NOT AVAILABLE",
                               "reason": "need India VIX now and >= %d sessions of history (have %d)"
                                         % (p["vix_min_sessions"], len(hist))}
    out["implied_volatility"] = iv

    # 6-7: Greek exposures and GEX ----------------------------------------------------------
    greeks_ok = have_oi and all(l.get(g) is not None for l in legs for g in ("delta", "gamma", "theta", "vega"))
    if greeks_ok:
        ex = {}
        for typ in ("CE", "PE"):
            ex[typ] = {"delta_units": round(_sum(legs, typ, lambda l: l["delta"] * l["oi"])),
                       "gamma_units_per_point": round(_sum(legs, typ, lambda l: l["gamma"] * l["oi"])),
                       "theta_rs_per_day": round(_sum(legs, typ, lambda l: l["theta"] * l["oi"])),
                       "vega_rs_per_iv_point": round(_sum(legs, typ, lambda l: l["vega"] * l["oi"]))}
        ex["note"] = "open interest x Greek from the holders' side; every long has a short"
        out["greek_exposure"] = ex
        per_strike = {}
        for l in legs:
            sign = 1 if l["option_type"] == "CE" else -1
            per_strike[l["strike"]] = per_strike.get(l["strike"], 0.0) + sign * l["gamma"] * l["oi"] * spot * spot * 0.01
        gex = sum(per_strike.values())
        zero = sum(1 for l in legs if l["gamma"] == 0)
        out["gex"] = {"rs_per_1pct_move": round(gex), "label": GEX_ASSUMPTION,
                      "largest_positive_strike": max(per_strike, key=per_strike.get),
                      "largest_negative_strike": min(per_strike, key=per_strike.get),
                      "legs_with_gamma_rounded_to_zero": zero}
        # 9: dealer positioning, estimate only
        out["dealer_positioning"] = {
            "estimate": "long gamma" if gex > 0 else "short gamma" if gex < 0 else "flat",
            "meaning": ("hedging would tend to dampen moves (sell rallies, buy dips)" if gex > 0 else
                        "hedging would tend to amplify moves (buy rallies, sell dips)" if gex < 0 else "no effect"),
            "label": "ESTIMATE from the GEX sign only; " + GEX_ASSUMPTION + "; says nothing about direction"}
    else:
        out["greek_exposure"] = out["gex"] = out["dealer_positioning"] = {
            "status": "NOT AVAILABLE", "reason": "OI or Greeks missing on some legs"}

    # 8: expiry and time decay ---------------------------------------------------------------
    ep = chain.get("expiry_epoch")
    exp_t = clock.from_epoch_ms(ep * 1000) if ep else (
        clock.at(dt.date.fromisoformat(chain["expiry"]), "15:30") if chain.get("expiry") else None)
    risk = {}
    if exp_t:
        hrs = (exp_t - clock.to_ist(now)).total_seconds() / 3600.0
        risk.update({"expiry_time": exp_t.isoformat(), "hours_to_expiry": round(hrs, 1),
                     "expiry_day": exp_t.date() == now.date()})
    atm_legs = [by[(t, atm)] for t in ("CE", "PE") if (t, atm) in by]
    pct = [abs(l["theta"]) / l["ltp"] * 100 for l in atm_legs if l.get("theta") is not None and l.get("ltp")]
    risk["atm_theta_pct_of_premium_per_day"] = round(sum(pct) / len(pct), 1) if pct else None
    out["expiry_risk"] = risk or {"status": "NOT AVAILABLE"}
    return out


def context(setup, direction, desk, p=None):
    """Supportive / caution notes for one entry. Never a filter."""
    p = dict(DEFAULTS, **(p or {}))
    if desk.get("status") != "OK":
        return {"verdict": "NOT AVAILABLE", "reason": desk.get("reason"), "desk": desk}
    sup, cau, notes = [], [], []
    dp = desk.get("dealer_positioning", {}).get("estimate")
    if dp == "short gamma":
        (sup if setup == "orb" else cau).append(
            "estimated dealer short gamma: hedging may extend moves (%s)" % ("helps a breakout" if setup == "orb"
                                                                           else "works against a reversal"))
    elif dp == "long gamma":
        (cau if setup == "orb" else sup).append(
            "estimated dealer long gamma: hedging may damp moves (%s)" % ("works against a breakout" if setup == "orb"
                                                                        else "helps a reversal"))
    r = desk.get("expiry_risk", {})
    if r.get("expiry_day"):
        cau.append("expiry day: fast time decay and gamma risk")
    th = r.get("atm_theta_pct_of_premium_per_day")
    if th is not None and th >= p["theta_high_pct"]:
        cau.append("ATM theta %.1f%% of premium per day" % th)
    ivp = desk.get("implied_volatility", {}).get("iv_percentile", {}).get("value")
    if ivp is not None and ivp >= p["vix_high_pct"]:
        cau.append("India VIX in its top %d%% of the year: options are expensive" % (100 - p["vix_high_pct"]))
    elif ivp is not None and ivp <= p["vix_low_pct"]:
        sup.append("India VIX in its bottom %d%% of the year: options are cheap" % p["vix_low_pct"])
    spot = desk["spot"]
    for typ in ("CE", "PE"):
        for c in (desk.get("open_interest", {}).get("concentration") or {}).get(typ, [])[:1]:
            if abs(c["strike"] - spot) / spot * 100 <= p["wall_near_pct"]:
                notes.append("largest %s OI at %.0f, %s spot %.0f (within %.1f%%; a level to watch, OI alone is not "
                             "direction)" % (typ, c["strike"], "above" if c["strike"] > spot else "below"
                                             if c["strike"] < spot else "at", spot, p["wall_near_pct"]))
    verdict = "SUPPORTIVE" if len(sup) > len(cau) else "CAUTION" if len(cau) > len(sup) else "NEUTRAL"
    return {"verdict": verdict, "supports": sup, "cautions": cau, "notes": notes, "setup": setup,
            "direction": "up" if direction > 0 else "down", "desk": desk,
            "note": "context only: recorded with the decision, never blocks or forces a trade"}


def for_entry(ctx, setup, underlying, option_type, expiry, now):
    """Fetch the chain (and India VIX history when the adapter has it) and build the context.
    Any failure gives NOT AVAILABLE and never affects the entry."""
    try:
        from .contract_select import parse_chain
        iid = ctx.master.index_id(ctx.cfg["index_names"][underlying])
        try:
            raw = ctx.md.option_chain(iid, expiry, enrich_depth=False)
        except TypeError:
            raw = ctx.md.option_chain(iid, expiry)
        chain = parse_chain(raw)
        vix = ctx.md.vix_daily(now) if hasattr(ctx.md, "vix_daily") else None
        return context(setup, 1 if option_type == "CE" else -1, analyse(chain, now, vix))
    except Exception as e:                      # noqa: BLE001  (context only)
        return {"verdict": "NOT AVAILABLE", "reason": "%s: %s" % (type(e).__name__, e)}
