# Mobile dashboard (iPhone Chrome), ₹0

Flask app in `nse_bot/web/`, reading only files the paper bot writes (ledger, shared risk book, alerts,
Telegram message log, decisions, forward report, EOD report, live FYERS recorder). Nothing is computed
or made up. Controls (start paper-day, pause/resume new entries, emergency halt) can only block NEW
entries; open positions keep being managed. The paper loop reads them from the `bot-control` branch
each cycle (`nse_bot/control.py`) and pauses entries if it cannot read them 3 cycles in a row.

Run locally: `pip install -r requirements-dashboard.txt`, then
`DASH_KEY=<32+ random chars> DASH_STATE_ROOT=state python3 -m nse_bot.web.app` and open
`http://127.0.0.1:8080/login?k=<key>`.

Hosting (`.github/workflows/dashboard.yml`, manual only): Flask inside a GitHub Actions job behind a
free Cloudflare quick tunnel (HTTPS on trycloudflare.com); the sign-in link goes only to Telegram.
Up only while a run is going (max 6 h per job), new address each run, no uptime guarantee.
