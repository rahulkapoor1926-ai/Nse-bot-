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

## Step by step for Option A (only after you choose it)

1. In Safari, create a free GitHub account, then create an empty **public**
   repository (for example `nse-trading-bot`).
2. Open [Connect GitHub](https://claude.ai/connect-github) and give Claude
   access to that repository, then tell me its name. I push the project and a
   workflow file. You type nothing technical.
3. Get read-only market-data credentials from your broker (SAHI: an API key
   with only `marketdata.read`; Angel One: a SmartAPI key). Do not paste them
   into chat.
4. In the repository on GitHub: Settings → Secrets and variables → Actions →
   New repository secret. Add each credential there.
5. Each trading morning, approve the API key in the broker app if it requires
   daily approval (SAHI does).
6. Results appear in the repository (`state/` folder and the job log). I can
   read and summarise them for you in this chat.

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
