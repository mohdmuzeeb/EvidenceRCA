# Phase 2: Workload, Fault Injection, Ground Truth and Leakage Prevention

| | |
|---|---|
| **Outcome** | **PASS.** Pilot: 8/8 cases valid (4 scenarios × 2 repeats, seed 11 = dev). Mechanism checks: 6/6 fault types valid. Sustained load (20 req/s): valid, no telemetry loss. Leakage checks: all pass (35 leakage tests; 42 tests in total). |
| Machine-readable results | [`results/phase2/pilot_results.json`](../results/phase2/pilot_results.json), [`ground_truth_validation.json`](../results/phase2/ground_truth_validation.json), [`leakage_test_results.json`](../results/phase2/leakage_test_results.json), [`environment.json`](../results/phase2/environment.json), [`phase1_regression/`](../results/phase2/phase1_regression/) |
| Fault definitions | [`fault_catalog.md`](fault_catalog.md) |
| Protocol changes | Logged in [`experiment_protocol.md` §12](experiment_protocol.md#12-deviation-log) **before** the pilot was captured |
| Reproduce | See the README, "Phase 2" section |

Not done, as instructed: no RCA agent, evidence verifier, LLM integration or Phase 3 work, and no full six-seed matrix. Only the dev seed (11) was used. Calibration and test seeds are refused by the code unless explicitly allowed.

## 1. Phase 1 artifacts verified first

- The git tree was clean at `dec9d67`, and the Phase 1 tests passed (7/7).
- The SHA-256 of the archived Phase 1 telemetry matches `telemetry_validation.json` for all three files.
- `results/phase1/` was not modified.
- After the Phase 2 testbed changes (§3), the Phase 1 run was repeated into `results/phase2/phase1_regression/`: **smoke 7/7, telemetry 22/22**. That run is the final testbed code, the one the pilot used.

## 2. What was built

| Part | Where | Notes |
|---|---|---|
| Workload generator | `harness/vm/workload.py` | Runs **inside the VM** (VM clock), uninstrumented, excluded from docker_stats. Seeded open-loop Poisson arrivals, with the whole schedule generated before the first request (`schedule_sha256`). Default 10 req/s, max 32 in flight; excess arrivals are shed and recorded. Mix: stock lookups, orders, order lookups, invalid orders (404/409/422). Records offered vs achieved throughput, error rates and p50/p95. Also runs TCP-connect probes every 5 s. |
| Fault injectors (6) + cleanup | `harness/faults.py` | Crash, processing latency, network timeout, DB pool exhaustion, CPU pressure, cascading failure. See the catalog. |
| Control channel | `testbed/app/control.py` | Stdlib server on 127.0.0.1:9000 inside each service. Uninstrumented and silent. Reached from the VM host via `nsenter -n`, so no process starts in the container. |
| Symmetric ground-truth monitor | `harness/monitor.py` | Every 5 s, in every case: Docker state; VM-host reads of cgroup CPU, qdisc, control state and busy processes; `pg_stat_activity`/`pg_locks` through a pre-opened session. `docker stats` every 15 s, host memory every 30 s. |
| Independent validator | `harness/validate.py` | Fault present during steady injection (≥ 80 % of samples), absent before and after. Recovery checks, observable impact, analysis-only propagated symptoms, and capture continuity (sleep/stall detection). |
| Case runner | `harness/case.py`, `harness/schedule.py` | Fresh stack per case; opaque UUID4 `case_id`; raw bundle sealed read-only with a hash-only manifest; separate ground-truth root. Pre-registered seed splits, with a guard against non-dev seeds. |
| Telemetry completeness | `harness/telemetry_check.py` | Collector→disk reconciliation, refused/failed counts, trace completeness, per-window/per-service/per-modality presence, answered-call→server-span loss, SDK drop scan. |
| RCA input builder | `evidencerca/rca_input.py` | The only sanctioned case reader for agent-side code. Allowlisted root, UUID4 ids, symlink-safe, manifest keys allowlisted, hashes verified. |
| Leakage checks | `harness/leakage.py`, `scripts/leakage_check.py`, `tests/test_leakage.py` | §6 |
| Re-evaluation | `harness/reevaluate.py` | Recomputes verdicts from recorded files after an analysis-code fix. Raw data is only read, and each run is logged in the ground-truth record. |

## 3. Changes to the Phase 1 testbed (each with its reason)

| Change | Reason | Effect on Phase 1 checks |
|---|---|---|
| **Unhandled exceptions are now caught in the request middleware**: recorded on the server span, logged at ERROR with traceback through OTLP, counted in `app.requests`/`app.errors`, answered with 500 | **Instrumentation gap found in Phase 2.** A pool-timeout `TimeoutError` propagated past the middleware, so the request produced *no OTLP log* (only uvicorn's non-propagating logger printed it) and was missing from the app request/error counters. The most important error evidence was invisible. | Regression run: 22/22 |
| Loopback control channel (`control.py`) and a middleware delay hook | Needed for the processing-latency and pool-exhaustion faults, without network exposure or telemetry | 22/22 |
| **Docker socket proxy** (`tecnativa/docker-socket-proxy:v0.5.0`, pinned by digest). The collector no longer mounts `docker.sock` and **no longer runs as root**. | Item 10 (minimize socket exposure). The proxy allows only GET on containers/version/ping/events. Probed: `POST …/exec`, `POST …/kill`, `DELETE`, `/images`, `/info` and `/volumes` all return **403**. The proxy itself still needs the socket (read-only mount, not on any published port, reachable only on the compose network). | 22/22 |
| docker_stats excludes the harness and proxy images | Infrastructure must not appear in agent-visible metrics | 22/22 |
| Seed stock raised to 1,000,000 (sku-004 stays at 50) | ~5 min at 10 req/s would exhaust 100 units | Smoke test unchanged (it reads initial stock) |
| `run_phase1.sh` takes a `RESULTS` override | Lets the regression run without touching `results/phase1/` | — |

All service ports stay bound to 127.0.0.1. The control port is loopback-only *inside* each container, and a probe from another container on the network gets `ConnectionRefused`.

## 4. Pilot results (seed 11, protocol timeline: 30 s warm-up / 90 s + 0–10 s jitter baseline / 120 s injection / 60 s recovery)

Targets were drawn by the pre-declared seeded rule (`schedule.pilot()`); the draw picked `inventory` for all three faults. Each scenario was captured twice (runs A and B) with new case IDs.

| Scenario | Run | case_id | Valid | Occurred (independent checks) | Recovered | Unexpected-outcome rate, baseline → steady injection | p95, baseline → injection | Observable impact |
|---|---|---|---|---|---|---|---|---|
| control | A | `be737f04` | ✅ | n/a (3/3: no signature anywhere) | ✅ | 0 → 0 | 57 → 40 ms | no |
| control | B | `77092a17` | ✅ | n/a (3/3) | ✅ | 0 → 0 | 37 → 40 ms | no |
| processing_latency, inventory `POST /reserve` (+500 ms) | A | `a4f3f9b6` | ✅ | ✅ 5/5 | ✅ | 0 → 0 | 37 → 547 ms | yes |
| processing_latency, inventory | B | `21cca7a8` | ✅ | ✅ 5/5 | ✅ | 0 → 0 | 38 → 544 ms | yes |
| service_crash, inventory | A | `70a3e296` | ✅* | ✅ 4/4 | ✅ | 0 → 86.3 % (923 × 5xx) | 39 → 133 ms | yes |
| service_crash, inventory | B | `ce9aaa1b` | ✅ | ✅ 4/4 | ✅ | 0.1 % → 86.3 % (923 × 5xx) | 39 → 132 ms | yes |
| db_pool_exhaustion, inventory (5/5 held) | A | `c4a78010` | ✅ | ✅ 3/3 | ✅ | 0 → 82.9 % (710 × 5xx) | 37 → 4139 ms | yes |
| db_pool_exhaustion, inventory | B | `3e64441f` | ✅ | ✅ 3/3 | ✅ | 0 → 83.2 % (709 × 5xx) | 37 → 4142 ms | yes |

\* `70a3e296` was first marked invalid only because of a harness shutdown race (§7.5). It was re-evaluated from its recorded files; nothing was recaptured.

**Per-fault independent evidence** (full detail in `ground_truth_validation.json`):
- **Latency:** client-side median latency of order requests rose by about 500 ms, and inventory's `POST /reserve` SERVER-span median rose by the same amount. Both returned to baseline after cleanup.
- **Crash:** TCP connect to `inventory:8000` failed in ≥ 80 % of steady-injection probes, and Docker showed the container as not running over the same window. Neither signature was present before injection or after recovery.
- **Pool exhaustion:** `pg_stat_activity` showed 5 `idle in transaction` sessions with `application_name=inventory` during injection, and none before or after.
- **All cases:** no detector fired in the baseline or after recovery settled (contamination check), and the controls showed no signature in any window.

**Observed propagation (analysis only):**
- latency on inventory → gateway, order;
- crash or pool exhaustion on inventory → gateway, payment.

Payment is listed because no request reached it while inventory failed (zero traffic counts as deviation in this analysis-only field).

**Workload:** offered 10 req/s, achieved 9.5–10.05 req/s.
- **No shedding** in controls, latency or crash cases.
- **Pool exhaustion:** 235 and 241 arrivals shed, because 4 s timeouts filled all 32 slots. The shed arrivals are recorded and are part of the impact.

### Repeatability (A vs B, same seed)

| Scenario | Identical offered schedule (sha256) | Same validation / recovery / impact / propagation | Steady-injection p95, A vs B | Span count difference |
|---|---|---|---|---|
| control | ✅ | ✅ / ✅ / ✅ / ✅ | 40.3 vs 40.1 ms (0.4 %) | 0.0 % |
| processing_latency | ✅ | ✅ / ✅ / ✅ / ✅ | 546.5 vs 544.4 ms (0.4 %) | 0.0 % |
| service_crash | ✅ | ✅ / ✅ / ✅ / ✅ | 133.3 vs 131.6 ms (1.3 %) | 0.2 % |
| db_pool_exhaustion | ✅ | ✅ / ✅ / ✅ / ✅ | 4138.8 vs 4142.0 ms (0.1 %) | 0.1 % |

## 5. Mechanism checks (all six faults, short timeline 15/40/45/40 s; not experiment data)

| Fault (target) | case_id | Valid | Independent evidence | Recovered | Impact (unexpected rate in injection, p95) |
|---|---|---|---|---|---|
| processing_latency (payment `/charge`) | `bf256e6a` | ✅ 5/5 | +~500 ms on the client and on the span; back after cleanup | ✅ | 0 %, 564 ms |
| service_crash (order) | `638e4132` | ✅ 4/4 | TCP unreachable + not running | ✅ | 57.5 %, 136 ms |
| db_pool_exhaustion (order) | `2be489f1` | ✅ 3/3 | 5 × idle-in-transaction for `order` | ✅ | 43.8 %, 4170 ms |
| network_timeout (payment, netem 2500 ms) | `d0d96d34` | ✅ 4/4 | TCP connect ≥ 2 s to payment (523 of 535 caller attempts failed at connect) | ✅ | 39.6 %, 4134 ms |
| cpu_pressure (order) | `915ee773` | ✅ 3/3 | cgroup usage ≥ 0.8 cores | ✅ | 0 %, 79 ms (**below the impact rule**) |
| cascading_failure (postgres lock on `items`) | `a2d0582d` | ✅ 3/3 | ACCESS EXCLUSIVE on `items` held by a non-service session, with service sessions waiting | ✅ | 75.4 %, 4139 ms |

Six earlier mechanism runs are listed as `superseded_development_runs`, with reasons, in `pilot_results.json`. One more run was aborted and quarantined (`data/aborted/`). None are counted.

## 6. Leakage prevention and tests

**Layout:**
- `data/cases/<uuid4>/` holds the agent-visible bundle: three telemetry files plus a manifest with only `{schema, case_id, files}`. It is sealed read-only.
- `data/ground_truth/<uuid4>/` holds the labels, injector actions, validation and harness records.
- Derived views (Phase 3) will go to `data/views/`. Raw files are never modified, and the builder verifies their hashes.

| Check (`leakage_test_results.json`) | Result |
|---|---|
| Forbidden-vocabulary scan of every agent-visible byte (fault-type names, "fault", "inject", "netem", "crash", "exhaust", "cascad", the burner token, control routes, "lock table", "harness", "workload", …) for 8 pilot + 12 mechanism + 1 load bundles | **0 hits** |
| The same scan over the RCA input builder's output | 0 hits; top-level keys are only `case_id, manifest_sha256, signals` |
| Structural checks per bundle | Only gateway SERVER root spans (no spans from the control channel), no control routes, only system-under-test containers in docker_stats, all Postgres `psql` connections before the workload starts |
| Cross-case consistency | Identical file sets, identical resource-attribute keys, and an identical Postgres harness footprint (5 `psql` connections: 3 from the image's init scripts and 2 pre-opened harness sessions) in every case |
| Path-guard attacks on the input builder (11) | **All blocked**: traversal id, absolute GT path, encoded traversal, non-canonical id, empty id, suffix traversal, telemetry file symlinked to GT, manifest with an injector key, manifest entry into GT, case dir symlinked to GT, cases root set to the GT root |
| Static check: agent-side packages (`evidencerca/`, future `rca/`, `baselines/`) | No import of `harness`/`scripts` and no ground-truth reference outside `paths.py`/`rca_input.py`. A planted violating module is flagged (test). |
| Scanner non-vacuity | Planted tokens detected; ordinary symptom words such as "timeout", "latency" and "default" are not flagged |
| `pytest tests/test_leakage.py` | **35 passed** |

## 7. Problems found and fixed (none hidden)

1. **Instrumentation gap: unhandled exceptions were invisible** in logs and app metrics (§3). Fixed in the testbed, and the Phase 1 regression re-passed. All mechanism cases captured before the fix were recaptured.
2. **The control channel would have left a CPU blip in the target at injection time.** `docker exec` starts a Python process inside the container. Control calls now use `nsenter -n` from the VM host, and CPU-burner cleanup also runs from the VM host.
3. **A harness Postgres connection was logged at injection time.** `PGOPTIONS` does not suppress `connection authorized`. Fix: two sessions are pre-opened before warm-up in every case, and they disable their own statement logging.
4. **Self-matching process counter.** The busy-process counter and killer matched their own command line. Both now skip their own PID; the counter moved to the VM host.
5. **Shutdown race.** The `docker stats` side thread wrote after the monitor file closed, incrementing an error counter. `stop()` now joins side threads, and monitor errors are counted from recorded error records.
6. **Telemetry-completeness rule refined twice.** Both refinements came from evidence, and both keep real loss detectable:
   - spans/logs are only expected from a service that actually received a request;
   - for network_timeout, connections failed at the handshake, so no request reached payment.

   A new check requires every *answered* call to have its server span. It found 4–8 spans per crash case that were lost from the killed process's 1 s export buffer. These are classified as fault-caused loss and counted, not hidden.
7. **The Mac slept during captures.** Twice: a lid close on battery at 20:15:44, and idle sleep between stages at 20:49:43. One mechanism case spanned the sleep and was rerun. A **capture-continuity check** now invalidates any case with a monitor gap > 15 s or a workload overrun > 10 s. Run long captures under one `caffeinate -dims` wrapper, with the lid open, on AC.
8. **Scanner false positives**: "fault" inside "default", and a hard-coded harness `psql` count. Fixed with token boundaries and a cross-case equality rule.

Verdicts for cases whose *analysis* changed after capture were recomputed by `harness/reevaluate.py` from the recorded files, and each recomputation is logged in that case's `reevaluations`. No raw telemetry was altered, and no case that failed for a capture-side reason was relabelled. Those were all recaptured.

## 8. Telemetry completeness, throughput and resources

| Measure | Pilot cases (10 req/s, ~304 s workload) | Sustained load (20 req/s, 140 s) |
|---|---|---|
| Collector → disk | read back = exporter sent (all cases); 0 refused, 0 send failures | same |
| Trace completeness | every response with a trace id has its gateway root span (0 missing in all cases) | 0 missing |
| Throughput to disk | 79–112 spans/s, 47–58 log records/s, 36–40 metric points/s | 218 spans/s, 112 logs/s, 41 points/s |
| SDK drop messages | none | none |
| Raw bundle size | 39–41 MB (control, latency, crash); 66 MB (pool exhaustion, with tracebacks) | 38 MB |
| Collector peak | ≤ 57 MiB, ≤ 14 % CPU (limit 224 MiB, 1 CPU) | 56 MiB, 11.7 % |
| All containers, peak | 458–530 MiB in total; the largest single one is inventory at 108 MiB / 192 MiB during pool exhaustion | 468 MiB |
| VM (3 GiB) | min MemAvailable 1.86 GiB | 1.96 GiB |
| Host (8 GB) | macOS memory-status free level ≥ 32 %; heavy compression from other apps (~3 GiB) | ≥ 34 % |
| Wall-clock | ~336–346 s per pilot case, plus ~10 s setup; 174 s per mechanism check | 195 s |
| Disk | `data/` = 781 MB in total (8 pilot, 13 mechanism, 1 load bundle); host free 32 GB; VM disk 2.0 / 29 GB | |

**Projection for the full matrix (not run):**
- disk: 132 cases × ~45 MB ≈ **6 GB** uncompressed (gzip is about 10×, per Phase 1);
- capture time: ~13 h of pure capture at ~6 min per case.

Ollama was not running during any capture, no model was downloaded, and no paid API was used.

## 9. Clock offsets (VM − host)

All ground-truth times are taken on the VM clock, from the injection mechanism's own timestamp. Offsets are recorded at case start and end and in every 5 s monitor sample:
- **The offset was +90 to +660 ms** over this session. Phase 1 measured 17 ms; the offset grows after macOS sleep and is not corrected by Colima.
- Bracket uncertainty is about ±120–160 ms (the `colima ssh` round trip).
- Onset relative to plan was −120 to +400 ms, which is host scheduling error; the *recorded* onset is the VM-side timestamp.

Phase 3 must use only VM-clock times; the workload and all telemetry already are.

## 10. Remaining limitations

1. **CPU pressure barely moves the SLO.** 2 burners at a 1-CPU cap gave p95 79 ms vs 40 ms baseline, below the "observable impact" rule. Such cases will likely be `not_triggered` for an alert-anchored RCA window (protocol §2). Options: more burners, or keep the setting and report it. **Your decision** before the matrix is captured.
2. **The pilot targets all happened to be `inventory`** (seeded draw). The mechanism checks covered payment, order and postgres targets; the full matrix covers all eligible targets.
3. **Propagated-symptom analysis** marks zero-traffic services as deviated, for example payment while inventory is down. The field is analysis-only.
4. **Fault-caused span loss on crash:** up to 8 spans per case are lost in the killed process's export buffer. This is realistic, but RCA methods will see slightly incomplete traces at the onset.
5. **Postgres** still runs `log_connections=on`, so its init and pre-opened sessions leave 5 identical lines per case. They are harmless (identical across cases) but are agent-visible harness/system noise.
6. **Collector privileges remaining:** none beyond a non-root container with a write bind mount. The socket proxy holds the Docker socket read-only and exposes only container reads.
7. **Code and capture provenance:** the pilot ran on the working tree before it was committed. Image IDs and SHA-256 hashes of every build input are in `results/phase2/environment.json`, and the code is committed together with these results.
8. **Host sleep remains an operational risk** (§7.7). The continuity check makes it detectable, not impossible.
9. **qwen3:4b is still not installed.** This is irrelevant until Phase 4.

## 11. Next phase (needs approval)

Phase 3 covers the evidence store, views and perturbations, B1–B3, the verifier, EvidenceRCA without the LLM, and the agent-side alert detector.

Decisions for you:
- the CPU-pressure intensity (§10.1);
- whether to capture the calibration/test matrix (~13 h, ~6 GB) before or after Phase 3 development on dev-seed data.
