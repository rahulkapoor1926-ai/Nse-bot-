"""Option contract selection for sweep_reversal, from SAHI's option chain with Greeks.

Rules: CE for bullish, PE for bearish, long only. ATM (strike nearest spot) or
up to `max_itm_strikes` in the money. Out-of-the-money strikes are refused.
Every value (bid, ask, OI, volume, IV, delta, gamma, theta, vega) is SAHI's
reported value; a missing one makes that contract ineligible. Nothing is
computed from a model to fill a gap.

Premium projection (estimate only, never an execution price):
    P(dS, dIV, days) = P + delta*dS + 0.5*gamma*dS^2 + vega*dIV + theta*days
with theta per calendar day and vega per 1 IV point, as SAHI documents them.
"""

import datetime as dt
import math

from . import clock
from .errors import DataError

UNITS_NOTE = ("SAHI reports prices, theta and vega in paise; they are converted to rupees here. "
              "Delta and gamma are unit-free. Verify against the first live option chain.")


def _px(v):
    return None if v is None else v / 100.0


def parse_chain(doc):
    """SAHI /v1/marketdata/optionchain -> {"spot", "legs": [...]}; prices in rupees.
    A FYERS chain arrives already in this shape ("format": "legs") and is passed through."""
    if isinstance(doc, dict) and doc.get("format") == "legs":
        if not isinstance(doc.get("legs"), list):
            raise DataError("option chain has no legs")
        return doc
    if not isinstance(doc, dict) or "strikes" not in doc:
        raise DataError("option chain response has no strikes")
    legs = []
    for row in doc["strikes"] or []:
        strike = row.get("strike")
        for typ in ("ce", "pe"):
            leg = row.get(typ)
            if not leg or strike is None:
                continue
            g = leg.get("greeks") or {}
            legs.append({
                "instrument_id": leg.get("instrument_id"), "option_type": typ.upper(),
                # strikes arrive in paise like every other SAHI price (2,500,000 = Rs 25,000);
                # a value already in rupees (below 1,000,000) is kept as is
                "strike": strike / 100.0 if strike > 10 ** 6 else float(strike),
                "bid": _px(leg.get("top_bid_price")), "ask": _px(leg.get("top_ask_price")),
                "bid_qty": leg.get("top_bid_qty"), "ask_qty": leg.get("top_ask_qty"),
                "ltp": _px(leg.get("last_trade_price")), "volume": leg.get("volume_today"),
                "oi": leg.get("open_interest"), "prev_oi": leg.get("prev_open_interest"),
                "iv": leg.get("implied_volatility"),
                "delta": g.get("delta"), "gamma": g.get("gamma"),
                "theta": _px(g.get("theta")), "vega": _px(g.get("vega")),
            })
    spot = doc.get("last_price")
    return {"spot": _px(spot), "legs": legs, "units_note": UNITS_NOTE}


def project(leg, premium, d_spot, d_iv, days):
    """Taylor estimate of the premium after a spot move, IV change and time decay."""
    return (premium + leg["delta"] * d_spot + 0.5 * leg["gamma"] * d_spot * d_spot
            + leg["vega"] * d_iv + leg["theta"] * days)


def moneyness(option_type, strike, spot, atm_strike):
    if strike == atm_strike:
        return "ATM"
    itm = strike < spot if option_type == "CE" else strike > spot
    return "ITM" if itm else "OTM"


def _tick_floor(x, tick):
    return round(math.floor(x / tick + 1e-9) * tick, 2)


def _tick_ceil(x, tick):
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def select(chain, master, bias, spot, u_stop, u_target, now, cfg, params):
    """Choose the contract. Returns (chosen or None, [every candidate with its verdict]).

    chosen carries the entry (ask + slippage), premium stop and target, the
    conservative stop used for sizing, and reward:risk.
    """
    typ = "CE" if bias == "bullish" else "PE"
    legs = [l for l in chain["legs"] if l["option_type"] == typ]
    strikes = sorted({l["strike"] for l in legs})
    if not strikes or spot is None:
        return None, [{"verdict": "no %s strikes or no spot in the chain" % typ}]
    atm = min(strikes, key=lambda k: (abs(k - spot), k))
    flat = clock.at(now.date(), cfg["flat_by"])
    hold_days = max(0.0, (flat - now).total_seconds()) / 86400.0
    out, eligible = [], []
    for leg in sorted(legs, key=lambda l: l["strike"]):
        m = moneyness(typ, leg["strike"], spot, atm)
        row = {"strike": leg["strike"], "option_type": typ, "moneyness": m,
               "instrument_id": leg["instrument_id"], "reasons": []}
        if m == "OTM":
            continue                                    # never considered, never selected
        steps = abs(strikes.index(leg["strike"]) - strikes.index(atm))
        if m == "ITM" and steps > params["max_itm_strikes"]:
            continue
        out.append(row)
        why = row["reasons"]
        for k in ("bid", "ask", "bid_qty", "ask_qty", "volume", "oi", "iv", "delta", "gamma", "theta", "vega"):
            if leg.get(k) is None:
                why.append("%s missing from SAHI" % k)
        if why:
            continue
        c = master.by_id(int(leg["instrument_id"])) if master else None
        if c is None:
            why.append("not in today's scrip master")
            continue
        row.update({k: leg[k] for k in ("bid", "ask", "volume", "oi", "iv", "delta", "gamma", "theta", "vega")})
        row["symbol"], row["expiry"] = c.symbol, c.expiry.isoformat()
        if leg["bid"] <= 0 or leg["ask"] <= 0 or leg["bid"] > leg["ask"]:
            why.append("crossed or empty quote")
            continue
        mid = (leg["bid"] + leg["ask"]) / 2.0
        spread = (leg["ask"] - leg["bid"]) / mid * 100.0
        row["spread_pct"] = round(spread, 3)
        if spread > cfg["max_spread_pct"]:
            why.append("spread %.2f%% > %s%%" % (spread, cfg["max_spread_pct"]))
        if leg["ask_qty"] < cfg["min_top_qty_lots"] * c.lot_size or leg["bid_qty"] < cfg["min_top_qty_lots"] * c.lot_size:
            why.append("top-of-book size below %d lots" % cfg["min_top_qty_lots"])
        if leg["volume"] < cfg["min_volume_today"]:
            why.append("volume %s < %s" % (leg["volume"], cfg["min_volume_today"]))
        if leg["oi"] < cfg["min_open_interest"]:
            why.append("OI %s < %s" % (leg["oi"], cfg["min_open_interest"]))
        d = abs(leg["delta"])
        if not params["delta_min"] <= d <= params["delta_max"]:
            why.append("|delta| %.2f outside %.2f-%.2f" % (d, params["delta_min"], params["delta_max"]))
        if leg["ask"] > 0 and abs(leg["theta"]) / leg["ask"] * 100.0 > params["max_theta_pct_per_day"]:
            why.append("theta %.2f%% of premium per day > %s%%" % (abs(leg["theta"]) / leg["ask"] * 100.0,
                                                                  params["max_theta_pct_per_day"]))
        if why:
            continue
        tick = c.tick_size
        slip = cfg["slippage_ticks"] * tick
        entry = round(leg["ask"] + slip, 2)
        ds_stop, ds_tgt = u_stop - spot, u_target - spot
        # planned stop on the premium: spot at the underlying stop, IV and time unchanged
        stop = _tick_ceil(project(leg, entry, ds_stop, 0.0, 0.0), tick)
        # conservative fill for sizing: IV falls, theta for the whole holding window,
        # then a gap allowance below the stop and exit slippage
        worst = project(leg, entry, ds_stop, -params["iv_shock_points"], hold_days)
        gap = (entry - min(stop, worst)) * params["gap_allowance_pct"] / 100.0
        sizing_stop = _tick_floor(min(stop, worst) - gap - slip, tick)
        # target: spot at the opposing liquidity, IV unchanged, theta for the holding window
        target = _tick_floor(project(leg, entry, ds_tgt, 0.0, hold_days), tick)
        row.update({"contract": c, "entry": entry, "premium_stop": stop, "sizing_stop": sizing_stop,
                    "premium_target": target, "hold_days": round(hold_days, 4)})
        if stop <= tick or stop >= entry:
            why.append("projected premium stop %.2f is not between one tick and the entry %.2f" % (stop, entry))
        elif sizing_stop <= 0:
            why.append("conservative stop estimate is not positive")
        elif target <= entry:
            why.append("projected target %.2f is not above the entry" % target)
        else:
            rr = (target - entry) / (entry - sizing_stop)
            row["reward_risk"] = round(rr, 3)
            if rr < cfg["min_reward_risk"]:
                why.append("reward:risk %.2f < %s (conservative stop)" % (rr, cfg["min_reward_risk"]))
        if not why:
            eligible.append(row)
    eligible.sort(key=lambda r: (r["spread_pct"], -r["oi"], abs(abs(r["delta"]) - params["delta_target"]),
                                 r["strike"]))
    for r in out:
        if r in eligible:
            r["verdict"] = "eligible (rank %d)" % (eligible.index(r) + 1)
        else:
            r["verdict"] = "rejected: " + "; ".join(r["reasons"])
    return (eligible[0] if eligible else None), out


def plan_for(row, cfg, day):
    """The exit plan in the shape exit.evaluate() uses, from a selected row."""
    c = row["contract"]
    flat = cfg["expiry_day_flat_by"] if c.expiry == day else cfg["flat_by"]
    return {"entry": row["entry"], "stop": row["premium_stop"], "target": row["premium_target"],
            "risk_points": round(row["entry"] - row["premium_stop"], 2),
            "reward_points": round(row["premium_target"] - row["entry"], 2),
            "reward_risk": row["reward_risk"], "flat_by": flat, "expiry": c.expiry.isoformat(),
            "sizing_stop": row["sizing_stop"],
            "note": ("stop and target are Greek-based ESTIMATES of the option premium at the underlying "
                     "stop/target, not execution prices; exits are judged on the live bid and gaps or IV "
                     "moves can fill below the stop")}
