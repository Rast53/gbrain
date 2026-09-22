#!/usr/bin/env bash
# Run every Laya checkpoint of the stand, sequentially, into out/.
#
#   LAYA_VENV=/tmp/laya-venv bash tools/decision-stand/run_all_laya.sh
#
# Checkpoints, in order: multilingual, english, typed-decisions, routed.
# Each pass appends to its own results file and resumes if interrupted.
set -euo pipefail
cd "$(dirname "$0")/../.."

VENV="${LAYA_VENV:-/tmp/laya-venv}"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "no python at $PY (set LAYA_VENV)" >&2; exit 2; }

OUT_DIR="${OUT_DIR:-tools/decision-stand/out}"
mkdir -p "$OUT_DIR"

for ckpt in multilingual english typed-decisions routed; do
  log="$OUT_DIR/run_laya_$ckpt.log"
  echo "== $ckpt -> $OUT_DIR/results_laya_$ckpt.jsonl"
  "$PY" tools/decision-stand/run_laya.py \
      --checkpoint "$ckpt" \
      --out "$OUT_DIR/results_laya_$ckpt.jsonl" 2>&1 | tee "$log"
done

echo "== all checkpoints done; now: python3 tools/decision-stand/decision_stand.py --mode report"
