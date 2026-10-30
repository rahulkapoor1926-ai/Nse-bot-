# File inventory: upstream day-trader-skill (protected)

Upstream commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` from https://github.com/Jakeschincariol/day-trader-skill, cloned 2026-10-30. Every file below is protected: the NSE work never edits, moves or deletes it. `tests/nse/test_protected.py` re-hashes them against `docs/nse/protected-sha256.txt`.

| file | lines | bytes | role |
|---|---:|---:|---|
| `.claude-plugin/marketplace.json` | 18 | 787 | other |
| `.claude-plugin/plugin.json` | 22 | 665 | other |
| `LICENSE` | 21 | 1073 | other |
| `README.md` | 216 | 11216 | other |
| `tests/_load.py` | 15 | 468 | upstream test |
| `tests/fixtures/trader-alerts/stooq-AAPL.csv` | 2 | 109 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/stooq-ZZZZ.csv` | 2 | 80 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-AAPL.json` | 0 | 1297 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-AXP.json` | 0 | 1307 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-BAC.json` | 0 | 1325 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-GOOGL.json` | 0 | 1304 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-KO.json` | 0 | 1308 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-TSLA.json` | 0 | 2875 | test fixture (real US data) |
| `tests/fixtures/trader-alerts/yahoo-ZZZZ.json` | 1 | 109 | test fixture (real US data) |
| `tests/fixtures/trader-analyst/000032019326000018/index.json` | 0 | 1890 | test fixture (real US data) |
| `tests/fixtures/trader-analyst/company_tickers.json` | 16 | 223 | test fixture (real US data) |
| `tests/fixtures/trader-analyst/companyfacts-CIK0000021344.json` | 0 | 6616 | test fixture (real US data) |
| `tests/fixtures/trader-analyst/companyfacts-CIK0000320193.json` | 0 | 26050 | test fixture (real US data) |
| `tests/fixtures/trader-analyst/submissions-CIK0000021344.json` | 351 | 3662 | test fixture (real US data) |
| `tests/fixtures/trader-analyst/submissions-CIK0000320193.json` | 511 | 5105 | test fixture (real US data) |
| `tests/fixtures/trader-budget/berkshire-q2-2026-top5.json` | 48 | 1183 | test fixture (real US data) |
| `tests/fixtures/trader-budget/plan-tsla.json` | 21 | 504 | test fixture (real US data) |
| `tests/fixtures/trader-entry/DEMO-1m-synthetic.json` | 0 | 2061 | test fixture (real US data) |
| `tests/fixtures/trader-entry/TSLA-2026-10-19-5m.json` | 0 | 2789 | test fixture (real US data) |
| `tests/fixtures/trader-news/chart-TSLA.json` | 0 | 2072 | test fixture (real US data) |
| `tests/fixtures/trader-news/company_tickers.json` | 36 | 532 | test fixture (real US data) |
| `tests/fixtures/trader-news/sec-8-K-0000021344.xml` | 106 | 6972 | test fixture (real US data) |
| `tests/fixtures/trader-news/sec-8-K-0000320193.xml` | 121 | 7456 | test fixture (real US data) |
| `tests/fixtures/trader-news/yahoo-AAPL.xml` | 60 | 4313 | test fixture (real US data) |
| `tests/fixtures/trader-news/yahoo-KO.xml` | 46 | 3157 | test fixture (real US data) |
| `tests/fixtures/trader-news/yahoo-TSLA.xml` | 144 | 12062 | test fixture (real US data) |
| `tests/fixtures/trader-pelosi/2026FD.zip` | 2 | 716 | test fixture (real US data) |
| `tests/fixtures/trader-pelosi/ptr-20033725-excerpt.txt` | 58 | 3592 | test fixture (real US data) |
| `tests/fixtures/trader-pelosi/ptr-20035143.txt` | 72 | 4596 | test fixture (real US data) |
| `tests/fixtures/trader-pelosi/ptr-20035562-excerpt.txt` | 84 | 3446 | test fixture (real US data) |
| `tests/fixtures/trader-pelosi/ptr-20035590.txt` | 47 | 2165 | test fixture (real US data) |
| `tests/fixtures/trader-risk/orders-reel.json` | 41 | 757 | test fixture (real US data) |
| `tests/fixtures/trader-risk/orders-tsla.json` | 31 | 922 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/TSLA-5m-live-2026-10-20-1001.json` | 0 | 1986 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/daily-spark-2026-10-19.json` | 1 | 44322 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/spark-3mo-live-2026-10-20.json` | 0 | 42905 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/spark-5m-live-2026-10-20-1001.json` | 0 | 28175 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/symdir/nasdaqlisted.txt` | 20 | 1384 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/symdir/otherlisted.txt` | 20 | 1728 | test fixture (real US data) |
| `tests/fixtures/trader-scanner/wall5m-2026-10-19.json.gz` | 250 | 65951 | test fixture (real US data) |
| `tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz` | 371 | 92941 | test fixture (real US data) |
| `tests/test_alerts.py` | 454 | 22688 | upstream test |
| `tests/test_analyst.py` | 163 | 7404 | upstream test |
| `tests/test_broker.py` | 307 | 15409 | upstream test |
| `tests/test_budget.py` | 374 | 18047 | upstream test |
| `tests/test_entry.py` | 605 | 29666 | upstream test |
| `tests/test_exit.py` | 290 | 14278 | upstream test |
| `tests/test_news.py` | 208 | 10001 | upstream test |
| `tests/test_pelosi.py` | 200 | 9490 | upstream test |
| `tests/test_repo.py` | 139 | 6655 | upstream test |
| `tests/test_risk.py` | 523 | 27559 | upstream test |
| `tests/test_scanner.py` | 646 | 35881 | upstream test |
| `tests/test_timemachine.py` | 614 | 32237 | upstream test |
| `trader-alerts/SKILL.md` | 160 | 8204 | skill instructions |
| `trader-alerts/alerts.example.json` | 9 | 597 | example config |
| `trader-alerts/alerts.py` | 787 | 32825 | skill tool (stdlib Python) |
| `trader-analyst/SKILL.md` | 123 | 5943 | skill instructions |
| `trader-analyst/facts.py` | 548 | 23996 | skill tool (stdlib Python) |
| `trader-analyst/metrics.example.json` | 85 | 2317 | example config |
| `trader-broker/SKILL.md` | 212 | 11451 | skill instructions |
| `trader-broker/claude_desktop_config.example.json` | 14 | 378 | example config |
| `trader-broker/ticket.py` | 720 | 32390 | skill tool (stdlib Python) |
| `trader-budget/SKILL.md` | 166 | 8629 | skill instructions |
| `trader-budget/config.example.json` | 25 | 595 | example config |
| `trader-budget/size.py` | 881 | 39238 | skill tool (stdlib Python) |
| `trader-budget/targets.example.json` | 11 | 610 | example config |
| `trader-entry/SKILL.md` | 144 | 7715 | skill instructions |
| `trader-entry/entry.py` | 960 | 42324 | skill tool (stdlib Python) |
| `trader-exit/SKILL.md` | 132 | 6987 | skill instructions |
| `trader-exit/exit.py` | 735 | 31107 | skill tool (stdlib Python) |
| `trader-news/SKILL.md` | 104 | 5346 | skill instructions |
| `trader-news/news.py` | 546 | 22467 | skill tool (stdlib Python) |
| `trader-pelosi/SKILL.md` | 158 | 7395 | skill instructions |
| `trader-pelosi/ptr.py` | 566 | 22928 | skill tool (stdlib Python) |
| `trader-risk/SKILL.md` | 172 | 9608 | skill instructions |
| `trader-risk/check.py` | 819 | 39151 | skill tool (stdlib Python) |
| `trader-scanner/SKILL.md` | 203 | 10940 | skill instructions |
| `trader-scanner/scan.py` | 1083 | 47679 | skill tool (stdlib Python) |
| `trader-timemachine/SKILL.md` | 168 | 9624 | skill instructions |
| `trader-timemachine/timemachine.py` | 956 | 43070 | skill tool (stdlib Python) |
