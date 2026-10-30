# Deployment guide

A chat with Claude does not keep running after it closes. The bot must run on
a computer or server you control, under a supervisor that restarts it.

## 1. Machine

- Any always-on Linux machine or small VPS in India (lower latency to SAHI),
  with the clock synced (`timedatectl set-ntp true`). Quote staleness checks
  depend on an accurate clock.
- Python 3.9 or newer. No packages to install.
- A dedicated user: `sudo useradd -m nsebot`.

## 2. Install

```bash
sudo -iu nsebot
git clone <your copy of this repository> nse-options-bot   # or unpack the project-files copy
cd nse-options-bot
cp config/paper.example.json config/paper.json
python3 -m nse_bot --config config/paper.json validate-config
scripts/test_all.sh
```

## 3. Secrets

Put the SAHI key in a root-readable environment file, not in the repository:

```bash
sudo install -m 600 -o nsebot /dev/null /etc/nse-bot.env
sudoedit /etc/nse-bot.env
#   SAHI_KEY_ID=...
#   SAHI_KEY_SECRET=...
```

The key must be approved in the SAHI dashboard each trading day (approval lapses
at 06:00 IST) unless it is a TOTP key. With a TOTP key, the 6-digit code must be
supplied at start (`SAHI_TOTP`); do not store the TOTP secret next to the key
secret on the same machine.

## 4. systemd units

`/etc/systemd/system/nse-bot-record.service` (records real history every session):

```ini
[Unit]
Description=NSE bot recorder (read-only)
After=network-online.target

[Service]
User=nsebot
WorkingDirectory=/home/nsebot/nse-options-bot
EnvironmentFile=/etc/nse-bot.env
ExecStart=/usr/bin/python3 -m nse_bot --config config/paper.json record --refresh-master
Restart=on-failure
RestartSec=30
```

`/etc/systemd/system/nse-bot-paper.service` is the same with
`ExecStart=/usr/bin/python3 -m nse_bot --config config/paper.json paper --strategy config/strategies/orb15.json --dataset <approved dataset dir>`.

Timers start both at 09:05 IST on weekdays (the bot itself checks the NSE
holiday list and exits after the close):

```ini
# /etc/systemd/system/nse-bot-paper.timer
[Timer]
OnCalendar=Mon..Fri 09:05 Asia/Kolkata
Persistent=false
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nse-bot-record.timer nse-bot-paper.timer
```

## 5. Monitoring

- Logs: `state/logs/nse_bot.jsonl` (one JSON object per line) and `journalctl -u nse-bot-paper`.
- Alerts: `state/alerts/alerts.jsonl`. Point any notifier you trust at this file
  (for example a small script that forwards new lines to Telegram or email).
- Health: `python3 -m nse_bot --config config/paper.json risk-status` shows the
  lockout; `report` checks the ledger hash chain.
- A watchdog cron that alerts if `state/logs/nse_bot.jsonl` has not changed for
  5 minutes during market hours catches a hung process.

## 6. Restart behaviour

- Risk state, lockout, alert dedupe and the paper ledger are on disk and written
  atomically, so a restart resumes with the same trade count and lockout.
- Open paper positions are rebuilt from the ledger and managed on the next cycle.
- If the process is down at `flat_by`, the time exit happens on the next cycle
  that has a fresh quote; it is never assumed.

## 7. Before any live phase (not in this build)

Live orders need a separate review and your explicit approval. SAHI order
endpoints require the `orders.write` scope and a registered static IP, and
SEBI's retail algorithmic-trading framework applies to API orders. None of that
is implemented here.
