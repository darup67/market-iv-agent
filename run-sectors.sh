#!/bin/bash
# Weekday open-screen scan of every S&P sector profile, one after another (not in
# parallel: Yahoo rate-limits bursts). Starts after the health-care 09:45 run is done.
# Each run writes data/sectors/<key>/handoff for ~/market-lab/event-desk.
cd "$(dirname "$0")"
for p in profiles/*.json; do
  key=$(basename "$p" .json)
  echo "$(date '+%m-%d %H:%M:%S') === $key"
  .venv/bin/python -u agent.py --profile "$key" --tag "open screen" || echo "$(date '+%H:%M:%S') $key FAILED ($?)"
done
echo "$(date '+%m-%d %H:%M:%S') all sectors done"
