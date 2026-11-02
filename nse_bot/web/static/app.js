// NIFTY paper-bot dashboard. Shows only what the bot wrote; never computes a price or a signal.
"use strict";
var S = { csrf: "", mode: null, tab: "market", canStart: false, controls: false };

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function $(id) { return document.getElementById(id); }
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
function rs(v) {
  if (v === null || v === undefined || v === "") return "—";
  var n = Number(v);
  return (n < 0 ? "-" : "") + "₹" + Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function num(v, d) { return v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d === undefined ? 2 : d }); }
function sign(v) { return v === null || v === undefined ? "" : Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : ""; }
function hhmm(t) { if (!t) return "—"; var m = String(t).match(/T?(\d\d:\d\d(:\d\d)?)/); return m ? m[1] : String(t); }
function empty(n, msg) { clear(n).appendChild(el("div", "empty", msg)); }
function pill(text, kind) { return el("span", "pill " + (kind || ""), text); }
function statusKind(s) { s = String(s || ""); return s === "OK" || s === "Running" ? "ok" : s.indexOf("MISSING") >= 0 || s === "Error" ? "bad" : "warn"; }

function api(path, opts) {
  opts = opts || {};
  opts.credentials = "same-origin";
  opts.headers = opts.headers || {};
  if (opts.method === "POST") { opts.headers["X-CSRF-Token"] = S.csrf; opts.headers["Content-Type"] = "application/json"; }
  var sep = path.indexOf("?") < 0 ? "?" : "&";
  var url = path.indexOf("/api/control") === 0 || path === "/api/session" || path === "/api/market" || path === "/api/reports"
    ? path : path + sep + "mode=" + encodeURIComponent(S.mode || "standard");
  return fetch(url, opts).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (r.status === 401) { $("hb-text").textContent = "signed out: open the Telegram link again"; throw new Error("401"); }
      j._status = r.status; return j;
    });
  });
}

function stat(parent, label, value, cls) {
  var s = el("div", "stat");
  s.appendChild(el("div", "label", label));
  s.appendChild(el("div", "v " + (cls || ""), value));
  parent.appendChild(s);
}
function item(parent, head, right, lines, rightCls) {
  var it = el("div", "item"), h = el("div", "h");
  h.appendChild(el("span", "", head));
  if (right !== undefined && right !== null) h.appendChild(typeof right === "object" ? right : el("span", rightCls || "", right));
  it.appendChild(h);
  (lines || []).forEach(function (l) { if (l) it.appendChild(el("div", "small muted", l)); });
  parent.appendChild(it);
}

// ---- market + overview ----------------------------------------------------------
function renderMarket(m) {
  if (!m || !m.available) {
    $("spot").textContent = "—"; clear($("spot-status")).textContent = "NO DATA";
    $("market-note").textContent = (m && m.reason) || "no live data";
    empty($("opts"), "No FYERS options snapshot yet. Nothing is shown rather than a guess.");
    clear($("spark")); return;
  }
  $("spot").textContent = num(m.spot);
  var st = $("spot-status"); st.className = "pill " + statusKind(m.spot_status); st.textContent = m.spot_status;
  $("spot-time").textContent = "snapshot " + hhmm(m.snapshot_time);
  $("market-note").textContent = m.source + " · ATM " + num(m.atm_strike, 0) + " · API calls today " + num(m.api_calls_today, 0);
  var o = clear($("opts"));
  (m.options || []).forEach(function (x) {
    var c = el("div", "opt " + (x.type === "CE" ? "ce" : "pe"));
    var top = el("div", "row between");
    top.appendChild(el("span", "k", num(x.strike, 0) + " " + x.type));
    top.appendChild(pill(x.moneyness, ""));
    c.appendChild(top);
    c.appendChild(el("div", "p", x.ltp === null ? "—" : num(x.ltp)));
    var ba = el("div", "ba"); ba.appendChild(el("span", "", "bid " + num(x.bid))); ba.appendChild(el("span", "", "ask " + num(x.ask)));
    c.appendChild(ba);
    c.appendChild(el("div", "small muted", "vol " + num(x.volume, 0) + " · OI " + num(x.oi, 0)));
    c.appendChild(pill(x.status, statusKind(x.status)));
    o.appendChild(c);
  });
  var pts = (m.spot_history || []).map(function (p) { return p.spot; }).filter(function (v) { return v !== null; });
  var svg = clear($("spark"));
  if (pts.length > 1) {
    var lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts), span = hi - lo || 1, d = "";
    pts.forEach(function (v, i) { d += (i ? "L" : "M") + (i * 300 / (pts.length - 1)).toFixed(1) + " " + (56 - (v - lo) / span * 52).toFixed(1); });
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", d); svg.appendChild(p);
  }
}

function renderOverview(j) {
  renderMarket(j.market);
  var h = j.health || {}, books = j.books || {}, dot = $("hb-dot");
  var orb = (books.ORB && books.ORB.bot) || {}, sw = (books.Sweep && books.Sweep.bot) || {};
  var running = orb.status === "Running" || sw.status === "Running";
  var err = orb.status === "Error" || sw.status === "Error";
  dot.className = "dot " + (err ? "bad" : running ? "ok" : "idle");
  $("hb-text").textContent = "ORB " + (orb.status || "no data") + " · Sweep " + (sw.status || "no data") +
    " · FYERS " + ((h.fyers || {}).connected ? "connected" : "no live data");
  var r = j.risk || {}, t = clear($("today"));
  stat(t, "Day P&L (both)", rs(r.day_pnl), sign(r.day_pnl));
  stat(t, "Realised", rs(r.realized_pnl), sign(r.realized_pnl));
  stat(t, "Unrealised", rs(r.unrealized_pnl), sign(r.unrealized_pnl));
  stat(t, "Loss room left", rs(r.remaining_loss_inr));
  stat(t, "Entries", (r.entries === undefined ? "—" : r.entries) + " / " + ((r.limits || {}).max_trades_per_day || "—"));
  stat(t, "SL exits", (r.stop_exits === undefined ? "—" : r.stop_exits) + " / " + ((r.limits || {}).max_stop_exits_per_day || "—"));
  stat(t, "Risk state", r.locked ? "LOCKED" : r.error ? "ERROR" : "open", r.locked || r.error ? "down" : "up");
  stat(t, "Open lots", (r.open_lots === undefined ? "—" : r.open_lots) + " / " + ((r.limits || {}).max_open_lots || "—"));
  renderPositions(books);
  renderRisk(r, books);
  renderHealth(j.health, books, j.controls);
}

function renderPositions(books) {
  var p = clear($("positions")), any = false;
  ["ORB", "Sweep"].forEach(function (b) {
    ((books[b] || {}).open_positions || []).forEach(function (x) {
      any = true;
      item(p, x.symbol + " · " + b, rs(x.unrealized_pnl), [
        "qty " + x.qty + " · entry " + num(x.entry_price) + " at " + hhmm(x.entry_time),
        "current bid " + num(x.current_bid) + " (" + hhmm(x.price_time) + ")" + (x.price_note ? " · " + x.price_note : ""),
        "stop " + num(x.stop) + " · target " + num(x.target)], sign(x.unrealized_pnl));
    });
  });
  if (!any) empty(p, "No open paper positions.");
}

function renderRisk(r, books) {
  var n = clear($("risk")), L = r.limits || {};
  if (r.error) { item(n, "Risk file error", pill("LOCKED", "bad"), [r.error]); return; }
  item(n, "Shared risk book " + (r.date || ""), pill(r.locked ? "LOCKED" : "open", r.locked ? "bad" : "ok"), [
    r.lock_reason ? "reason: " + r.lock_reason : r.file || null,
    "entries " + (r.entries || 0) + "/" + L.max_trades_per_day + " · SL exits " + (r.stop_exits || 0) + "/" + L.max_stop_exits_per_day +
      " · open lots " + (r.open_lots || 0) + "/" + L.max_open_lots,
    "realised " + rs(r.realized_pnl) + " · unrealised " + rs(r.unrealized_pnl) + " · loss room " + rs(r.remaining_loss_inr),
    "limits: capital " + rs(L.capital_inr) + " · max risk/trade " + rs(L.max_risk_per_trade_inr) + " · daily loss " +
      rs(L.max_daily_loss_inr) + " · profit stop " + rs(L.daily_profit_stop_inr) + " · real orders " + (L.live_orders_enabled ? "ON" : "disabled")]);
}

function renderHealth(h, books, c) {
  var n = clear($("health"));
  h = h || {};
  ["ORB", "Sweep"].forEach(function (b) {
    var bot = (books[b] || {}).bot || {};
    item(n, b + " bot", pill(bot.status || "no data", statusKind(bot.status)), [
      "heartbeat " + (bot.heartbeat ? String(bot.heartbeat).slice(0, 10) + " " + hhmm(bot.heartbeat) : "none"), bot.last_error ? "last error: " + bot.last_error : null]);
  });
  var f = h.fyers || {};
  item(n, "FYERS market data", pill(f.connected ? "connected" : "no live data", f.connected ? "ok" : "warn"),
    ["last snapshot " + (f.last_snapshot || "none"), f.detail ? "status: " + f.detail : null]);
  item(n, "Dashboard", pill("up", "ok"), ["bot files synced " + (h.state_synced_at || "unknown"), "real orders: disabled"]);
  var s = $("ctl-state");
  if (!c || !c.available) { s.textContent = "Controls not available in this deployment"; }
  else if (c.error) { s.textContent = "Controls unreadable: " + c.error; }
  else {
    var today = (h.now || "").slice(0, 10);
    s.textContent = c.halt_day === today ? "EMERGENCY HALT today: no new entries" :
      c.entries_paused ? "New entries PAUSED" : "New entries allowed (subject to every risk rule)";
    s.className = "ctl " + (c.halt_day === today || c.entries_paused ? "down" : "up");
  }
  $("btn-start").disabled = !S.canStart;
  ["btn-pause", "btn-resume", "btn-halt"].forEach(function (id) { $(id).disabled = !S.controls; });
}

// ---- other tabs -------------------------------------------------------------------
function renderSignals(j) {
  var o = clear($("orb"));
  (j.orb || []).forEach(function (x) { item(o, x.contract, pill(x.status, x.status === "CANDIDATE" ? "ok" : "warn"), [hhmm(x.time), x.reasons]); });
  if (!(j.orb || []).length) {
    (j.orb_forward_log || []).forEach(function (x) {
      item(o, x.contract + " (" + x.strategy + ")", pill(x.decision, x.decision === "ENTERED" ? "ok" : "warn"), [hhmm(x.time) + " · signal " + hhmm(x.signal_time), (x.reasons || []).join("; ")]);
    });
  }
  if (!o.firstChild) empty(o, "No ORB signal today.");
  var s = clear($("sweep"));
  (j.sweep || []).forEach(function (x) {
    item(s, x.underlying + " · " + (x.bias || "no bias"), pill(x.decision, x.decision === "TAKE" ? "ok" : "warn"),
      [hhmm(x.evaluated_at), x.skip_reason || x.why || null]);
  });
  if (!s.firstChild) empty(s, "No sweep decision today.");
  var hdiv = clear($("historical"));
  (j.historical || []).forEach(function (x) { item(hdiv, x.contract + (x.strategy ? " (" + x.strategy + ")" : ""), pill("HISTORICAL", ""), [hhmm(x.time) + " · " + (x.status || ""), x.reasons]); });
  $("hist-note").textContent = (j.historical || []).length ? j.historical[0].note : "";
  if (!hdiv.firstChild) empty(hdiv, "None.");
  var r = clear($("rejected"));
  (j.rejected || []).forEach(function (x) { item(r, x.contract, hhmm(x.time), [x.reasons]); });
  if (!r.firstChild) empty(r, "No rejected signal today.");
}

function renderTrades(j) {
  var n = clear($("trades")), rows = [];
  ["ORB", "Sweep"].forEach(function (b) { ((j[b] || {}).trades || []).forEach(function (t) { t._book = b; rows.push(t); }); });
  rows.sort(function (a, b) { return String(b.exit_time).localeCompare(String(a.exit_time)); });
  rows.slice(0, 100).forEach(function (t) {
    item(n, (t.symbol || t.instrument_id) + " · " + t._book, rs(t.net_pnl), [
      String(t.exit_time || "").slice(0, 10) + " · " + hhmm(t.entry_time) + " → " + hhmm(t.exit_time),
      "entry " + num(t.entry_price) + " · exit " + num(t.exit_price) + " · qty " + t.qty + " · " + (t.strategy || ""), t.exit_reason ? "exit: " + t.exit_reason : null], sign(t.net_pnl));
  });
  if (!rows.length) empty(n, "No completed paper trades yet.");
}

function renderPerf(j) {
  var n = clear($("perf")), fam = ((j.forward_report || {}).families) || {};
  ["ORB", "Sweep"].forEach(function (b) {
    var tot = ((j.by_book || {})[b] || {}).totals || {}, f = fam[b] || {};
    stat(n, b + " net P&L", rs(tot.realized_pnl), sign(tot.realized_pnl));
    stat(n, b + " trades / win%", (tot.completed_trades || 0) + " / " + (tot.win_rate_pct === null || tot.win_rate_pct === undefined ? "—" : tot.win_rate_pct + "%"));
    if (f.profit_factor !== undefined) stat(n, b + " profit factor", f.profit_factor === null ? "—" : f.profit_factor);
    if (f.max_drawdown_inr !== undefined) stat(n, b + " max drawdown", rs(f.max_drawdown_inr));
  });
  $("sample").textContent = (fam.ORB && fam.ORB.sample) || "";
}

function renderReports(j) {
  var n = $("report");
  n.textContent = j.latest_markdown || (j.latest && !j.latest.error ? JSON.stringify(j.latest, null, 1) : "No daily report yet.");
}

function renderLogs(j) {
  var n = clear($("logs"));
  (j.rows || []).forEach(function (x) { item(n, hhmm(x.time) + " · " + x.book + " · " + x.source, x.kind, [x.text]); });
  if (!n.firstChild) empty(n, "No log entries.");
}

// ---- controls -------------------------------------------------------------------
function control(action, body, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return;
  var m = $("ctl-msg"); m.textContent = "sending…";
  api("/api/control/" + action, { method: "POST", body: JSON.stringify(body || {}) }).then(function (j) {
    m.textContent = j.error ? "Not done: " + j.error : (j.note || "done");
    m.className = "small " + (j.error ? "down" : "up");
    refresh();
  }).catch(function (e) { m.textContent = "Not done: " + e.message; });
}

// ---- historical signal study (no fills, no P&L) ------------------------------------------
var SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, parent) {
  var e = document.createElementNS(SVGNS, tag);
  Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
  if (parent) parent.appendChild(e);
  return e;
}
var ST = { doc: null, day: null, chart: null };
function table(parent, head, rows) {
  var t = el("table", "t"), tr = el("tr");
  head.forEach(function (h) { tr.appendChild(el("th", "", h)); });
  t.appendChild(tr);
  rows.forEach(function (r) {
    var x = el("tr");
    r.forEach(function (c) { var td = el("td", c && c.na ? "na" : "", c && c.na ? c.na : c); x.appendChild(td); });
    t.appendChild(x);
  });
  parent.appendChild(t);
}
function renderStudy(j) {
  ST.doc = j;
  var list = clear($("study-list")), cov = clear($("study-cov")), rng = clear($("study-range")), tb = clear($("study-table")), au = clear($("study-audit"));
  clear($("study-months")); clear($("study-month-table"));
  if (!j || !j.available) { empty(list, (j && j.reason) || "no study"); $("pnl-status").textContent = "NOT AVAILABLE"; $("study-day").disabled = true; return; }
  if (j.note) $("study-note").textContent = j.note;
  var op = j.options_pnl || {};
  $("pnl-status").textContent = op.status || "NOT AVAILABLE";
  $("pnl-status").className = "big warn";
  clear($("pnl-trades")).textContent = "simulated option trades: " + (op.simulated_option_trades === undefined ? 0 : op.simulated_option_trades);
  $("pnl-reason").textContent = op.reason || "";
  $("pnl-next").textContent = op.what_will_fill_it || "";
  function d(v) { return v === undefined || v === null ? "—" : v; }
  var c = j.coverage || {}, ix = c.index || {}, i5 = c.index_5m_older || {}, fu = c.future || {}, rq = j.requested || {}, dl = j.index_daily || {};
  item(rng, "Requested", d(rq.from) + " → " + d(rq.to), ["Study generated " + d(j.generated)]);
  item(rng, "NIFTY index, daily", d(dl.sessions) + " sessions", [d(dl.first) + " → " + d(dl.last) + " (used for the trading calendar only)"]);
  item(rng, "NIFTY index, 1-minute", d(ix.sessions) + " sessions", [d(ix.first) + " → " + d(ix.last) + " · " + d(ix.bars) + " bars · ORB and Sweep"]);
  item(rng, "NIFTY index, 5-minute (older)", d(i5.sessions) + " sessions", [i5.sessions ? d(i5.first) + " → " + d(i5.last) + " · ORB only" : "FYERS served none older than the 1-minute history"]);
  item(rng, "NIFTY future, 1-minute" + (fu.symbol ? " (" + fu.symbol + ")" : ""), d(fu.sessions) + " sessions", [fu.sessions ? d(fu.first) + " → " + d(fu.last) + " · needed by Sweep (volume)" : (fu.note || "none")]);
  var NA = { na: "NOT AVAILABLE" };
  var fams = ["ORB", "Sweep"].map(function (f) { return (j[f] || {}).summary || null; });
  function cell(i, k) { var s = fams[i]; return s ? d(s[k]) : "not evaluable"; }
  table(tb, ["", "ORB", "Sweep"], [
    ["Sessions tested", cell(0, "sessions"), cell(1, "sessions")],
    ["Signals", cell(0, "signals"), cell(1, "signals")],
    ["CE / PE", fams[0] ? fams[0].CE + " / " + fams[0].PE : "—", fams[1] ? fams[1].CE + " / " + fams[1].PE : "—"],
    ["Signal days %", cell(0, "signal_days_pct"), cell(1, "signal_days_pct")],
    ["Option trades simulated", "0", "0"],
    ["Wins / losses", NA, NA], ["SL hits", NA, NA], ["Win rate", NA, NA], ["Net P&L", NA, NA],
    ["Max drawdown", NA, NA], ["Profit factor", NA, NA]]);
  (j.fetch_audit || []).forEach(function (r) { item(au, r.series + " " + r.size + " · " + r.from + " → " + r.to, r.result === "ok" ? pill(r.bars + " bars", "ok") : pill("REFUSED", "bad"), r.result === "ok" ? [] : [r.result]); });
  if (!(j.fetch_audit || []).length) empty(au, "no request log in this study file");
  var cal = j.calendar_added || {}, calTxt = Object.keys(cal).map(function (y) { return y + ": " + Object.keys(cal[y].holidays || {}).length + " non-trading weekdays, " + cal[y].source; }).join("\n");
  cov.textContent = "Source: " + d(j.source) +
    "\nORB days skipped: " + JSON.stringify((j.ORB || {}).skipped || {}) +
    "\nSweep furthest rule failed per day (rules in order): " + JSON.stringify((j.Sweep || {}).furthest_rule_failed || {}) +
    "\nSweep rule failing at the day's last check: " + JSON.stringify((j.Sweep || {}).funnel_last_failed_rule || {}) +
    "\nSweep days not evaluable: " + JSON.stringify((j.Sweep || {}).not_evaluable || {}) +
    "\nCalendar from observed sessions (years without an NSE circular in the bot):\n" + (calTxt || "none");
  monthBars(j);
  var all = [];
  ["ORB", "Sweep"].forEach(function (f) { ((j[f] || {}).signals || []).forEach(function (x) { all.push(x); }); });
  all.sort(function (a, b) { return String(b.signal_time).localeCompare(String(a.signal_time)); });
  if (!all.length) empty(list, "no signals in the covered period");
  all.slice(0, 400).forEach(function (x) {
    item(list, x.day + " " + hhmm(x.signal_time) + " · " + x.family, pill(x.option_type, x.option_type === "CE" ? "ok" : "bad"),
      ["Trigger " + num(x.trigger_close) + " · target strike " + num(x.target_strike, 0) + (x.entry_window ? " · entry " + hhmm(x.entry_window[0]) + "–" + hhmm(x.entry_window[1]) : ""),
       "Data " + (x.data || "1m") + (x.regime ? " · regime " + x.regime : "") + " · option fill/exit: NOT AVAILABLE · tap to chart"]);
    list.lastChild.style.cursor = "pointer";
    list.lastChild.addEventListener("click", function () { pickDay(x.day); window.scrollTo(0, $("study-day").getBoundingClientRect().top + window.scrollY - 80); });
  });
  var sel = clear($("study-day"));
  sel.disabled = !(j.chart_days || []).length;
  (j.chart_days || []).slice().reverse().forEach(function (dd) { var o = el("option", "", dd); o.value = dd; sel.appendChild(o); });
  if (!ST.day && j.chart_days && j.chart_days.length) pickDay(j.chart_days[j.chart_days.length - 1]);
  else if (ST.day) sel.value = ST.day;
}
function monthBars(j) {
  var svg = $("study-months"), months = {}, keys;
  ["ORB", "Sweep"].forEach(function (f) {
    var bm = ((j[f] || {}).summary || {}).by_month || {};
    Object.keys(bm).forEach(function (m) { months[m] = months[m] || { ORB: 0, Sweep: 0 }; months[m][f] = bm[m].signals; });
  });
  keys = Object.keys(months).sort();
  if (!keys.length) return;
  table($("study-month-table"), ["Month", "ORB", "Sweep", "Option P&L"], keys.slice().reverse().map(function (m) { return [m, months[m].ORB, months[m].Sweep, { na: "N/A" }]; }));
  var max = 1; keys.forEach(function (m) { max = Math.max(max, months[m].ORB, months[m].Sweep); });
  var W = 320, H = 140, top = 8, bot = 18, gw = (W - 20) / keys.length, bw = Math.max(2, Math.min(10, gw / 2 - 2));
  (max >= 4 ? [0, 0.5, 1] : [0, 1]).forEach(function (f) { var y = top + (H - top - bot) * (1 - f); sv("line", { x1: 20, x2: W, y1: y, y2: y, "class": "grid" }, svg); var t = sv("text", { x: 0, y: y + 3 }, svg); t.textContent = Math.round(max * f); });
  keys.forEach(function (m, i) {
    var x0 = 20 + i * gw + gw / 2;
    ["ORB", "Sweep"].forEach(function (f, k) {
      var h = (H - top - bot) * months[m][f] / max;
      sv("rect", { x: x0 - bw - 1 + k * (bw + 2), y: H - bot - h, width: bw, height: Math.max(h, 0), rx: 2, "class": k ? "b-sweep" : "b-orb" }, svg);
    });
    var hit = sv("rect", { x: 20 + i * gw, y: 0, width: gw, height: H, fill: "transparent" }, svg);
    hit.addEventListener("click", function () { $("study-months-tip").textContent = m + ": ORB " + months[m].ORB + ", Sweep " + months[m].Sweep + " signals"; });
    if (keys.length <= 12 || i % 2 === 0) { var t = sv("text", { x: x0, y: H - 4, "text-anchor": "middle" }, svg); t.textContent = "JanFebMarAprMayJunJulAugSepOctNovDec".substr((+m.slice(5, 7) - 1) * 3, 3) + (i === 0 || m.slice(5, 7) === "01" ? " " + m.slice(2, 4) : ""); }
  });
}
function pickDay(d) {
  ST.day = d; $("study-day").value = d;
  api("/api/study/chart?day=" + encodeURIComponent(d)).then(drawCandles);
}
function drawCandles(j) {
  var svg = clear($("study-chart")), sig = clear($("chart-sig"));
  ST.chart = j;
  if (!j || !j.available || !(j.bars || []).length) { $("chart-tip").textContent = (j && j.reason) || "no bars"; return; }
  var b = j.bars, W = 360, H = 220, L = 34, top = 6, bot = 16, lo = Infinity, hi = -Infinity;
  b.forEach(function (x) { lo = Math.min(lo, x[3]); hi = Math.max(hi, x[2]); });
  var pad = (hi - lo) * 0.05 || 1; lo -= pad; hi += pad;
  var bw = (W - L) / b.length;
  function X(i) { return L + bw * i + bw / 2; }
  function Y(v) { return top + (H - top - bot) * (hi - v) / (hi - lo); }
  for (var g = 0; g <= 4; g++) { var v = lo + (hi - lo) * g / 4, y = Y(v); sv("line", { x1: L, x2: W, y1: y, y2: y, "class": "grid" }, svg); var t = sv("text", { x: 0, y: y + 3 }, svg); t.textContent = Math.round(v); }
  b.forEach(function (x, i) {
    var cls = x[4] >= x[1] ? "c-up" : "c-dn";
    sv("line", { x1: X(i), x2: X(i), y1: Y(x[2]), y2: Y(x[3]), "class": cls, "stroke-width": 1 }, svg);
    sv("rect", { x: X(i) - Math.max(1, bw * 0.35), y: Math.min(Y(x[1]), Y(x[4])), width: Math.max(1, bw * 0.7), height: Math.max(1, Math.abs(Y(x[1]) - Y(x[4]))), "class": cls }, svg);
    if (i % 12 === 0) { var tt = sv("text", { x: X(i), y: H - 3, "text-anchor": "middle" }, svg); tt.textContent = hhmm(x[0]).slice(0, 5); }
  });
  (j.signals || []).forEach(function (s) {
    var i = -1, ts = String(s.signal_time);
    b.forEach(function (x, k) { if (String(x[0]) < ts) i = k; });
    if (i < 0) return;
    var up = s.option_type === "CE", y = up ? Y(b[i][3]) + 12 : Y(b[i][2]) - 12, x = X(i);
    sv("path", { d: up ? "M" + x + " " + (y - 8) + "l6 10h-12z" : "M" + x + " " + (y + 8) + "l6 -10h-12z", "class": "mk " + (s.family === "ORB" ? "b-orb" : "b-sweep") }, svg);
    if (s.target_strike) sv("line", { x1: L, x2: W, y1: Y(s.trigger_close), y2: Y(s.trigger_close), "class": "xh" }, svg);
    sig.appendChild(el("div", "", s.family + " " + s.option_type + " at " + hhmm(s.signal_time) + " · trigger " + num(s.trigger_close) + " · target strike " + num(s.target_strike, 0) +
      (s.entry_window ? " · entry window " + hhmm(s.entry_window[0]) + "–" + hhmm(s.entry_window[1]) : "")));
  });
  var xh = sv("line", { x1: 0, x2: 0, y1: top, y2: H - bot, "class": "xh", visibility: "hidden" }, svg);
  function at(ev) {
    var r = svg.getBoundingClientRect(), p = ev.touches ? ev.touches[0] : ev;
    var i = Math.floor(((p.clientX - r.left) / r.width * W - L) / bw);
    if (i < 0 || i >= b.length) return;
    var x = b[i];
    xh.setAttribute("x1", X(i)); xh.setAttribute("x2", X(i)); xh.setAttribute("visibility", "visible");
    $("chart-tip").textContent = hhmm(x[0]).slice(0, 5) + "  O " + num(x[1]) + "  H " + num(x[2]) + "  L " + num(x[3]) + "  C " + num(x[4]);
    if (ev.cancelable && ev.touches) ev.preventDefault();
  }
  ["mousemove", "touchstart", "touchmove", "click"].forEach(function (k) { svg.addEventListener(k, at, { passive: false }); });
}

// ---- loop -----------------------------------------------------------------------
var loaders = {
  market: function () { return api("/api/overview").then(renderOverview); },
  signals: function () { return api("/api/signals").then(renderSignals); },
  positions: function () { return Promise.all([api("/api/overview").then(renderOverview), api("/api/trades").then(renderTrades)]); },
  perf: function () { return Promise.all([api("/api/overview").then(renderOverview), api("/api/performance").then(renderPerf), api("/api/reports").then(renderReports)]); },
  study: function () { return ST.doc ? Promise.resolve() : api("/api/study").then(renderStudy); },
  control: function () { return Promise.all([api("/api/overview").then(renderOverview), api("/api/logs").then(renderLogs)]); }
};
var busy = false;
function refresh() {
  if (busy || document.visibilityState === "hidden") return;
  busy = true;
  (loaders[S.tab] || loaders.market)().catch(function () {}).then(function () { busy = false; });
}
function show(tab) {
  S.tab = tab;
  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) { t.classList.toggle("active", t.id === "tab-" + tab); });
  Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (b) { b.classList.toggle("on", b.getAttribute("data-tab") === tab); });
  window.scrollTo(0, 0);
  refresh();
}

document.addEventListener("DOMContentLoaded", function () {
  Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (b) {
    b.addEventListener("click", function () { show(b.getAttribute("data-tab")); });
  });
  $("study-day").addEventListener("change", function () { pickDay(this.value); });
  $("mode").addEventListener("change", function () { S.mode = this.value; refresh(); });
  $("btn-start").addEventListener("click", function () { control("start", { book: $("book").value }, "Start a paper-trading run on GitHub Actions? (paper only)"); });
  $("btn-pause").addEventListener("click", function () { control("pause", {}, "Stop new entries? Open positions keep being managed."); });
  $("btn-resume").addEventListener("click", function () { control("resume", {}); });
  $("btn-halt").addEventListener("click", function () { control("halt", { confirm: "HALT" }, "EMERGENCY HALT: block all new entries for the rest of today? This cannot be undone today."); });
  document.addEventListener("visibilitychange", refresh);
  api("/api/session").then(function (j) {
    S.csrf = j.csrf; S.mode = j.mode; S.canStart = j.can_start; S.controls = j.controls;
    $("mode").value = j.mode;
    refresh();
    setInterval(refresh, 15000);
  }).catch(function () {});
});
