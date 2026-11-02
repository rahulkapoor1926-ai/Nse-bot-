"""Read-only check of which free NSE end-of-day files are reachable from this machine.

No credentials, no trading. For each candidate URL: HTTP status, size, and for CSV/ZIP
files the header row, row count and the NIFTY rows that would be usable. Candidate URLs
come from NSE's own archive host; any that fail are reported as failed, never assumed.
"""

import csv
import datetime as dt
import io
import sys
import urllib.error
import urllib.request
import zipfile

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BASE = "https://nsearchives.nseindia.com"


def candidates(d):
    mon = d.strftime("%b").upper()
    return [
        ("F&O bhavcopy (UDiFF)", "%s/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip" % (BASE, d.strftime("%Y%m%d"))),
        ("F&O bhavcopy (old)", "%s/content/historical/DERIVATIVES/%s/%s/fo%s%s%sbhav.csv.zip" % (
            BASE, d.year, mon, d.strftime("%d"), mon, d.year)),
        ("Cash bhavcopy (UDiFF)", "%s/content/cm/BhavCopy_NSE_CM_0_0_0_%s_F_0000.csv.zip" % (BASE, d.strftime("%Y%m%d"))),
        ("Index close", "%s/content/indices/ind_close_all_%s.csv" % (BASE, d.strftime("%d%m%Y"))),
    ]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Referer": "https://www.nseindia.com/"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # network refusal, timeout, TLS
        return "error: %s" % type(e).__name__, b""


def rows(body, url):
    if url.endswith(".zip"):
        z = zipfile.ZipFile(io.BytesIO(body))
        body = z.read(z.namelist()[0])
    return list(csv.reader(io.StringIO(body.decode("utf-8", "replace"))))


def main(days):
    for d in days:
        print("=== %s" % d.isoformat())
        for name, url in candidates(d):
            status, body = fetch(url)
            print("%-22s %s  %d bytes  %s" % (name, status, len(body), url))
            if status != 200 or not body:
                continue
            try:
                rs = rows(body, url)
            except Exception as e:
                print("   unreadable: %s" % e)
                continue
            head = [h.strip() for h in rs[0]]
            print("   columns (%d): %s" % (len(head), ", ".join(head)))
            nifty = [r for r in rs[1:] if any(c.strip() in ("NIFTY", "Nifty 50") for c in r)]
            print("   rows: %d total, %d mention NIFTY / Nifty 50" % (len(rs) - 1, len(nifty)))
            for r in nifty[:3]:
                print("   sample: %s" % dict(zip(head, [c.strip() for c in r])))


if __name__ == "__main__":
    days = [dt.date.fromisoformat(a) for a in sys.argv[1:]] or [dt.date.today() - dt.timedelta(days=3)]
    main(days)
