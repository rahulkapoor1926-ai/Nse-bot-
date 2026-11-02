"""Describe the SHAPE of the FYERS_AUTH_CODE secret without revealing it: length, whether it is a JWT,
stray characters, and (for a JWT) only its issue/expiry times and whether it was issued for our app.
Never prints the code or any part of it. Exit 0 always: this is a diagnostic, the login step decides."""

import base64
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from nse_bot.broker.fyers import auth_code_from  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def when(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), IST).strftime("%Y-%m-%d %H:%M IST")
    except (TypeError, ValueError):
        return "not present"


def main():
    raw = os.environ.get("FYERS_AUTH_CODE") or ""
    code = auth_code_from(raw)
    print("secret set: %s; length %d; surrounding whitespace: %s; looks like a pasted URL: %s"
          % (bool(raw), len(raw), raw != raw.strip(), "auth_code=" in raw))
    parts = code.split(".")
    print("extracted code: length %d; JWT shape (3 parts): %s" % (len(code), len(parts) == 3))
    if len(parts) != 3:
        return 0
    try:
        p = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(p.encode()))
    except Exception:                                    # noqa: BLE001
        print("JWT payload not readable")
        return 0
    now = dt.datetime.now(IST)
    exp = claims.get("exp")
    print("issued: %s; expires: %s; now: %s; expired: %s" % (
        when(claims.get("iat")), when(exp), now.strftime("%Y-%m-%d %H:%M IST"),
        (int(exp) < now.timestamp()) if exp else "unknown"))
    app = (os.environ.get("FYERS_APP_ID") or "").strip()
    sub = str(claims.get("app_id") or claims.get("sub") or "")
    if app and sub:
        print("issued for this app id: %s" % (app.startswith(sub) or sub.startswith(app.split("-")[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
