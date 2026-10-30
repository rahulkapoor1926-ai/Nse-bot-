#!/bin/sh
# Commits the public paper-trading state (ledger, risk, alerts, dashboard data)
# in the state/ worktree to the paper-state branch and pushes it, with retries.
# The SAHI instrument list, recordings and logs are kept out by state/.gitignore.
cd "$(dirname "$0")/../state" || exit 1
git add -A
git commit -q -m "paper state $(date +%F_%H%M) IST" || { echo "nothing new to save"; exit 0; }
for i in 1 2 3 4; do
  git push -q origin paper-state && exit 0
  sleep $((i * 5))
done
echo "push of paper-state failed after 4 tries" >&2
exit 1
