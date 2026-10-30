# Running the bot with only an iPhone

Checked on 2026-10-30. Live trading and order placement stay disabled; nothing
below changes the strategy, the 11 original skills or the project layout.

## What was verified

| check | result |
|---|---|
| Tests in the Claude cloud environment | Work. 153 NSE tests and 315 original-skill tests pass here. |
| Reaching Angel One from the Claude environment | Blocked. The network policy refuses `apiconnect.angelone.in`, `apiconnect.angelbroking.com`, `smartapi.angelone.in`, `smartapisocket.angelone.in` and the Angel scrip-master host (HTTP 403 on connect). |
| Reaching SAHI, NSE or Yahoo from the Claude environment | Blocked in the same way (`direct.sahi.com`, `www.nseindia.com`, `query1.finance.yahoo.com`). |
| Running continuously in the Claude environment | Not suitable. Each session is a temporary container that is reclaimed when idle. Work runs in turns, and a background command is stopped after at most 2 hours. Nothing keeps running after the chat goes quiet. |

The project owner can allow extra hosts for future sessions in
[Project settings](#project-settings/environment) → Cloud environment → Add
cloud environment → Network access → Allowed domains. That would let a session
fetch data during a chat. It still would not make a session run for a whole
trading day.

## Answers

1. **Can the bot run here?** Tests, backtests on data files, code changes and
   short paper-trading replays: yes. A live paper-trading day: no. There is no
   broker network access and no persistent process.
2. **Live Angel One data here?** No, the hosts are blocked. Angel One's
   SmartAPI itself is free, including its historical data. Its historical data
   covers NSE equity and NFO futures; options history is "being evaluated"
   ([Angel One](https://www.angelone.in/knowledge-center/smartapi/detailed-introduction-to-smartapi)).
3. **Continuously during market hours here?** No.
4. **Completely free?** Yes, but on a different free host (below). Each has a
   catch.
5. **What to do from the iPhone:** see the two options below.

## Free hosting options that work from an iPhone

### Option A: GitHub Actions (simplest; done in Safari)

- Free for **public** repositories ([GitHub](https://docs.github.com/en/actions/reference/usage-limits-billing-and-administration)).
  In a private repository a free account's included minutes would not cover
  ~375 minutes a day for every trading day.
- A job can run for at most **6 hours** ([GitHub limits](https://docs.github.com/en/actions/reference/limits)).
  The NSE session is 6 h 15 min, so the day is split into two scheduled jobs
  (09:05–12:15 and 12:10–15:35 IST).
- Scheduled starts can run a few minutes late. Risk state and the paper ledger
  are saved back to the repository between jobs, so a restart resumes correctly.
- Catch: the code and the paper results are public. API keys stay private in
  GitHub Secrets.

### Option B: Oracle Cloud Always Free VM (true 24/7)

- A small Linux server that is free indefinitely. Sign-up needs a **Visa or
  Mastercard credit card** for a ~₹100 verification hold that is reversed.
  RuPay, prepaid and most debit cards are refused
  ([guide](https://dev.to/ashish-codejourney/getting-a-free-oracle-cloud-vps-from-india-the-full-guide-and-the-rupay-wall-nobody-warns-you-4p5o)).
- You manage it from the iPhone with a free SSH app (for example Termius) and
  follow `docs/nse/DEPLOY.md`.
- Catch: more setup steps, and the card requirement.

## Step by step for Option A (you chose this on 2026-10-30)

The workflow is ready in `.github/workflows/paper-day.yml` (plus `tests.yml`,
which runs every test on each push).

1. In Safari, create a free GitHub account, then create an empty **public**
   repository (for example `nse-trading-bot`). Leave "Add a README" unticked.
2. Open [Connect GitHub](https://claude.ai/connect-github) and give Claude
   access to that repository, then tell me its name. I push the project. You
   type nothing technical.
3. Create a SAHI API key with only the `marketdata.read` scope. Do not paste
   anything from it into chat.
4. In the repository on GitHub: Settings → Secrets and variables → Actions →
   New repository secret. Add `SAHI_KEY_ID` and `SAHI_KEY_SECRET` (and
   `SAHI_TOTP` only if SAHI asks for one).
5. Each trading morning before 09:05 IST, approve the API key in the SAHI app
   (SAHI tokens expire at 06:00 IST every day).
6. To try it at any time: Actions tab → paper-day → Run workflow. On a holiday
   or weekend it stops at once.
7. Results: the job log (Actions tab) shows the SIMULATED report, and the
   `paper-state` branch holds the ledger, risk lockout and recordings. I can
   read and summarise them for you here.

How the day runs: one job at 09:05 IST that stops at 12:15, and one at 12:10
IST that waits for the first to finish and stops after the close. Each job
runs the tests first, then `record` (builds real bid/ask history for the
validation gate) and `paper` (stock/ETF strategy first, then options). Until a
strategy passes the 10,000-example gate, `paper` only alerts candidates and
never enters a simulated trade. State is saved even if a job fails.

Things to know:
- GitHub can start scheduled jobs late, sometimes by 10–30 minutes at busy times.
- GitHub pauses schedules in a public repository after 60 days without
  activity; the Actions tab then shows a button to re-enable them.
- Not yet verified: whether SAHI accepts API calls from GitHub's servers (some
  brokers restrict by IP). The first manual run will show this.

## Angel One SmartAPI: feasibility

- Free API and free historical data (source above). It provides a WebSocket
  live feed and REST quotes and candles.
- Not confirmed from Angel One's official API reference, which is blocked from
  here: the exact login fields. From the SDK as I know it, login takes a client
  code, PIN and a TOTP code. Unattended login needs the TOTP secret stored as a
  GitHub Secret, so anyone who gets that secret could log in to your account.
  Treat that as a real risk.
- A forum thread (unofficial, March 2026) reports that creating a new API app
  asks for a static IP even for data-only use, and users entered any IP for
  data-only apps. Verify this when you create the app.
- Integration effort: a new read-only adapter beside the SAHI one
  (`nse_bot/broker/angelone.py`), with the same safety rules: market-data calls
  only, no order endpoints, and secrets from the environment. The strategy is
  unchanged.
