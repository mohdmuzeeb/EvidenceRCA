# EvidenceRCA

EvidenceRCA is the research prototype for ICICYTA 2026 Paper 3: an evidence-verification and abstention layer for root-cause analysis (RCA) in microservices, evaluated with a matched-perturbation protocol. The plan, literature and protocol are in [`docs/`](docs/).

Work proceeds in approval-gated phases. Phase 0 (design) and Phase 1 (testbed and telemetry validation) are done. **No fault injection or RCA code exists yet.**

| Phase | Status | Report |
|---|---|---|
| 0: research plan, literature matrix, experiment protocol | done | [`docs/research_plan.md`](docs/research_plan.md) |
| 1: microservice testbed and telemetry validation | done | [`docs/phase1_validation.md`](docs/phase1_validation.md) |
| 2+: fault injection, RCA, evaluation | not started (needs approval) | — |

## Repository layout

```
compose.yaml                  testbed: 4 services + postgres + OpenTelemetry Collector
testbed/
  app/                        FastAPI services (gateway, order, payment, inventory) + shared OTel setup
  Dockerfile                  one image for all four services (python:3.12-slim, pinned by digest)
  requirements.lock           hash-pinned deps for the image (exported from uv.lock)
  otel-collector.yaml         OTLP + docker_stats + fluent_forward -> JSON-lines files
  postgres/init.sql           schema and seed stock
scripts/
  run_phase1.sh               end-to-end Phase 1 run (build, smoke test, validate, record, stop)
  smoke_test.py               real transactions through the gateway + direct DB state checks
  validate_telemetry.py       22 checks over the persisted traces/logs/metrics files
  collect_env.py              reproducibility manifest (versions, digests, limits, resources)
tests/                        validator self-test (corrupted copies must fail)
results/phase1/               committed reports and evidence from the recorded run
runs/                         raw per-run data (gitignored)
```

## Testbed

```
client ──> gateway ──> inventory   (GET /stock: availability check)
              └──────> order ──> inventory  (POST /reserve)
                          └────> payment    (POST /charge)
           order, inventory ──> postgres    (SQLAlchemy pool, size 5)
all services ──OTLP/HTTP──> otel-collector <── docker_stats (container CPU/mem/net)
                                    ^────────── postgres stdout (Docker fluentd log driver)
                            └──> runs/<run_id>/telemetry/{traces,logs,metrics}.jsonl
```

Each service emits:
- traces: FastAPI server spans, httpx client spans and SQLAlchemy DB spans, with W3C context propagation;
- structured logs over OTLP (and JSON on stdout), carrying `trace_id` and `span_id`;
- metrics: `app.requests`, `app.errors`, the `app.request.duration` histogram, OTel HTTP semantic-convention histograms, process CPU/memory metrics, and DB pool gauges.

Inter-service calls have a 2 s timeout and one bounded retry.

## Prerequisites

Tested on macOS 26.5 with an M1 and 8 GB RAM. See `results/phase1/environment_post.json` for the exact versions.

- [Colima](https://github.com/abiosoft/colima) (or any Docker engine) with Docker Compose v2.
- [uv](https://docs.astral.sh/uv/) and Python 3.12. uv uses `.python-version`.
- About 1.5 GB of disk for images. The VM below uses 3 GiB RAM, and the testbed needs about 0.6 GiB of it.
- Do not run the local LLM (Ollama) at the same time on an 8 GB machine.

## Reproduce Phase 1

```bash
# 1. Docker VM (once). Settings used for the recorded run:
colima start --cpu 4 --memory 3 --disk 30 --vm-type vz --mount-type virtiofs --arch aarch64

# 2. Python environment (host-side scripts and tests)
uv sync --frozen

# 3. Full run: build, start, smoke test, validate, record environment, stop containers.
#    Exit code 0 only if the smoke test and all telemetry checks pass.
./scripts/run_phase1.sh                 # or: RUN_ID=my-run ./scripts/run_phase1.sh

# 4. Validator self-test against the recorded run (corrupted copies must fail)
uv run --frozen pytest -q tests
```

`run_phase1.sh` does the following:
1. Creates a local, gitignored `.env` holding a random Postgres password.
2. Starts the stack with fresh containers (Postgres data is on tmpfs, so every run starts from the seed state).
3. Runs `scripts/smoke_test.py`, waits 12 s so the metric exports settle, and snapshots `docker stats`.
4. Stops the services so their SDKs flush, scrapes the collector's self-metrics, then stops the collector so the file exporter flushes.
5. Validates the persisted files.
6. Writes the environment manifest and runs `docker compose down`. Images, configs and data are kept.

While it runs, it holds a `caffeinate` assertion, because macOS sleep pauses the VM.

Outputs:

| Path | Content |
|---|---|
| `results/phase1/smoke_test_results.json` | every request (status, latency, trace id) and DB-state checks |
| `results/phase1/telemetry_validation.json` | pass/fail per check, with evidence IDs and sample trace IDs |
| `results/phase1/environment_{pre,post}.json` | OS/hardware, versions, image digests, build-input hashes, git commit, limits, resources |
| `results/phase1/telemetry_<run_id>.tar.gz` | the raw OTLP-JSON files the validation read |
| `results/phase1/collector_internal_metrics.prom`, `docker_stats_*.jsonl` | collector self-metrics, container resource snapshots |
| `runs/<run_id>/` | the same files plus the container stdout log (gitignored) |

Useful manual commands:

```bash
docker compose up -d --wait                       # start (needs .env; TELEMETRY_DIR defaults to runs/scratch/telemetry)
curl -s -X POST localhost:8080/api/orders -H 'content-type: application/json' \
     -d '{"item_id":"sku-001","quantity":1}' -i   # response header x-trace-id = the trace to look up
docker compose down                               # stop and remove containers
```

## Constraints and integrity rules

- No fabricated telemetry or results. Every check reads what the collector actually wrote.
- No paid API calls. Local LLM only (Phase 4).
- No secrets in the repository. `.env` is gitignored, and manifests redact passwords.
