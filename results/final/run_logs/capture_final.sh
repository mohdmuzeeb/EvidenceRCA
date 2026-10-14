#!/bin/sh
# Frozen-protocol capture: calibration seeds then test seeds; second pass recaptures invalid cases only.
cd "$(dirname "$0")/.."
for spec in 23:calibration 37:calibration 41:test 53:test 67:test; do
  seed=${spec%%:*}; label=${spec##*:}
  for pass in 1 2; do
    uv run --frozen python -m scripts.run_phase2 matrix --seed "$seed" --allow-non-dev-seed --cpu-variant strong --label "$label"
    echo "SEED_${seed}_PASS_${pass}_EXIT $?"
  done
done
echo "ALL_CAPTURE_DONE"
