#!/bin/sh
# Re-run only the durability component of existing grades (after the 2026-11-16 durability fix), 4 at a time.
#   regrade_durability.sh <run_dir> <seed> <wseed> [oracle_version]
cd "$(dirname "$0")"
RUN=$1 SEED=$2 WSEED=$3 export BBX_ORACLE_VERSION=${4:-2}
for g in "$RUN"/grades/*; do
  n=$(basename "$g")
  if [ "$n" = final ]; then w="$RUN/work"; else w="$RUN/snapshots/$n"; fi
  echo "$w $g"
done | xargs -P 4 -n 2 sh -c 'python3 grader/grade.py --work "$0" "$1" --seed '"$SEED"' --wseed '"$WSEED"' --only durability > "$1/regrade.log" 2>&1'
