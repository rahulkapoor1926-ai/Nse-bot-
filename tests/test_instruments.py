import datetime as dt
import unittest

from nse_bot import instruments
from nse_bot.errors import DataError

HEADER = ("id,isin,symbol,company_name,series,underlying_symbol,exchange,segment,token,instrument_type,"
          "option_type,expiry_date,strike_price,lot_size,tick_size,freeze_quantity,price_precision,price_multiplier")
# Sample rows exactly as published at https://docs.sahi.com/api/v1/reference/scrip-master/
DOC_ROWS = """10854,,,Nifty 50,,,NSE,NSE,26000,IDX,,,0.00,0,0.00,0,0,0
12027,INE002A01018,RELIANCE,Reliance Industries,EQ,,NSE,NSE,2885,STK,,,0.00,1,0.10,0,2,1
1914891,,NIFTY27OCT26C25000,,,NIFTY,NSE,NFO,51440,OPTIDX,CE,27-10-2026,25000.00,65,0.05,3510,2,1
1914890,,NIFTY27OCT26P25000,,,NIFTY,NSE,NFO,51441,OPTIDX,PE,27-10-2026,25000.00,65,0.05,3510,2,1
1963491,,RELIANCE23NOV26F,,,RELIANCE,NSE,NFO,61697,FUTSTK,,23-11-2026,-0.01,500,0.10,20000,2,1"""


def chain_rows():
    rows, i = [], 3000000
    for k in range(24800, 25250, 50):
        for t in ("CE", "PE"):
            i += 1
            rows.append("%d,,NIFTY03NOV26%s%d,,,NIFTY,NSE,NFO,1,OPTIDX,%s,03-11-2026,%d.00,65,0.05,1820,2,1"
                        % (i, t[0], k, t, k))
    return "\n".join(rows)


class Master(unittest.TestCase):
    def test_doc_sample(self):
        m = instruments.parse(HEADER + "\n" + DOC_ROWS + "\n", index_names=("Nifty 50",))
        self.assertEqual(len(m.contracts), 2)
        c = m.by_id(1914891)
        self.assertEqual((c.underlying, c.expiry, c.strike, c.option_type, c.lot_size, c.tick_size),
                         ("NIFTY", dt.date(2026, 10, 27), 25000.0, "CE", 65, 0.05))
        self.assertEqual(m.index_id("Nifty 50"), 10854)
        with self.assertRaises(DataError):
            m.index_id("Nifty Bank")

    def test_pick_atm_and_offsets(self):
        m = instruments.parse(HEADER + "\n" + chain_rows())
        e = dt.date(2026, 11, 3)
        self.assertEqual(m.pick("NIFTY", e, "CE", 25012, 0).strike, 25000)
        self.assertEqual(m.pick("NIFTY", e, "CE", 25012, 1).strike, 25050)
        self.assertEqual(m.pick("NIFTY", e, "PE", 25012, 1).strike, 24950)
        with self.assertRaises(DataError):
            m.pick("NIFTY", e, "CE", None)
        with self.assertRaises(DataError):
            m.pick("NIFTY", e, "CE", 25012, 50)

    def test_bad_rows_rejected_not_patched(self):
        bad = "\n".join([
            "1,,X,,,NIFTY,NSE,NFO,1,OPTIDX,CE,03-11-2026,abc,65,0.05,1820,2,1",
            "2,,X,,,NIFTY,NSE,NFO,1,OPTIDX,XX,03-11-2026,25000,65,0.05,1820,2,1",
            "3,,X,,,NIFTY,NSE,NFO,1,OPTIDX,CE,03-11-2026,25000,0,0.05,1820,2,1",
            "4,,X,,,NIFTY,NSE,NFO,1,OPTIDX,CE,03-11-2026,25000,65,0.05,1820,2,1",
            "4,,X,,,NIFTY,NSE,NFO,1,OPTIDX,PE,03-11-2026,25000,65,0.05,1820,2,1",
        ])
        m = instruments.parse(HEADER + "\n" + bad)
        self.assertEqual(len(m.contracts), 1)
        self.assertEqual(len(m.rejected), 4)

    def test_missing_columns_and_mixed_lots(self):
        with self.assertRaises(DataError):
            instruments.parse("id,symbol\n1,X\n")
        mixed = chain_rows().replace(",65,0.05", ",75,0.05", 1)
        with self.assertRaises(DataError):
            instruments.parse(HEADER + "\n" + mixed)
