#!/usr/bin/env bash
# Phase 1: build and start the testbed, run the smoke test, validate telemetry, record the environment,
# then stop the containers. Raw telemetry stays in runs/<run_id>/; reports go to results/phase1/.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN_ID="${RUN_ID:-phase1-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="runs/${RUN_ID}"
RESULTS="${RESULTS:-results/phase1}"   # override to keep the official Phase 1 artifacts untouched
SETTLE_S="${SETTLE_S:-12}"   # > two metric export intervals (5 s) and one docker_stats interval (5 s)
mkdir -p "${RUN_DIR}/telemetry" "${RESULTS}"
export TELEMETRY_DIR="./${RUN_DIR}/telemetry"

# Local-only DB password, generated once and never committed (.env is gitignored).
if [ ! -f .env ]; then
  umask 077
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 16)" > .env
fi

# Keep macOS awake for the duration: idle sleep pauses the Colima VM mid-run (seen in a dry run).
if command -v caffeinate >/dev/null 2>&1; then caffeinate -is -w $$ & fi

step() { printf '\n== %s\n' "$*"; }

step "environment (pre)"
uv sync --frozen --quiet
uv run --frozen python scripts/collect_env.py --stage pre --out "${RUN_DIR}/environment_pre.json"

step "build and start (fresh containers)"
docker compose build --quiet
docker compose up -d --wait --force-recreate --renew-anon-volumes
docker compose ps --format 'table {{.Service}}\t{{.Status}}' | tee "${RUN_DIR}/compose_ps.txt"
docker stats --no-stream --format json > "${RUN_DIR}/docker_stats_idle.jsonl"

step "smoke test"
smoke_rc=0
uv run --frozen python scripts/smoke_test.py --run-id "${RUN_ID}" \
  --out "${RESULTS}/smoke_test_results.json" || smoke_rc=$?

step "settle ${SETTLE_S}s, then snapshot resources"
sleep "${SETTLE_S}"
docker stats --no-stream --format json > "${RUN_DIR}/docker_stats_after_smoke.jsonl"

step "stop services (SDKs flush on shutdown), then the collector (file exporter flushes)"
docker compose stop gateway order payment inventory postgres
sleep 3
curl -fsS http://127.0.0.1:8888/metrics > "${RUN_DIR}/collector_internal_metrics.prom"
docker compose logs --no-color --timestamps > "${RUN_DIR}/container_stdout.log" 2>&1
docker compose stop otel-collector

step "validate telemetry from the persisted files"
validation_rc=0
uv run --frozen python scripts/validate_telemetry.py \
  --telemetry-dir "${RUN_DIR}/telemetry" \
  --smoke "${RESULTS}/smoke_test_results.json" \
  --collector-metrics "${RUN_DIR}/collector_internal_metrics.prom" \
  --out "${RESULTS}/telemetry_validation.json" || validation_rc=$?

step "environment (post), copy evidence, remove containers"
uv run --frozen python scripts/collect_env.py --stage post --out "${RUN_DIR}/environment_post.json"
docker compose down   # removes containers and network; images, configs and run data are kept
cp "${RUN_DIR}"/environment_*.json "${RUN_DIR}"/docker_stats_*.jsonl \
   "${RUN_DIR}/collector_internal_metrics.prom" "${RUN_DIR}/compose_ps.txt" "${RESULTS}/"
tar -C "${RUN_DIR}" -czf "${RESULTS}/telemetry_${RUN_ID}.tar.gz" telemetry
echo "${RUN_ID}" > "${RESULTS}/RUN_ID"

step "result"
echo "run: ${RUN_ID}  smoke rc=${smoke_rc}  validation rc=${validation_rc}"
exit $(( smoke_rc || validation_rc ))
