# Groww Trade API: free-trial investigation

Checked 2026-10-30. No code was changed for Groww, nothing was bought or
activated, and no request was sent to Groww with your key.

## Bottom line

Groww's free trial covers the parts of the API this bot must **not** use yet
(placing orders) and leaves out the parts it **needs** (live prices, option
chain, historical candles). Groww says so itself: "The Free Trial gives you
Groww Cloud and every API except the Live Data and Backtesting APIs"
([Groww blog](https://groww.in/blog/api-trading-on-groww)). So on the free trial
Groww cannot feed the scanner, the paper trader or the validation gate. SAHI
stays the market-data source.

## Free features (per Groww's own pages)

| feature | free trial | used by the bot now? |
|---|---|---|
| Authentication (access token) | yes | only for a connection test |
| Instrument list (public CSV) | yes, no login needed | not needed; the SAHI list is already used |
| User profile, holdings, positions, margin | yes | read-only account check only |
| Place / modify / cancel orders, GTT/OCO smart orders | yes | **no, blocked** (live orders stay disabled) |

## Paid-only (₹499 + 18% GST a month, auto-renews from your Groww balance)

| feature | why the bot would need it |
|---|---|
| Live data: quote, LTP, OHLC, live feed (WebSocket) | scanning, entries, exits, paper fills |
| Option chain and Greeks | choosing NIFTY/BANKNIFTY contracts |
| Historical candles ("Backtesting APIs"; equities, indices, F&O from 2020) | building datasets for the 10,000-example gate |

Pricing: [groww.in/trade-api](https://groww.in/trade-api) lists ₹499 + taxes a
month; the Groww blog lists a ₹0 Free Trial plan and says paid plans
auto-renew from the Groww balance. **Do not tap any upgrade or plan button.**

Note on history: even the paid candles are OHLC bars without bid/ask, so for
options they could not pass the gate (options need recorded bid/ask). For
stocks/ETFs they would be allowed with the estimated spread you approved.

## Connection test results

| test | result |
|---|---|
| Reach `api.groww.in` from this Claude environment | **Blocked** (HTTP 403 from the network policy, also `groww.in` and `growwapi-assets.groww.in`) |
| Authentication with your key | **Not run.** Cannot reach Groww from here. |
| Profile / holdings / positions / margin | **Not run**, same reason |
| Live data / option chain / candles (expected 403 on free trial) | **Not run**, same reason |

Nothing in the tables above is claimed to work until a real run shows it.

## Limitations and risks found

1. **No read-only key.** The official SDK (`growwapi` 1.5.0 from PyPI) shows one
   access token for everything, including `/order/create`. Groww's docs list no
   scopes. A leaked token could place orders. SAHI's key, by contrast, is
   limited to `marketdata.read`.
2. Two key types ([SDK docs](https://groww.in/trade-api/docs/python-sdk)):
   key + secret needs **daily approval** on the Groww Cloud API keys page;
   TOTP keys have "No Expiry". The safer one here is key + secret, because no
   long-lived TOTP seed has to be stored anywhere.
3. Token endpoint limit: 150 requests per 24 hours.
4. Static IP rules are not covered in the official docs I could read.
5. The official SDK pulls in pandas, protobuf, NATS and others and includes
   order functions, so it would not be used. A small client with a fixed list
   of allowed GET paths would be written instead, as was done for SAHI.

Endpoints, from the official SDK source (host `https://api.groww.in/v1`):
token `POST /token/api/access`; free: `GET /user/detail`, `/holdings/user`,
`/positions/user`, `/margins/detail/user`; paid: `/live-data/quote|ltp|ohlc`,
`/option-chain/...`, `/historical/candles`, `/historical/expiries`,
`/historical/contracts`. Order paths (`/order/*`, `/order-advance/*`) would be
refused by the client and never called.

## Next steps, zero spending (only if you approve)

1. I add a read-only `groww-check` command: it mints a token, calls the four
   free account endpoints and one live-data endpoint, and prints only HTTP
   status codes and field names, never values like balances or the token.
   It refuses every order path, like the SAHI client.
2. It runs on GitHub Actions (the repository you are creating), not here.
   You add `GROWW_API_KEY` and `GROWW_API_SECRET` as GitHub Secrets and approve
   the key in Groww Cloud that morning.
3. You tap Run workflow once. I report which endpoints answered and which
   returned "subscription required".
4. Data for the bot keeps coming from SAHI, which is free. Groww would matter
   only later, if you approve real orders and choose Groww as the broker
   for them.
