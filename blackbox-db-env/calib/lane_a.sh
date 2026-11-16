#!/bin/sh
# Calibration lane A: oracle self-check, stock PostgreSQL, and the layer ablations (oracle with layers off).
cd "$(dirname "$0")/.."
python3 grader/grade.py --oracle-seed 7101 calib/oracle-7101 > logs/cal-oracle.log 2>&1
python3 grader/grade.py --postgres calib/postgres > logs/cal-postgres.log 2>&1
python3 grader/grade.py --oracle-seed 7101 --layers wire calib/abl-wire > logs/cal-abl-wire.log 2>&1
python3 grader/grade.py --oracle-seed 7101 --layers wire,dialect calib/abl-wire-dialect > logs/cal-abl-wire-dialect.log 2>&1
python3 grader/grade.py --oracle-seed 7101 --layers wire,semantics calib/abl-wire-semantics > logs/cal-abl-wire-semantics.log 2>&1
