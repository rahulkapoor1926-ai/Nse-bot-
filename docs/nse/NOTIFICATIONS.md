# P&L notifications to your iPhone (paper trades only)

The paper bot sends four kinds of message to a private Telegram bot. The Telegram app
pushes them to your iPhone. Reporting only: the notifier never places, changes or blocks a
trade, and a failed send never affects trading (`tests/test_notify.py` proves both).

| When | Message |
|---|---|
| A paper trade opens | contract, CE/PE, strike, expiry, fill price, quantity, initial stop-loss, target, entry charges |
| While it is open, every 5 minutes | current FYERS bid and its quote time, unrealized P&L in Rs and %, stop-loss, trailing stop |
| A paper trade closes | reason, entry, exit, quantity, gross P&L, entry and exit charges itemised, total costs, net P&L |
| End of each run | trades closed, winners, losers, gross, costs, net P&L for both books (ORB and sweep) |

## Where the numbers come from

- Entry and exit prices are the paper fills in the hash-chained ledger, which were made from
  FYERS bid/ask quotes that passed the freshness checks.
- Live P&L uses the same bid the exit logic accepted: timestamped, at most
  `max_quote_age_seconds` old, and from the session's data source. If the price is stale,
  missing or from another source, the update says **PRICE NOT VERIFIED** and shows no P&L.
- Unrealized P&L subtracts the entry charges and the charges a sale at that bid would cost
  (labelled as estimated). Percent is of the premium plus entry charges paid.
- Charges come from the published charge schedule in `config/costs`, the same model the
  ledger uses. Net P&L in the close report is the ledger's realized figure.
- Trailing stop: **none**. Both strategies use a fixed stop set before entry; the message
  says so rather than inventing one. Adding a trailing stop would be a strategy change.

## Setup (once, about 5 minutes)

Never paste the token in chat; it only goes into GitHub's secret store.

1. On your iPhone, install **Telegram** and open a chat with **@BotFather** (blue tick).
2. Send `/newbot`, choose a name and a username ending in `bot`. BotFather replies with a
   **token**. Keep that screen open.
3. Open a chat with your new bot and press **Start** (a bot cannot message you until you do).
4. Get your chat id: open a chat with **@userinfobot** and press Start; it replies with your `Id`
   (a number). @userinfobot is a widely used third-party bot; it only sees your public profile.
5. In GitHub: repository **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` = the token from step 2
   - `TELEGRAM_CHAT_ID` = the number from step 4
6. **Actions → notify-test → Run workflow.** A test message should arrive on your iPhone within
   seconds. The run fails and says why if it was not delivered.

Without these secrets the bot still runs; messages only go to the run log and
`state/notify/notify.jsonl` on the `paper-state` branch.
