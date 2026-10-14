# EvidenceRCA

EvidenceRCA is the research prototype for ICICYTA 2026 Paper 3: an evidence-verification and abstention layer for root-cause analysis (RCA) in microservices, evaluated with a matched-perturbation protocol. The plan, literature and protocol are in [`docs/`](docs/).

Work proceeds in approval-gated phases. Phases 0–3 and the frozen deterministic evaluation are done. Phase 4A (local LLM setup, the post-study B4/E1 conditions, and a five-call dev benchmark) is done on branch `phase4a`, in the separate package `phase4/`. It never modifies the frozen study. Phase 4A-v2 (a typed, ownership-aware prompt p4a-v2 compared with p4a-v1 on a pre-registered dev pilot) is on branch `phase4a-v2`. Phase 4A-v3 (E2 deterministic evidence ownership and the B4-G detector gate) is on branch `phase4a-v3`. Phase 4B (the full LLM evaluation) needs approval.

| Phase | Status | Report |
|---|---|---|
| 0: research plan, literature matrix, experiment protocol | done | [`docs/research_plan.md`](docs/research_plan.md) |
| 1: microservice testbed and telemetry validation | done | [`docs/phase1_validation.md`](docs/phase1_validation.md) |
| 2: workload, fault injection, ground truth, leakage prevention, pilot | done | [`docs/phase2_validation.md`](docs/phase2_validation.md), [`docs/fault_catalog.md`](docs/fault_catalog.md) |
| 3: baselines B1–B3, EvidenceRCA (no LLM), verifier V0–V6, abstention, dev evaluation | done | [`docs/phase3_validation.md`](docs/phase3_validation.md), [`docs/baseline_implementation.md`](docs/baseline_implementation.md), [`docs/evidence_verifier_spec.md`](docs/evidence_verifier_spec.md) |
| Frozen calibration/test evaluation (deterministic methods) | done | [`docs/final_results.md`](docs/final_results.md), [`docs/frozen/frozen_protocol.md`](docs/frozen/frozen_protocol.md) |
| 4A: local LLM (qwen3:4b via Ollama), B4/E1 integration, five-call dev benchmark | done | [`docs/phase4a_validation.md`](docs/phase4a_validation.md), [`docs/llm_evaluation_protocol.md`](docs/llm_evaluation_protocol.md) |
| 4B: full LLM evaluation | not started (needs approval) | — |

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
  app/control.py              loopback-only, uninstrumented control channel (Phase 2 fault hooks)
harness/                      Phase 2 capture side (may read ground truth)
  vm/workload.py              seeded open-loop workload + TCP probes (runs in the VM, uninstrumented)
  faults.py                   six fault injectors with explicit cleanup
  monitor.py                  symmetric ground-truth monitor (Docker, VM cgroups/netns, pg_stat_activity/pg_locks)
  validate.py                 independent validation, recovery verification, observable impact
  telemetry_check.py          per-case completeness, loss, throughput
  case.py, schedule.py        case runner (opaque UUID4 ids, sealing), seed splits and scenario matrix
  leakage.py, reevaluate.py   label-leakage checks; recompute verdicts from recorded files
evidencerca/                  agent side (never reads ground truth)
  rca_input.py                the only sanctioned case reader: allowlisted, symlink-safe, hash-verified
  evidence.py                 normalized evidence records (content ids) and the View object
  core.py, signals.py         alert detector + windows, observed graph; per-service signals and onsets
  methods/                    B1, B2 (MicroRCA-inspired), B3 (TraceRCA-inspired), EvidenceRCA E0 (+ A-graph)
  verifier.py                 V0-V6, independent of the pipeline
  abstention.py               sufficiency, modality agreement, margin, verified-claim coverage; A-margin
evaluation/                   evaluation side (may read ground truth): views (18 per case), dev runs, verifier
                              mutation tests, CPU-variant analysis, audit sheet
scripts/run_phase2.py         pilot / mechanism / load captures; leakage_check.py; summarize_phase2.py
phase4/                       post-study LLM conditions (separate from the frozen study)
  agent/                      evidence pack, versioned prompts, output schema/validator, bounded Ollama client, B4/E1
  eval/                       structured-output tests (mock server), five-call dev benchmark
tests/                        validator self-test, leakage tests, Phase 4 tests
results/phase1/, phase2/      committed reports and evidence
data/                         cases/ (raw, read-only), ground_truth/, mechanism/, load/ (gitignored)
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

## Phase 2: workload, faults, ground truth, leakage

```bash
colima start                                     # the VM from above; Ollama must not run at the same time
uv sync --frozen
docker compose --profile harness build           # service image + harness image tag
uv run python -m scripts.run_phase2 mechanism    # all six faults, short timeline (~18 min; not experiment data)
uv run python -m scripts.run_phase2 pilot --label A   # 4 pilot cases, protocol timeline, seed 11 = dev (~23 min)
uv run python -m scripts.run_phase2 pilot --label B   # same seed again: repeatability
uv run python -m scripts.run_phase2 load --rate 20    # sustained no-fault load (collector headroom)
uv run python -m scripts.summarize_phase2        # -> results/phase2/pilot_results.json, ground_truth_validation.json
uv run python -m scripts.leakage_check           # -> results/phase2/leakage_test_results.json
uv run pytest -q                                 # all tests (validator self-test, leakage tests)
```

- Wrap long captures in one `caffeinate -dims …`, keep the lid open and use AC power: macOS sleep pauses the VM. A case spanning a sleep is detected (`capture_continuity`) and must be rerun.
- Non-dev seeds (calibration 23/37, test 41/53/67) are refused unless explicitly allowed; the full matrix is not run in Phase 2.
- Raw bundles in `data/cases/` are sealed read-only. To delete data, run `chmod -R u+w data` first.
- Phase 1 regression on the Phase 2 testbed: `RUN_ID=x RESULTS=results/phase2/phase1_regression ./scripts/run_phase1.sh`.

## Phase 3: baselines, EvidenceRCA, verifier, abstention (dev seed 11 only)

```bash
colima start && uv sync --frozen
caffeinate -dims uv run python -m scripts.run_phase2 matrix --seed 11      # dev matrix captures (skips valid ones)
caffeinate -dims uv run python -m scripts.run_phase2 cpu-variant           # strong cpu_pressure dev cases
colima stop                                                                # never analyse while capturing
uv run python -m evaluation.run_dev               # -> results/phase3/dev_results.json, abstention_dev_results.json
uv run python -m evaluation.verifier_tests        # -> results/phase3/verifier_tests.json (mutations by construction)
uv run python -m evaluation.cpu_variant_analysis  # -> results/phase3/cpu_variant_analysis.json
uv run pytest -q                                  # all tests (Phase 1-3)
```

- Methods only see a `View` (opaque `view_id`, records, unavailable modalities, time tolerance). The view builder (evaluation side) may read ground truth for the CS/RL/CF perturbations; its transform log never enters a View.
- Calibration/test seeds remain refused by default. tau (abstention) is not calibrated in Phase 3.

## Frozen calibration and test evaluation

```bash
uv run python -m evaluation.freeze verify                       # refuses to proceed if any frozen file changed
caffeinate -dims runs/capture_final.sh                          # seeds 23/37 then 41/53/67 (strong CPU), 2 passes each
uv run python -m evaluation.run_final calibrate                 # tau from seeds 23/37 only
uv run python -m evaluation.run_final evaluate                  # held-out test with the hash-checked calibration
uv run python -m evaluation.sensitivity_adjudicated             # sensitivity: adjudicated excluded scenario
```

## Constraints and integrity rules

- No fabricated telemetry or results. Every check reads what the collector actually wrote.
- No paid API calls. Local LLM only (Phase 4).
- No secrets in the repository. `.env` is gitignored, and manifests redact passwords.

## Reproduce Phase 4A (local LLM; dev seed 11 only)

Stop Docker/Colima first; the 8 GB machine cannot run both. Use AC power and close memory-heavy apps.

```bash
uv run python -m phase4.eval.structured_tests       # 17 mock-server scenarios + prompt leakage scan (no model)
uv run pytest -q tests/test_phase4.py
ollama pull qwen3:4b                                # 2.4 GiB, digest 359d7dd4bcda...
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &
caffeinate -dims uv run python -m phase4.eval.benchmark   # -> results/phase4a/five_call_benchmark.json, raw/
pkill -f "ollama serve"
```

## Reproduce Phase 4A-v2 (prompt revision p4a-v2; dev seed 11 only)

p4a-v1 and its results stay unchanged; `tests/test_phase4_v2.py` checks their hashes. The rationale and the pre-registered pilot rule are in `docs/prompt_revision_rationale.md`. The results are in `docs/phase4a_v2_validation.md`.

```bash
uv run python -m phase4.eval.structured_tests_v2    # typed-schema mock scenarios + leakage scan (no model)
uv run python -m phase4.eval.pilot_v2 plan          # predeclared selection rule -> results/phase4a_v2/pilot_plan.json
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &
caffeinate -dims uv run python -m phase4.eval.pilot_v2 run       # <= 80 calls, resumable, AC power required
uv run python -m phase4.eval.pilot_v2 analyze       # dev_comparison / claim_analysis / control_abstention + audit sheet
pkill -f "ollama serve"
```

## Reproduce Phase 4A-v3 (E2 evidence ownership, B4-G detector gate; dev seed 11 only)

The design and pre-registration are in `docs/evidence_ownership_design.md`; the results are in `docs/phase4a_v3_validation.md`. E2 and B4-G reuse the hash-documented p4a-v1 calls from the Phase 4A-v2 pilot. Only a predeclared 9-call re-execution check makes new calls.

```bash
uv run python -m phase4.eval.pilot_v3 plan
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &
caffeinate -dims uv run python -m phase4.eval.pilot_v3 reverify
pkill -f "ollama serve"
uv run python -m phase4.eval.pilot_v3 analyze
```
