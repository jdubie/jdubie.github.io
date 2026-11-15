#!/bin/sh
# Full calibration: naive baseline + expected answers, then smart and sloppy against them.
cd "$(dirname "$0")"
exec python3 grader/grade.py ref/grades smart=ref/smart sloppy=ref/sloppy --calibrate
