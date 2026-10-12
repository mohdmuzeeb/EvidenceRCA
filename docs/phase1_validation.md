# Phase 1: Microservice Testbed and Telemetry Validation

| | |
|---|---|
| **Outcome** | **PASS.** Real end-to-end transactions succeeded, and traces, logs and metrics were each validated independently (22/22 telemetry checks, 7/7 smoke checks). |
| Recorded run | `phase1-20261012T044845Z` (2026-10-12, 04:48 UTC) |
| Code commit | `db5531c31c2931752f528c5fa9b8155ce74665fc` (no uncommitted changes to tracked files at run time) |
| Machine-readable reports | [`results/phase1/telemetry_validation.json`](../results/phase1/telemetry_validation.json), [`results/phase1/smoke_test_results.json`](../results/phase1/smoke_test_results.json) |
| Raw evidence | [`results/phase1/telemetry_phase1-20261012T044845Z.tar.gz`](../results/phase1/) (the exact files that were validated) |
| Reproduce | `./scripts/run_phase1.sh` (see the [README](../README.md#reproduce-phase-1)) |

All telemetry in this report was produced by the running services and read back from the collector's output files. No telemetry was synthesized.

## 1. Environment and resource checks (before the build)

| Item | Found | Action |
|---|---|---|
| Disk | 41 GB free before the build, 37 GB after (it was 8.4 GB in Phase 0) | none needed |
| RAM | 8 GB. Under memory pressure from other apps (~3.4 GiB compressed, ~0.1 GiB free). | Colima VM sized to 3 GiB. Ollama not used. |
| Colima | 0.8.1 is installed, **but the previous VM instance no longer existed** (`colima list` was empty, so it was probably removed when disk was freed) | Created a new VM: 4 vCPU, 3 GiB RAM, 30 GiB sparse disk, `vz` + `virtiofs`. No prune was run, and no other project's resources were touched (the new VM contains only this project's images). |
| Docker | client 28.1.1, engine 27.4.0, Compose 2.36.0 | — |
| Python / uv | 3.12.7 (`/opt/local/bin/python3.12`), uv 0.12.22 | `.python-version` = 3.12 |
| Ollama | 0.34.2 is running, **but the model store is empty: `qwen3:4b` is no longer present** | Not downloaded, as instructed. Phase 1 needs no LLM. **This blocks Phase 4** (see §9). |
| Git | 2.48.1 | `git init` (no remote). Third-party PDFs in `Literature/` are gitignored. |
| Paid APIs | none used | — |

## 2. What was built

```
smoke client ──HTTP──> gateway ──> inventory (GET /stock)
                          └──────> order ──> inventory (POST /reserve) ──> postgres
                                      ├────> payment   (POST /charge)
                                      └────────────────────────────────> postgres
services ──OTLP/HTTP──> otel-collector-contrib ──file exporter──> traces/logs/metrics.jsonl
docker.sock ──docker_stats──┘   postgres stdout ──fluentd driver──> fluent_forward ┘
```

- **Services:** four small FastAPI apps sharing one image. They have no framework beyond FastAPI, SQLAlchemy and httpx.
  - Order and inventory each own a SQLAlchemy pool: `pool_size=5`, no overflow, 2 s pool timeout, and `application_name` set to the service name.
  - Every downstream call has a 2 s timeout and one bounded retry, applied only to 5xx responses and transport errors.
  - Reservations and charges are idempotent per `order_id`, so retries are safe.
- **Instrumentation (OTel Python SDK 1.45.1, instrumentations 0.66b1):**
  - FastAPI server spans, httpx client spans with W3C `traceparent` propagation, and SQLAlchemy DB spans;
  - logs through the OTLP logging handler, which carries `trace_id`/`span_id`, with the same records also written as JSON to stdout;
  - metrics: `app.requests`, `app.errors`, the `app.request.duration` histogram, the semconv `http.server.request.duration` and `http.client.request.duration`, `process.cpu.*`, `process.memory.*`, thread and fd counts, GC counts, and DB pool gauges.
  - `/health` is deliberately excluded from all telemetry.
  - Every instrumentor is called with `raise_exception_on_conflict=True`, so a version mismatch fails at startup instead of silently dropping spans (see §7).
- **Collector (contrib 0.162.0):**
  - receivers: OTLP/HTTP, `docker_stats` every 5 s, and `fluent_forward` for Postgres logs;
  - processors: `memory_limiter` and `batch`;
  - one file exporter per signal, writing OTLP-JSON lines.
  - Its self-metrics on `:8888` are scraped for check C1.

### Configuration and resource limits

| Container | Image (immutable reference) | mem_limit | cpus |
|---|---|---|---|
| gateway, order, payment, inventory | `evidencerca-testbed:phase1`, built locally, image id `sha256:7f64c20a…` at run time; build inputs hashed in `environment_post.json` | 192 MiB each | 1.0 |
| postgres | `postgres:16.15-alpine3.24@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea` | 256 MiB | 1.0 |
| otel-collector | `otel/opentelemetry-collector-contrib:0.162.0@sha256:39923a8e431bd1f57be82411999d389fcfe40857492e4365456d97a4c1f74be6` | 224 MiB (memory_limiter 160 MiB) | 1.0 |
| service base image | `python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` | — | — |

The local image has no registry digest. Reproducibility comes from the pinned base digest, the hash-pinned `testbed/requirements.lock` (`pip install --require-hashes --only-binary=:all:`), and the SHA-256 of every build input, recorded in the manifest.

## 3. Smoke test (real transactions)

Command: `uv run python scripts/smoke_test.py` (driven by `run_phase1.sh`).

| # | Request | Status (expected) | Latency | Trace ID |
|---|---|---|---|---|
| 1–5 | `GET /api/stock/sku-00{1..5}` | 200 (200) | 3.9–49.0 ms (first call is cold) | `c6126f34…f554`, … |
| 6 | `POST /api/orders` sku-001 ×2 | 201 (201) | 89.1 ms (cold pool) | `63ac0804390fceb01f8563478f195843` |
| 7–10 | `POST /api/orders` sku-002 ×1, sku-003 ×3, sku-005 ×10, sku-001 ×1 | 201 (201) | 12.9–14.5 ms | `05eabfd9…`, `9c244ff0…`, `85e03999…`, `3ec27e61…` |
| 11 | `GET /api/orders/{id}` | 200 (200) | 5.4 ms | `6070bef841220848e0b42db761c51f9d` |
| 12 | `POST /api/orders` sku-004 ×999 (**deliberate** business-rule rejection) | 409 (409) | 4.3 ms | `cf620e35d1179682d280bff36cd9ae49` |

| Check | Result | Evidence |
|---|---|---|
| S1 every status as expected | pass | 12/12 |
| S2 orders confirmed with a payment id | pass | 5/5 |
| S3 W3C trace id on every response | pass | 12/12 |
| S4 no 5xx | pass | 0 |
| S5 orders persisted in Postgres (queried directly with `psql`) and matching the API | pass | 5 rows `confirmed`; amount and payment id match |
| S6 stock decreased by exactly the ordered quantities; the rejected order changed nothing | pass | sku-001 100→97, sku-002 100→99, sku-003 100→97, sku-004 50→50, sku-005 500→490 |
| S7 one reservation per order | pass | 5 = 5 |

## 4. Telemetry validation (from persisted files)

Command: `uv run python scripts/validate_telemetry.py`. Input: the three JSONL files, the smoke report and the collector self-metrics. Records read back: 130 spans, 203 log records (73 from OTLP and 130 from Postgres) and 885 metric points (511 from OTLP and 374 from docker_stats).

| ID | Signal | Check | Result | Key evidence |
|---|---|---|---|---|
| P1 | all | files exist and every line parses as OTLP JSON | pass | traces 2 lines, logs 26, metrics 19; SHA-256 values in the report |
| P2 | all | records read back = records the file exporters report as sent | pass | 130/130 spans, 203/203 logs, 885/885 points |
| T1 | traces | spans from every service | pass | gateway 29, order 38, inventory 58, payment 5 |
| T2 | traces | trace IDs 32 hex and span IDs 16 hex, non-zero, unique | pass | 130 spans, 0 invalid, 0 duplicate |
| T3 | traces | every trace ID returned to the client exists in the export | pass | 12/12 |
| T4 | traces | each smoke trace is one tree with a single gateway SERVER root and no orphans | pass | e.g. `63ac0804…` has 19 spans, 1 root, 0 orphans |
| T5 | traces | cross-service propagation: in every order trace, a CLIENT span is the direct parent of the callee's SERVER span for gateway→inventory, gateway→order, order→inventory and order→payment | pass | 5/5 order traces; e.g. order client `81d0ee411484d759` → payment server `4dfc357f135e70bd` |
| T6 | traces | `postgresql` CLIENT spans inside order and inventory | pass | `INSERT`/`UPDATE`/`SELECT shop` spans under the service's own SERVER span |
| T7 | traces | one `service.instance.id` per service; SERVER spans carry only that service's routes | pass | e.g. payment has only `/charge` |
| L1 | logs | logs from every service and from Postgres | pass | gateway 20, order 18, inventory 23, payment 12, postgres 130 |
| L2 | logs | app logs structured (severity, timestamp, body, typed attributes) | pass | 73/73. Postgres: structured envelope with a text body (see §8) |
| L3 | logs | every smoke trace has logs from each service in it; each log's `(trace_id, span_id)` resolves to a span of the same service and lies inside that span's time window | pass | 12/12 traces, 0 mismatches |
| L4 | logs | Postgres logged the pool connections of order and inventory during the run | pass | `connection authorized: … application_name=order` / `=inventory` |
| M1 | metrics | request count, latency histograms and process CPU/memory for every service | pass | 15–16 metric names per service |
| M2 | metrics | request counters equal the requests actually made, per service, route, method and status | pass | gateway `/api/orders POST 201` = 5, `409` = 1; inventory `/stock GET 200` = 11; payment `/charge` = 5; … |
| M3 | metrics | histogram count = counter per series, sums > 0, semconv histogram agrees | pass | mean latency: gateway order 27.1 ms, order 20.9 ms, reserve 9.5 ms, charge 1.4 ms |
| M4 | metrics | error counter equals the 1 expected client error; 0 server errors | pass | gateway `client` = 1; others 0 |
| M5 | metrics | container CPU, memory and network metrics (docker_stats) for every container during the run | pass | 12–13 container metrics each |
| C1 | collector | receivers accepted data, exporters sent it, 0 refused, 0 send failures | pass | OTLP spans 130, OTLP logs 73, fluent logs 130, docker_stats 374, OTLP metrics 511 |
| X1 | consistency | span start ≤ end; smoke spans inside the client's window (±500 ms); children nest in parents (±1 ms); metric start ≤ time | pass | 0 violations (host/VM clock offset in §8) |
| X2 | consistency | each service's `service.instance.id` is identical across traces, logs and metrics | pass | e.g. 4 services × 3 signals each with one id |
| E1 | all | no ERROR spans, no 5xx server spans, no ERROR logs; the only WARN log belongs to the deliberate rejection | pass | 1 WARN: gateway "order refused: insufficient stock" on trace `cf620e35…` |

### A real order trace (`63ac0804390fceb01f8563478f195843`)

```
gateway   SERVER POST /api/orders        071f5dcd3184a26f   +0.00 ms  87.79 ms
  gateway   CLIENT GET                   f8d978dc3186e492   +1.73     2.16
    inventory SERVER GET /stock/{item_id} 5b447430ed93f679  +2.07     1.58
      inventory CLIENT connect / SELECT shop
  gateway   CLIENT POST                  e4032575768bd291   +4.62    82.25
    order     SERVER POST /orders        ba73ba088f9a145b   +9.83    76.83
      order     CLIENT connect / INSERT shop
      order     CLIENT POST              3b143c333e0328b6  +32.97    41.74
        inventory SERVER POST /reserve   e4d6d700ca67d1e4  +34.41    39.94
          inventory CLIENT connect / INSERT / UPDATE / SELECT shop
      order     CLIENT POST              81d0ee411484d759  +75.46     8.43
        payment   SERVER POST /charge    4dfc357f135e70bd  +77.73     6.11
      order     CLIENT connect / UPDATE shop
```

There are 10 correlated log records in this trace: "order created", "stock reserved", "payment authorised", "order confirmed", "order placed", and one "request completed" per service. Each carries the `span_id` of the SERVER span that was active when it was emitted.

### Validator self-test

`uv run pytest tests` runs the validator on the recorded run (all checks must pass), then on six corrupted copies. Each corrupted copy must fail its targeted checks:
- payment spans removed → T1, T5;
- order spans orphaned → T4, T5;
- trace context stripped from logs → L3;
- a phantom request added to the smoke report → M2;
- one span marked ERROR → E1;
- one metrics line deleted → P2.

**7 passed.** This shows the checks can fail and do not pass vacuously.

## 5. Resource consumption

| | Idle (after start) | After smoke + 12 s |
|---|---|---|
| gateway | 58.9 MiB, 2.2 % CPU | 59.6 MiB |
| order | 72.7 MiB | 73.9 MiB |
| inventory | 70.6 MiB | 71.8 MiB |
| payment | 57.3 MiB | 57.5 MiB |
| postgres | 59.0 MiB | 65.5 MiB |
| otel-collector | 40.1 MiB | 41.4 MiB |
| **Total containers** | **~359 MiB** | **~370 MiB** (all under 40 % of their limits) |

- **Colima VM (3 GiB):** 376 MiB used and 2.5 GiB available during the run.
- **Docker images:** 982 MB in the VM. That covers the three pulled images (~810 MB) plus the built image, which shares the python base; the remaining untagged images are intermediate layers from this session's own rebuilds.
- **Telemetry volume:** 620 KB raw for this ~1 s smoke run, 39 KB as `.tar.gz`. Per-case volume at ~10 req/s still has to be measured in Phase 2.
- **Host disk:** 37 GB free after the run.
- **Wall-clock:** the full `run_phase1.sh` takes about 60 s after the image is built.

## 6. Reproducibility record

The manifests `results/phase1/environment_pre.json` and `environment_post.json` contain:
- macOS 26.5.2 (25F84), Darwin 25.5.0, Apple M1, 8 logical CPUs, 8 GiB;
- toolchain versions (Python 3.12.7 on the host and 3.12.15 in containers, uv 0.12.22, Docker 28.1.1/27.4.0, Compose 2.36.0, Colima 0.8.1 with its VM settings, Ollama 0.34.2 with an empty model list);
- all 52 locked package versions (runtime and dev);
- image references, digests and IDs;
- SHA-256 of the build inputs;
- git commit `db5531c`, with no uncommitted changes to tracked files;
- the resolved compose config, with the DB password redacted;
- service limits;
- host and VM memory, disk and load before and after.

Hostnames, user names and absolute paths are excluded. The `.env` holding the random local DB password is gitignored, and a scan of all result files for the password, user name and hostname found nothing. The test commands and their outputs are those listed in §3, §4 and the README.

## 7. Problems found during development and how they were fixed

1. **DB spans were silently missing.** uv resolved SQLAlchemy 2.1.4, but `opentelemetry-instrumentation-sqlalchemy` 0.66b1 supports only `< 2.1`. Its default behavior is to log and skip, so no DB spans were produced.
   - Fix: pinned `sqlalchemy>=2.0,<2.1` (2.0.54), and every instrumentor now raises on a dependency conflict.
   - Check T6 guards against a regression.
2. **The Mac slept mid-run.** In a dry run, macOS maintenance sleep (00:33:31–00:47:07 per `pmset -g log`) paused the Colima VM, and `docker compose stop` took about 14 minutes. The validation still passed.
   - Fix: `run_phase1.sh` now holds `caffeinate -is` for its duration. This matters more for the multi-hour Phase 2 captures.
3. **L4 initially matched the wrong connections.** It was satisfied by `pg_isready` healthcheck connections.
   - Fix: pools now set `application_name`, and L4 requires the order and inventory pool connections specifically.
4. **Validator rigor was tightened.** The host/VM time slack was reduced from 2 s to 500 ms. P2 was added: an exact reconciliation between the exporter-sent counts and the records read back.

Two dry runs (`runs/phase1-dryrun*/`, gitignored) were used for development. Only the recorded run above is reported.

## 8. Deviations from the research plan and known limitations

| Plan (research_plan.md §4.1 / §2) | Phase 1 | Reason / impact |
|---|---|---|
| Workload generator (seeded Poisson, mixed requests) is part of the testbed | **Not built yet.** Phase 1 uses a fixed 12-request smoke plan. | It is only needed for baseline and fault traffic. I propose building it at the start of Phase 2. |
| Telemetry written to a named volume and copied out (sshfs was slow) | Bind mount to `runs/<run_id>/telemetry` | The new VM uses virtiofs, so the slow-mount reason no longer applies. Simpler, and the data persists directly. |
| Colima VM with ~3.8 GiB | 3 GiB | Host memory pressure. The testbed uses ~0.4 GiB. It can be raised for Phase 2 fault headroom. |
| OTLP (protocol unspecified) | OTLP over HTTP/protobuf | Avoids `grpcio`, keeping the image smaller. |
| — | SQLAlchemy pinned to 2.0.x | §7.1 |
| — | Postgres data on tmpfs | Every run starts from the identical seed state, which suits per-case resets in Phase 2. |
| — | Collector runs as root | Needed to read the VM's `docker.sock` for docker_stats. It is local only, and its ports are bound to 127.0.0.1. |
| — | docker_stats excludes the collector's own container | Avoids self-observation noise. It can be re-enabled if needed. |

Known limitations to carry into Phase 2 and 3:
- **Postgres logs** are plain text inside a structured fluentd envelope. The envelope timestamp has **1 s resolution**, while the millisecond timestamp is inside the text. A parser will be needed if DB logs are used as evidence.
- **Host↔VM clock offset.** Gateway span times are consistently about 17 ms ahead of the host client's clock (+17.5 ms at send, −16.3 ms at receive). Fault-injection timestamps (ground truth) should therefore be taken from the VM clock, or the offset must be recorded per case.
- **Cold start.** The first request after start is slower (49 ms for a stock lookup, 89 ms for an order) because of pool connection setup. Phase 2 warm-up must precede any baseline window.
- **Short smoke run.** Telemetry volume and collector headroom at sustained load (~10 req/s) remain **unmeasured**.

## 9. Blockers and decisions for the next phase

1. **`qwen3:4b` is no longer installed** (Ollama's model list is empty). It is not needed until Phase 4. Re-pulling it (~2.5 GB) needs your approval, or you can restore it yourself.
2. Phase 2 (fault injection, ground-truth validator, leakage test, workload generator) **has not been started** and needs your approval.

The containers are stopped and removed (`docker compose down`). Images, configs and all run data are kept. The Colima VM is still running and can be stopped with `colima stop`.
