#!/bin/sh
# The one model run the brief allows: Opus 5.5, 20 minutes, snapshots every 2 minutes; then grade final + snapshots (2 at a time).
cd "$(dirname "$0")"
python3 run_agents.py --models opus --minutes 20 --tag opus20 --no-grade > logs/opus-run.log 2>&1
python3 grade_runs.py runs/opus20/opus__r0 --jobs 2 > logs/opus-grade.log 2>&1
