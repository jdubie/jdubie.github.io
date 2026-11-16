#!/bin/sh
# After the durability fix: re-run only durability for the remaining public calibration rows (3 at a time).
cd "$(dirname "$0")/.."
python3 grader/grade.py --oracle-seed 7101 --layers wire --only durability calib/abl-wire > logs/fix-abl-wire.log 2>&1 &
python3 grader/grade.py --oracle-seed 7101 --layers wire,semantics --only durability calib/abl-wire-semantics > logs/fix-abl-ws.log 2>&1 &
python3 grader/grade.py --oracle-seed 7101 --layers wire,dialect --only durability calib/abl-wire-dialect > logs/fix-abl-wd.log 2>&1 &
wait
python3 grader/grade.py --work cheat/work --only durability calib/cheat > logs/fix-cheat.log 2>&1 &
python3 grader/grade.py --oracle-seed 7102 --only durability calib/cross-7102-on-7101 > logs/fix-cross-a.log 2>&1 &
python3 grader/grade.py --oracle-seed 7101 --seed 7102 --only durability calib/cross-7101-on-7102 > logs/fix-cross-b.log 2>&1 &
wait
