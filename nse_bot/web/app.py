"""Flask dashboard for the PAPER bot (iPhone Chrome friendly). Read-only views plus three controls
that can only block NEW entries; it never places, sizes or closes a trade and holds no broker token.

Security
  - Every page and API needs a session. A session is opened only by /login?k=<DASH_KEY>, the
    one-time link the job sends to your private Telegram chat. DASH_KEY is random per deployment,
    compared in constant time, and never printed (masked in the job log). The URL is cleaned
    straight after login and Referrer-Policy is no-referrer, so the key does not leak onwards.
  - Controls are POST only, need the session's CSRF token, and are recorded with a time.
  - Cookies: HttpOnly, Secure, SameSite=Strict. Strict CSP: no inline script, no third-party code.
  - The app never reads FYERS or Telegram credentials; GITHUB_TOKEN (the job's own, short-lived)
    is used server-side only, to publish controls and start a paper run.

Environment
  DASH_KEY             required login key
  DASH_STATE_ROOT      checkout of the paper-state branch (or a local state/ folder)
  DASH_RECORDINGS      folder of the live FYERS recorder (optional)
  DASH_LIVE_FEED       folder of the FYERS WebSocket feed, live_feed.py (optional)
  DASH_DAY_AUDIT       folder of day-audit replays, <day>.json (optional)
  DASH_CONTROL_DIR     git worktree of the bot-control branch (optional; enables controls)
  DASH_CONFIG          bot config (default config/paper.example.json)
  GITHUB_REPOSITORY / GITHUB_TOKEN   for "start paper run" (optional)
"""

import datetime as dt
import hmac
import json
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.request

from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session

from .. import clock, config, control, state
from .data import MODES, View

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BAD_LOGINS = 20


def _publish_git(control_dir, doc):
    """Write control.json in the bot-control worktree, commit and push (retries)."""
    path = os.path.join(control_dir, control.FILE)
    state.write_json(path, doc)
    run = lambda *a: subprocess.run(["git", "-C", control_dir] + list(a), capture_output=True, text=True, timeout=60)
    run("add", control.FILE)
    run("commit", "-q", "-m", "dashboard control %s" % doc.get("updated_at"))
    for _ in range(4):
        if run("push", "-q", "origin", "HEAD:%s" % control.BRANCH).returncode == 0:
            return True
        run("pull", "-q", "--rebase", "origin", control.BRANCH)
    return False


def _github(method, path, token, body=None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer %s" % token,
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return r.status, json.loads(raw) if raw else None


def create_app(env=None, publish=None, github=None, now=clock.now_ist):
    env = dict(os.environ if env is None else env)
    key = env.get("DASH_KEY") or ""
    if len(key) < 24:
        raise RuntimeError("DASH_KEY must be set (at least 24 random characters)")
    cfg = config.load(env.get("DASH_CONFIG", "config/paper.example.json"))
    root = env.get("DASH_STATE_ROOT", "state")
    control_dir = env.get("DASH_CONTROL_DIR")
    view = View(root, cfg, env.get("DASH_RECORDINGS"),
                os.path.join(control_dir, control.FILE) if control_dir else None, now=now,
                study_path=env.get("DASH_STUDY"), live_feed=env.get("DASH_LIVE_FEED"),
                day_audit_dir=env.get("DASH_DAY_AUDIT"))
    publish = publish or (lambda doc: _publish_git(control_dir, doc))
    github = github or _github
    repo, gtoken = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_TOKEN")

    app = Flask(__name__, static_folder=None)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=env.get("DASH_INSECURE") != "1",
                      SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12))
    bad = {"n": 0}

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                                   "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                                                   "base-uri 'none'; form-action 'self'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Strict-Transport-Security"] = "max-age=86400"
        return resp

    def authed():
        return session.get("ok") is True

    @app.before_request
    def guard():
        if request.endpoint in ("login", "healthz", "static_files"):
            return None
        if not authed():
            if request.path.startswith("/api/"):
                return jsonify({"error": "not signed in: open the link from your Telegram message"}), 401
            return LOCKED_PAGE, 401, {"Content-Type": "text/html; charset=utf-8"}
        if request.method == "POST":
            tok = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(tok, session.get("csrf", "")):
                return jsonify({"error": "bad or missing CSRF token"}), 403
        return None

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "app": "nse-bot paper dashboard", "live_orders_enabled": False})

    @app.get("/login")
    def login():
        if bad["n"] >= MAX_BAD_LOGINS:
            abort(429)
        k = request.args.get("k", "")
        if not hmac.compare_digest(k.encode(), key.encode()):
            bad["n"] += 1
            return LOCKED_PAGE, 401, {"Content-Type": "text/html; charset=utf-8"}
        session.clear()
        session.permanent = True
        session["ok"] = True
        session["csrf"] = secrets.token_urlsafe(24)
        return redirect("/", code=303)                       # drops the key from the address bar

    @app.get("/")
    def index():
        return send_from_directory(os.path.join(HERE, "static"), "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(os.path.join(HERE, "static"), name)

    def mode():
        m = request.args.get("mode") or view.active_mode()
        if m not in MODES:
            abort(400)
        return m

    @app.get("/api/session")
    def api_session():
        return jsonify({"csrf": session["csrf"], "mode": view.active_mode(), "modes": list(MODES),
                        "controls": bool(control_dir), "can_start": bool(repo and gtoken)})

    @app.get("/api/overview")
    def api_overview():
        m = mode()
        books = view.books(m)
        slim = {}
        for name, b in books.items():
            slim[name] = {k: b.get(k) for k in ("bot", "today", "totals", "by_strategy", "open_positions",
                                                 "ledger_integrity", "errors", "label", "missing", "error")}
        return jsonify({"mode": m, "books": slim, "risk": view.risk(m), "health": view.health(m),
                        "controls": view.controls(), "market": view.market()})

    @app.get("/api/market")
    def api_market():
        return jsonify(view.market())

    @app.get("/api/signals")
    def api_signals():
        return jsonify(view.signals(mode()))

    @app.get("/api/day-audit")
    def api_day_audit():
        return jsonify(view.day_audit(mode()))

    @app.get("/api/trades")
    def api_trades():
        m = mode()
        return jsonify({name: {"trades": b.get("trades", []), "equity_curve": b.get("equity_curve", []),
                               "days": b.get("days", [])} for name, b in view.books(m).items()})

    @app.get("/api/performance")
    def api_performance():
        return jsonify(view.performance(mode()))

    @app.get("/api/logs")
    def api_logs():
        return jsonify({"rows": view.logs(mode())})

    @app.get("/api/study")
    def api_study():
        return jsonify(view.study())

    @app.get("/api/study/trades.csv")
    def api_study_csv():
        text = view.study_csv()
        if text is None:
            return jsonify({"error": "this study has no trade outcomes yet"}), 404
        return text, 200, {"Content-Type": "text/csv; charset=utf-8",
                           "Content-Disposition": "attachment; filename=orb-index-model-trades.csv"}

    @app.get("/api/study/chart")
    def api_study_chart():
        day = request.args.get("day", "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            return jsonify({"error": "day must be YYYY-MM-DD"}), 400
        return jsonify(view.study_chart(day))

    @app.get("/api/reports")
    def api_reports():
        return jsonify(view.reports())

    def _set_controls(**changes):
        if not control_dir:
            return jsonify({"error": "controls are not configured in this deployment"}), 409
        cur = view.controls()
        if cur.get("error"):
            return jsonify({"error": cur["error"]}), 500
        doc = {k: cur.get(k) for k in control.DEFAULT}
        doc.update(changes)
        doc["updated_at"] = now().isoformat()
        ok = publish(doc)
        return jsonify({"ok": bool(ok), "controls": doc,
                        "note": "the paper bot reads this within one cycle (30 s); open positions keep being managed"
                        if ok else "could not publish the control: nothing changed for the bot"}), (200 if ok else 502)

    @app.post("/api/control/pause")
    def pause():
        return _set_controls(entries_paused=True, note="paused from the dashboard")

    @app.post("/api/control/resume")
    def resume():
        cur = view.controls()
        if cur.get("halt_day") == now().date().isoformat():
            return jsonify({"error": "an emergency halt is in force today; entries stay blocked until tomorrow"}), 409
        return _set_controls(entries_paused=False, note=None)

    @app.post("/api/control/halt")
    def halt():
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "HALT":
            return jsonify({"error": "confirmation missing"}), 400
        return _set_controls(entries_paused=True, halt_day=now().date().isoformat(),
                             note="EMERGENCY HALT from the dashboard")

    @app.post("/api/control/start")
    def start():
        if not (repo and gtoken):
            return jsonify({"error": "starting a paper run is not configured in this deployment"}), 409
        body = request.get_json(silent=True) or {}
        book = body.get("book") if body.get("book") in ("standard", "forward") else "standard"
        try:
            _, runs = github("GET", "/repos/%s/actions/workflows/paper-day.yml/runs?status=in_progress&per_page=1"
                             % repo, gtoken)
            if runs and runs.get("total_count"):
                return jsonify({"error": "a paper run is already in progress", "run": runs["workflow_runs"][0].get(
                    "html_url")}), 409
            code, _ = github("POST", "/repos/%s/actions/workflows/paper-day.yml/dispatches" % repo, gtoken,
                             {"ref": "main", "inputs": {"book": book}})
        except urllib.error.HTTPError as e:
            return jsonify({"error": "GitHub refused: HTTP %s" % e.code}), 502
        except Exception as e:                      # noqa: BLE001
            return jsonify({"error": "could not reach GitHub: %s" % str(e)[:120]}), 502
        return jsonify({"ok": code == 204, "book": book,
                        "note": "paper-day started (paper only). It stops at once on a non-trading day."})

    return app


LOCKED_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot dashboard</title>
<link rel="stylesheet" href="/static/app.css"></head><body><main class="locked"><h1>Bot dashboard</h1>
<p>Please open the sign-in link from your Telegram message.</p></main></body></html>"""


def main():
    host = os.environ.get("DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("DASH_PORT", "8080"))
    create_app().run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
