"""Indian market news: RSS 2.0 / Atom feeds you choose, plus scheduled events.

Feeds are listed in a JSON file ({"feeds": [{"name", "url"}]}); nothing is
fetched from a source you did not list. Each item keeps its source link and an
IST timestamp. Items mentioning the index, banks, RBI, policy rates, budget,
inflation or GDP are tagged. News is context for the trader; it never triggers
or approves a trade. Only scheduled high-impact events (events.py) can block
entries, through the event blackout.
"""

import datetime as dt
import email.utils
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

from .clock import IST
from .errors import DataError

TAGS = {
    "nifty": r"\bnifty\b|\bsensex\b|\bindex\b",
    "banks": r"\bbank ?nifty\b|\bbanks?\b|\bhdfc\b|\bicici\b|\bsbi\b|\bkotak\b|\baxis\b",
    "rbi": r"\brbi\b|\brepo\b|\bmonetary policy\b|\bmpc\b",
    "macro": r"\bcpi\b|\binflation\b|\bgdp\b|\bbudget\b|\biip\b|\bfiscal\b",
    "flows": r"\bfii\b|\bfpi\b|\bdii\b",
    "global": r"\bfed\b|\bcrude\b|\bdollar\b|\brupee\b",
}
UA = "nse-bot/0.1 (personal research tool)"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        t = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if t is None or t.tzinfo is None:
        return None
    return t.astimezone(IST)


def parse_feed(data, source):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise DataError("feed %s is not valid XML: %s" % (source, e))
    items = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        f = {_local(c.tag): c for c in el}
        title = (f["title"].text or "").strip() if "title" in f else ""
        link = ""
        if "link" in f:
            link = (f["link"].text or f["link"].get("href") or "").strip()
        when = None
        for k in ("pubDate", "published", "updated"):
            if k in f:
                when = parse_time(f[k].text)
                if when:
                    break
        if not title or not link:
            continue
        items.append({"title": title, "link": link, "published": when, "source": source,
                      "timestamp_known": when is not None})
    return items


def tag(items):
    for it in items:
        t = it["title"].lower()
        it["tags"] = sorted(k for k, rx in TAGS.items() if re.search(rx, t))
    return items


def dedupe(items):
    seen, out = set(), []
    for it in items:
        k = it["link"].split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def collect(feeds, now, hours=24, fetcher=fetch):
    """Returns (items newest first within `hours`, errors). One failing feed does not stop the rest."""
    items, errors = [], []
    for f in feeds:
        try:
            items.extend(parse_feed(fetcher(f["url"]), f["name"]))
        except Exception as e:           # network, HTTP, XML: report and continue
            errors.append("%s: %s" % (f.get("name"), e))
    cut = now - dt.timedelta(hours=hours)
    keep = [i for i in dedupe(tag(items)) if i["published"] is None or i["published"] >= cut]
    keep.sort(key=lambda i: i["published"] or dt.datetime.min.replace(tzinfo=IST), reverse=True)
    return keep, errors


def load_feeds(path):
    try:
        with open(path, encoding="utf-8") as f:
            feeds = json.load(f)["feeds"]
    except (OSError, ValueError, KeyError) as e:
        raise DataError("cannot read feeds file %s: %s" % (path, e))
    for f in feeds:
        if not str(f.get("url", "")).startswith("https://") or not f.get("name"):
            raise DataError("each feed needs a name and an https url: %r" % f)
    return feeds
