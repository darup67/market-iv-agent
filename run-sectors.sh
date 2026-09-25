#!/bin/bash
# Weekday late-morning scan of every S&P sector profile, one after another (not in
# parallel: Yahoo rate-limits bursts). Starts 10:58, after the 10:52 health-care run.
# Each run writes data/sectors/<key>/handoff for ~/market-lab/event-desk.
cd "$(dirname "$0")"
for p in profiles/*.json; do
  key=$(basename "$p" .json)
  echo "$(date '+%m-%d %H:%M:%S') === $key"
  .venv/bin/python -u agent.py --profile "$key" --tag "late-morning screen" || echo "$(date '+%H:%M:%S') $key FAILED ($?)"
done
echo "$(date '+%m-%d %H:%M:%S') all sectors done"
