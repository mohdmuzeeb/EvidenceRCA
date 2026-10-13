# Fault Catalog (Phase 2)

This catalog covers the six fault types of the research plan (§4.2) and the no-fault control. Code lives in `harness/faults.py` (injectors) and `harness/validate.py` (independent validation). The pilot and mechanism-check outcomes are in [`phase2_validation.md`](phase2_validation.md).

## Principles

1. **Scope.**
   - Every container action goes through `docker_ops.project_container()`. It refuses any container that lacks the `com.docker.compose.project=evidencerca` label for the expected service.
   - VM-level actions run either inside one project container's network namespace (`nsenter -t <pid> -n`), or as reads of that container's cgroup files.
   - The database fault touches one table of the testbed database.
   - No host-wide or destructive command is used: no prune, no iptables, no host qdisc, no host CPU load.
2. **Inject, then validate independently.** An injector only *acts* and records a VM-clock timestamp taken by the mechanism itself. Whether the fault *occurred* is decided afterwards from observations made through a different mechanism (table below).
3. **Explicit, verified cleanup.** Every fault has a cleanup action. Recovery is verified before a case is accepted:
   - the fault's signature is absent in every sample once recovery settles (15 s after cleanup);
   - injector state reads back as clear: no delays, no held connections, no busy processes;
   - all containers are running and healthy;
   - the workload error rate and p95 latency over the last 30 s of recovery are back near baseline.

   Each case also starts from freshly created containers and a tmpfs database, and refuses to start if any project container is left over.
4. **No observable harness footprint.** Agent-visible telemetry must not reveal the label or the onset through the harness itself. See "Leakage controls" below and `results/phase2/leakage_test_results.json`.

## Catalog

| Fault (`fault_type`) | Eligible targets (protocol §3) | Injection | Parameters | Cleanup | Independent validation (decides "occurred") | Ground-truth root |
|---|---|---|---|---|---|---|
| `none` (control) | — | none | — | none | **No** detector fires for any service in any window | — |
| `service_crash` | order, payment, inventory | `docker kill --signal KILL` on the target container | SIGKILL (exit 137) | `docker start`, then wait until the healthcheck is `healthy` | (1) TCP connect from the workload container to `target:8000` fails in ≥ 80 % of steady-injection probes and in none before or after. (2) Docker state is "not running" over the same window. | target service |
| `processing_latency` | gateway, order, payment, inventory | Control channel: `/delay` adds a fixed sleep inside the request middleware for one operation (inside the SERVER span) | +500 ms on `POST /api/orders` (gateway), `POST /orders` (order), `POST /charge` (payment), `POST /reserve` (inventory). Below the 2 s caller timeout. | `/delay/clear` | (1) The client-side median latency of order requests (whose path traverses the operation) rises by ≥ 400 ms in the workload's own records. (2) The target's SERVER-span median for that route rises by ≥ 400 ms in raw telemetry. (3) The client latency shift after cleanup is < 80 ms. | target service + operation |
| `network_timeout` | order, payment, inventory | `tc qdisc add dev eth0 root netem delay 2500ms`, run from the VM host inside the target's network namespace only | 2500 ms egress delay, above the 2.0 s caller timeout. The target needs no `NET_ADMIN` and no extra tools. | `tc qdisc del dev eth0 root` (same namespace) | (1) TCP connect time to `target:8000`, measured from another container, is ≥ 2000 ms in ≥ 80 % of steady-injection probes and in none before or after (SYN-ACK leaves through the delayed qdisc). (2) A `tc qdisc show` readback is recorded but not counted as independent, because it uses the injecting tool. | target service |
| `db_pool_exhaustion` | order, inventory | Control channel: `/hold` checks out all pooled connections and keeps each in an open transaction (under `suppress_instrumentation`, so no spans) | 5 of 5 connections held (`pool_size=5`, `max_overflow=0`, `pool_timeout=2s`) | `/release` (rollback + return to pool) | Postgres's own view: `pg_stat_activity` shows ≥ 5 `idle in transaction` sessions with `application_name=<target>` in ≥ 80 % of steady-injection samples and in none before or after | target service (operation: DB pool) |
| `cpu_pressure` | order, payment, inventory | `docker exec` starts 2 busy-loop Python processes in the target, which is capped at `cpus: 1.0`. Each self-terminates after injection + 15 s. | 2 busy processes vs a 1.0-CPU cap (CFS throttling) | Killed from the VM host using the target cgroup's process list (nothing is executed in the container) | cgroup `cpu.stat` of the target, read on the VM host: CPU usage ≥ 0.8 cores in ≥ 80 % of steady-injection samples and in none before or after | target service |
| `cpu_pressure`, variant `strong` (Phase 3) | order, payment, inventory | As above, **plus** the target's own CFS quota is lowered to 0.2 CPU (`docker update --cpus 0.2`) for the injection | 2 busy loops; quota 0.2 CPU | Kill the busy loops (VM host), then restore the exact original quota (`HostConfig.NanoCpus`, verified) | cgroup `cpu.stat` throttled-period ratio ≥ 0.5 in ≥ 80 % of steady-injection samples, absent before and after (usage cannot reach 0.8 cores under a 0.2 cap) | target service. `fault_type` stays `cpu_pressure`; the variant is in `params` |
| `cascading_failure` | postgres (root) | A pre-opened psql session runs `BEGIN; LOCK TABLE items IN ACCESS EXCLUSIVE MODE;` and keeps the transaction open. Inventory's reads and reservations block, then callers time out and retry, and the gateway returns errors. | ACCESS EXCLUSIVE lock on `items` | `COMMIT` in the same session | Postgres lock table (`pg_locks` from a separate session): an ACCESS EXCLUSIVE lock on `items` held by a non-service session while service sessions wait, in ≥ 80 % of steady-injection samples and in none before or after | `postgres`. Inventory, order and gateway carry propagated symptoms (analysis-only field). |

**What "≥ 80 % of steady-injection samples, none before or after" means:**
- *Steady injection* excludes the first 15 s after injection.
- *Before* is the whole baseline window.
- *After* is recovery from 15 s after cleanup.
- Monitor samples are taken every 5 s; TCP probes also run every 5 s, inside the VM.

## Ground-truth record (per case, `data/ground_truth/<case_id>/ground_truth.json`)

Each record contains:
- `scenario`: master seed, split, fault type, target, replicate, derived workload seed;
- `fault_type`, `primary_root_services[]`, `root_operation`, `params`;
- `timeline_vm_ns`: workload start, warm-up end, injection start/end (from the mechanism's own VM-clock timestamp), recovery end, planned onset;
- `injection_actions`: what each injector and cleanup reported;
- `clock_offsets`: VM minus host at case start and end, with uncertainty;
- `validation`: independent checks with evidence, the full detector × service × window matrix, and the signatures seen during injection;
- `observable_impact`: whether the gateway SLO deviated (unexpected-outcome rate +5 pp, or p95 ×2 versus baseline), from the workload's client-side records;
- `recovery`: the checks above;
- `propagated_symptom_services` and `propagation_analysis_only`: services whose SERVER spans deviated during injection. These are **analysis only**; they are not ground truth for scoring;
- `workload_summary`, `telemetry_check` (completeness, loss and throughput), `raw_bundle` (manifest hash) and `case_valid`.

## Leakage controls built into the injectors

| Risk | Control |
|---|---|
| Labels in IDs or paths | `case_id` is a random UUID4 created before injection. The raw bundle path is `data/cases/<case_id>/`. The ground truth sits under a sibling root, `data/ground_truth/<case_id>/`. |
| Injector metadata in the raw bundle | The raw manifest holds only `{schema, case_id, files}`. The input builder rejects any other key. |
| Control traffic in telemetry | The control channel is a stdlib HTTP server bound to `127.0.0.1:9000` inside each container. It is not instrumented and is silent (no logs). It is reached from the VM host through `nsenter -n` (network namespace only), so no process starts in the container and no CPU blip appears in container metrics. The pool hold runs under `suppress_instrumentation()`. |
| Harness Postgres sessions in DB logs | The monitor and injector psql sessions are opened once, before warm-up, in **every** case (control included). Both disable their own statement logging. The resulting two `connection authorized … application_name=psql` lines are identical across cases and precede the workload. |
| Workload/probe container in metrics | It runs as image `evidencerca-harness:phase2`, excluded by the collector's `docker_stats` receiver. It is not instrumented and logs only to Docker's json-file driver. TCP probes do not reach Postgres, whose `log_connections` would record them. |
| Timing marks from the monitor | All monitor sampling (Docker inspect, VM-host cgroup/qdisc/control reads, `pg_stat_activity`) runs at a fixed 5 s cadence in every case, from warm-up to the end, independent of injection time. |
| Remaining unavoidable footprint | `cpu_pressure` must start processes in the target (that *is* the fault). `service_crash` necessarily restarts the container, so a "service started" log line appears at recovery. Both are symptoms of the fault, not labels. |
