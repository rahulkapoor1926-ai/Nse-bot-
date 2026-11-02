"""Print the layout of FYERS's public symbol master files (no login): row count, column
count and a few NIFTY rows, so the parser is written against the real file, not a guess."""

import csv
import io
import json
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) nse-bot-readonly-check/1.0"
for url in ("https://public.fyers.in/sym_details/NSE_FO.csv", "https://public.fyers.in/sym_details/NSE_CM.csv",
            "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
        print("== %s: HTTP 200, %d bytes" % (url, len(body)))
    except Exception as e:
        print("== %s: FAILED %s" % (url, e))
        continue
    if url.endswith(".json"):
        doc = json.loads(body)
        items = list(doc.items())[:400] if isinstance(doc, dict) else []
        for k, v in [kv for kv in items if "NIFTY2" in kv[0] and "BANK" not in kv[0]][:2]:
            print("   %s -> %s" % (k, json.dumps(v)[:900]))
        continue
    rows = list(csv.reader(io.StringIO(body)))
    print("   rows %d, columns per row %s" % (len(rows), sorted({len(r) for r in rows[:2000]})))
    print("   first row: %s" % rows[0])
    shown = 0
    for r in rows:
        if any(c.startswith("NSE:NIFTY") and "BANK" not in c and "FIN" not in c and "MID" not in c and "NXT" not in c
               for c in r):
            print("   nifty row: %s" % r)
            shown += 1
            if shown >= 4:
                break
