#!/bin/sh
# Calibration lane B: the record-and-replay cheat, and cross-seed (a perfect server for one seed graded on another).
cd "$(dirname "$0")/.."
python3 cheat/make_cheat.py cheat/work > logs/cal-cheat-build.log 2>&1
until [ -f cache/oracle-v2-7101-90210.json ]; do sleep 5; done
python3 grader/grade.py --work cheat/work calib/cheat > logs/cal-cheat.log 2>&1
python3 grader/grade.py --oracle-seed 7102 calib/cross-7102-on-7101 > logs/cal-cross-a.log 2>&1
python3 grader/grade.py --oracle-seed 7101 --seed 7102 calib/cross-7101-on-7102 > logs/cal-cross-b.log 2>&1
