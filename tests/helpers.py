import datetime as dt

from nse_bot import clock
from nse_bot.instruments import Contract
from nse_bot.quotes import Quote

IST = clock.IST


def t(h, m, s=0, day=dt.date(2026, 10, 29)):
    return dt.datetime.combine(day, dt.time(h, m, s), IST)


def contract(iid=101, typ="CE", strike=25000.0, expiry=dt.date(2026, 11, 3), lot=65, freeze=1820):
    return Contract(iid, "NIFTY03NOV26%s%d" % (typ[0], strike), "NIFTY", expiry, strike, typ, lot, 0.05, freeze)


def quote(bid=100.0, ask=100.5, at=None, qty=650, iid=101, volume=200000, oi=900000, ltp=None):
    return Quote(instrument_id=iid, ltp=ltp if ltp is not None else bid, bid=bid, ask=ask, bid_qty=qty,
                 ask_qty=qty, ts=at or t(10, 0), volume=volume, oi=oi)
