# EvidenceRCA — Research Plan (Phase 0)

Working title: *EvidenceRCA: An Uncertainty-Aware, Evidence-Verifiable Agent for Root-Cause Analysis Under Incomplete Microservice Telemetry*

Status: Phase 0 complete (planning only). Nothing in this document is an experimental result. No code, containers, model downloads or paid API calls have been made.
Date: 2026-10-11

Companion documents: [literature_matrix.md](literature_matrix.md), [experiment_protocol.md](experiment_protocol.md).

---

## 1. Environment inspection (2026-10-11)

| Item | Observed | Implication |
|---|---|---|
| OS / arch | macOS 26.5.2 (build 25F84), arm64 | All container images must be arm64/multi-arch |
| CPU | Apple M1, 8 cores (8 physical reported) | Adequate for 5 small services + collector |
| RAM | 8 GB total | **Constraint.** Docker VM (4 GB) and a local LLM (~3 GB resident for a 4B model) should not run at the same time |
| Free disk | **8.4 GB free of 228 GB (96 % used)** | **Blocker-level constraint** (see §8) |
| Python | 3.12.7 and 3.13.3 available; `uv` 0.12.22 | Use Python 3.12 via `uv` (also RCAEval's recommended version) |
| Docker | Docker 28.1.1 client, Compose 2.36.0, engine 27.4.0 running in **Colima** (Virtualization.Framework, aarch64, 4 vCPU, ~3.8 GiB VM RAM, sshfs mounts) | Docker Compose testbed is feasible; sshfs bind mounts are slow, so write telemetry to a named volume and copy it out per case |
| Docker disk use | 40 images (4.0 GB, 3.35 GB reclaimable), 631 MB of reclaimable build cache, 3 running containers (not ours) | Pruning could recover about 4 GB, **but only with your approval** (the images may be yours) |
| git | 2.48.1; **project is not a git repository** | `git init` is needed so every run can record its commit hash |
| LLM credentials | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`: all **unset**. `~/.aws/credentials` file **exists**; I did not inspect it or probe Bedrock access | No paid provider is configured. Claude Code access is **not** assumed to include Anthropic API access |
| Local LLM | Ollama installed (`/usr/local/bin/ollama`), model `qwen3:4b` (digest `359d7dd4bcda`, 2.5 GB) already present | A zero-marginal-cost LLM mode is possible with no download. A 4B model is a weak reasoner, so results must be reported as "with a 4B local model" |
| Other tools | `pdftotext` absent (I used an ephemeral `uv` + `pypdf` run in a scratch directory; nothing was installed into the project); Java and Node present; Go absent | — |

Hardware and software fingerprints (OS build, CPU, RAM, Docker/Colima versions, Python and package lockfile, Ollama version and model digest, git commit) will be captured automatically into every run manifest from Phase 1 onwards.

---

## 2. Research hypothesis (unproven)

> **H.** When telemetry is missing or conflicting, an RCA agent that (a) grounds every factual claim in evidence records, (b) checks those claims with an independent programmatic verifier, and (c) abstains when evidence is insufficient achieves lower selective risk at matched coverage and fewer unsupported factual claims than methods without verification or abstention. Localization accuracy at full coverage does not degrade materially.

This is a hypothesis. It may be refuted (see §3.3: a closely related abstention hypothesis was refuted in an independent study). The plan treats a negative or null result as reportable.

### Research questions
- **RQ1**: Top-1/top-3 localization and fault-type macro-F1 compared with reproducible baselines on identical cases.
- **RQ2**: Robustness under random and structured telemetry loss, timestamp jitter, and conflicting signals.
- **RQ3**: Reduction of unsupported factual claims through programmatic verification, audited against human labels.
- **RQ4**: The risk–coverage trade-off under abstention, with thresholds set on a calibration split and evaluated on held-out cases.
- **RQ5**: Runtime (p50/p95), tokens, and financial cost.

---

## 3. Refined novelty assessment

### 3.1 Not novel in isolation (prior work exists; we must cite and not claim)

| Component | Prior work that already does it |
|---|---|
| Multimodal (metrics/logs/traces) fusion | DiagFusion (TSC 2023); CausaLLM-Graph (COMPSAC 2026); PDiagnose and Eadro (cited in DiagFusion and CauseLens) |
| Dependency or causal graph plus random walk or PageRank | MicroRCA (NOMS 2020); MicroCause (IWQoS 2020); MonitorRank and CloudRanger (cited) |
| Graphs built from observed traces | DiagFusion (trace + deployment), CauseLens (operation/entity graph from span parent–child links) |
| Abnormal-vs-normal trace spectrum ranking | TraceRCA (IWQoS 2021) |
| Missing-modality robustness | DiagFusion §VI-B (62 cases with no metrics); its authors claim it works with any 2 of 3 modalities |
| Propagation paths as explanations | CauseLens (counterfactual paths); CausaLLM-Graph (BFS path + LLM report) |
| LLM evidence-chain reports with post-hoc claim verification | **CausaLLM-Graph** Alg. 3, `VerifyAndFilter(R_report, E_set)`: "Claims that cannot be traced to evidence are flagged or removed" |
| Deterministic evidence extraction decoupled from LLM reasoning | **EviRCA** (arXiv 2609.19825, 17 Sep 2026; abstract only reviewed): "evidence cards" plus read-only tools, evaluated on OpenRCA |
| Calibrated confidence for LLM RCA | PACE-LM (arXiv 2309.05833; only the search abstract was seen, the paper was not read) |
| Abstention / selective prediction for LLM RCA | `risk-adjusted-abstention` GitHub study (self-published, not peer-reviewed, ~Sep 2026): SLO- and cost-aware abstention on RCAEval RE1 and RCA100, with **simulated** confidence. **The pre-registered hypothesis was refuted.** |

### 3.2 What may still be a defensible contribution (to be confirmed by experiment)
1. **A typed, specified, auditable claim verifier.** CausaLLM-Graph describes verification in one sentence and does not report how many unsupported claims were produced or removed. Its explainability evaluation is an expert FAE score on 50 reports. EviRCA's abstract does not mention claim-level checks against evidence IDs. Our verifier would separately check ID existence, case and time-window membership, service and source association, numeric value and unit consistency, the predicate the claim asserts (for example "exceeds baseline"), and each propagation edge against observed topology. It would also report its own precision and recall against human labels.
2. **Feeding verification outcomes into abstention.** In our design, verified-claim coverage is one input to the abstain decision, alongside evidence sufficiency, cross-modal agreement and score margin. The study that tested abstention for RCA used simulated confidence and did not use verification signals. We did not find, in the six papers or in the searched work, any design that couples claim-level verification with abstention. Our search was not exhaustive.
3. **A matched-perturbation robustness protocol.** Every method sees the *same* degraded evidence view: random record loss, modality loss, targeted critical-span removal, removal of root-cause logs, jitter, and conflicting decoys. Results are reported as risk–coverage curves rather than top-k only. DiagFusion evaluated naturally missing metrics only (n=62). TraceRCA perturbed anomaly-detection labels and trace sampling, not the evidence itself.

### 3.3 Honest risks to the novelty claim
- The space is moving fast: EviRCA (Sep 2026), GALA/GALA+ (Aug 2026), SpecRCA (Jan 2026), and a trajectory-level study of LLM RCA agents (Aug 2026) all appeared in 2026. Before writing the paper we need a focused literature sweep on "verification + abstention + RCA", and the papers above must be read in full, not just their abstracts.
- The one prior test of RCA abstention was **refuted**. Our heuristic may also fail to beat a simple score-margin threshold. That comparison is therefore a required ablation (see the protocol, §7).
- **Circularity.** If the same verifier both filters EvidenceRCA's claims and measures "unsupported claims", EvidenceRCA wins RQ3 by construction. The mitigations in the protocol (§6.3) are: report rates before and after filtering, measure the verifier against blinded human labels, and report the claim-level yield (verified claims per diagnosis) so that "say nothing" is not rewarded.
- **Small synthetic testbed.** With five services, graph ranking may saturate (a ceiling effect), so RQ1 differences may be uninformative. External validation on RCAEval addresses part of this, for localization only.

Recommended framing for the paper: *an evidence-verification and abstention layer for RCA, plus a matched-perturbation evaluation protocol*. Do not frame it as a new RCA algorithm.

---

## 4. Proposed architecture

```
 ┌────────────── Docker Compose (Colima VM) ──────────────┐
 │  workload-gen ──HTTP──> gateway ──> order ──> payment    │
 │                                 │        └──> inventory  │
 │                                 └──────────> inventory   │
 │                      order, inventory ──SQL──> postgres  │
 │  (all services: OTel SDK → traces, logs w/ trace_id,     │
 │   metrics; admin/fault routes NOT instrumented)          │
 │                     │ OTLP                                │
 │              otel-collector (contrib)                     │
 │   receivers: otlp, docker_stats, fluentforward(postgres)  │
 │   exporter: file (JSONL, per-signal)                      │
 └─────────────────────┬────────────────────────────────────┘
                       │ per-case slice + sha256 manifest (read-only)
          results/raw/<opaque_case_id>/            ground_truth/<opaque_case_id>.json
                       │                              (separate dir; never read by rca/)
          telemetry/normalize → evidence store (immutable IDs)
                       │
          telemetry/views: deterministic perturbation(spec, seed) → evidence VIEW
                       │   (same view object fed to every method)
     ┌─────────────────┼───────────────────────────────────────────┐
     B1 anomaly rank   B2 MicroRCA-inspired   B3 TraceRCA-inspired   B4 LLM-only
                       │
              EvidenceRCA pipeline:
              detect → temporal order → observed-graph-constrained ranking
              → evidence retrieval → [LLM reasoning | template reasoning]
              → independent verifier → abstention gate → JSON diagnosis
                       │
          experiments/evaluate (joins ground_truth only here) → results/processed
```

### 4.1 Testbed (Phase 1)
- **Services**: `gateway`, `order`, `payment`, `inventory` (Python 3.12, FastAPI or Starlette, one shared base image), and `postgres` (alpine). Order and inventory own database connection pools (SQLAlchemy, small `pool_size`, so pool exhaustion can be injected realistically).
- **Real dependencies**: gateway→order, order→inventory (reserve), order→payment (charge), and gateway→inventory (stock lookup). Each service has an HTTP client timeout and a bounded retry, so cascades can occur.
- **Workload generator**: an open-loop seeded Poisson arrival process with a mix of request types. It tags no requests with scenario information.
- **Telemetry**: OTel Python SDK auto- and manual instrumentation for spans (`trace_id`, `span_id`, `parent_span_id`), structured JSON logs carrying `trace_id`/`span_id` via the OTel logging bridge, and app metrics (request duration histogram, error count, pool in-use/waiting gauges). Container metrics come from the collector's `docker_stats` receiver. Postgres stdout is collected via the Docker fluentd log driver (`fluentd-async=true`) into the collector's `fluentforward` receiver.
- **Phase 1 exit criterion**: an automated check that all three signal types for every service actually land in collector output, with spans correctly parented across services.

### 4.2 Faults (Phase 2)
| Fault | Mechanism | Ground-truth root |
|---|---|---|
| Service crash | `docker kill` on the target container | Target service |
| Injected processing latency | An admin-toggled in-process delay of N ms on one operation | Target service and operation |
| Network timeout | `tc netem delay` above the caller timeout on the target container (needs `NET_ADMIN` and `iproute2` in the image) | Target service |
| DB connection-pool exhaustion | An admin-toggled leak that holds the target service's pooled connections | Target service (operation: database pool) |
| CPU pressure (resource-capped) | A busy-loop or stress process inside a container already capped with `cpus:`. The cap protects the host | Target service |
| Cascading dependency failure | A Postgres-side lock or slowdown that triggers timeouts and retries in order/inventory, then errors at the gateway | `postgres` (primary). The other services carry propagated symptoms and are labelled as such |

Fault control routes are excluded from instrumentation and logging. An automated **leakage test** greps all agent-visible telemetry for fault names, case labels and injection markers. Ground truth is checked by an independent validator (for example `docker inspect` state, `tc qdisc show`, the admin state endpoint, pool gauges), and the validator's result is stored separately.

### 4.3 Evidence store (Phase 3)
- **Record fields**: `evidence_id`, `case_id` (opaque UUID), `view_id`, `modality` ∈ {trace, log, metric}, `source`, `service`, `operation`, `ts_start`/`ts_end` (UTC ns), `measurement`, `value`, `unit`, `trace_id`, `span_id`, `parent_span_id`, `provenance` (raw file, byte offset or line, raw sha256, transform chain).
- **Evidence IDs** are content-derived hashes, so they are deterministic, immutable and contain no label information.
- **Observation status** per (service, signal, window) is one of: `observed_normal`, `observed_anomalous`, `missing` (expected from the baseline cadence or a dangling parent span, but absent), or `unavailable` (the source was not configured or not collected for this view).
- **Dependency graph**: built only from observed cross-service parent→child span pairs and DB client spans, with counts and first/last-seen times. No edges come from configuration files.

### 4.4 Methods (Phases 3–4)
- **B1** deterministic anomaly ranking: robust z-scores (median/MAD) of each service's latency, error rate and resource usage, analysis window versus baseline window, ranked by maximum score.
- **B2** MicroRCA-inspired: an attributed graph of service nodes plus one host node (single Colima VM), BIRCH anomaly detection on per-edge response times, edge weights per MicroRCA Eq. 2, anomaly score per Eq. 3, and personalized PageRank (c=0.15, α=0.55 as published). The differences from the paper are documented.
- **B3** TraceRCA-inspired: per-pair invocation anomaly severity (`|v−μ|/σ`) on latency and status, adaptive feature selection (δ_fs=10 %), δ_ad=1, FP-growth (δ_spt=10 %), Jaccard index, top-k=100 sets, and in-set score. The differences from the paper are documented (no "last-period" day-long history).
- **B4** LLM-only: the identical evidence view serialized under a token budget, with the same output JSON schema. No verifier and no abstention gate are applied, but the verifier is still run on its output as a **measurement**.
- **EvidenceRCA**, in two configurations: `no-LLM` (template claims) and `LLM` (local qwen3:4b via Ollama; optional paid provider via the abstraction).

### 4.5 LLM provider abstraction
A `Provider` interface (`generate(messages, json_schema, seed, temperature)`) with three adapters:
- `none`: deterministic; template reasoning.
- `ollama`: local; `temperature=0`; fixed `seed`; JSON-schema `format`; thinking disabled or recorded.
- `anthropic` / `openai`: only if you provide keys.

Every call logs the model ID or digest, prompt hash, token counts, latency and cost estimate. Telemetry text is placed inside a delimited, escaped data block and marked as untrusted. The system prompt forbids following instructions found inside it. A prompt-injection probe (an adversarial log line) is part of the test suite.

---

## 5. Baseline feasibility

| Baseline | Feasibility | Reason / differences that must be disclosed |
|---|---|---|
| B1 simple anomaly rank | High | Our own design; no paper claimed |
| B2 MicroRCA-inspired | High | The algorithm is fully specified (Eqs. 1–4, Alg. 1). Differences: OTel span latencies instead of Istio/Prometheus; a single host node (one VM), so the host-correlation term degenerates; 5 services rather than Sock-shop's 13. Official code: **not verified** (check in Phase 3) |
| B3 TraceRCA-inspired | High | Fully specified. Official code and data at `github.com/NetManAIOps/TraceRCA` (cited in the paper; repository not yet inspected). RCAEval bundles a TraceRCA implementation (MIT) that can be used to cross-check ours. Difference: no last-period history |
| B4 LLM-only | High | Same view, same schema. Its quality is bounded by the 4B local model unless a paid provider is approved |
| DiagFusion (exact) | **Not recommended** | Supervised (fastText + GNN); its authors trained on 160 and 80 labelled cases. Our labelled set would be too small and would leak into evaluation. We will cite it and not reproduce it |
| CauseLens (exact) | Possible later, not planned | Code at `github.com/hanpanda/CauseLens` (not inspected). It needs a GAT autoencoder trained on long normal periods; training took 692–1098 s on a GPU server in their setup. Treated as an optional extension |
| MicroCause | Out of scope as a baseline | It targets *intra*-microservice metric-level root causes (64 metrics per service, 4 h windows). Its temporal-ordering idea informs EvidenceRCA's ranking (cited) |
| CausaLLM-Graph | **Not reproducible** | No code or data link; needs a Llama-3-8B fine-tuned on an unreleased 15k-example dataset and 4×A100. Comparison is conceptual only |

### External validation (RCAEval)
- **Access**: `github.com/phamquiluan/RCAEval`. Code and datasets are MIT per the README. Some bundled baselines have other licenses, and CausalRCA and RUN have none. Data is on Hugging Face (~3.4 GB Parquet, per-case download possible) and Zenodo (~5.2 GB zip, expanding to ~38 GB).
- **Datasets**: RE1 (375 cases, metrics only); RE2 (270 cases, metrics + logs, traces for Online Boutique and Train Ticket only); RE3 (90 code-level faults).
- **Applicability**: RE2-OB and RE2-TT have all three modalities, so they are the only RCAEval subsets that can exercise EvidenceRCA's cross-modal checks. RE1 can only test metric-only localization. RCAEval supplies an `inject_time` convention that differs from our alert-triggered window, so this must be disclosed.
- **Schema compatibility**: trace and log field names are not documented on the README; inspect one case before committing to integration.
- **Disk**: the full RE2 download does not fit in the current 8.4 GB free. Only per-case Hugging Face downloads are feasible.
- **Results**: synthetic and RCAEval results are reported in separate tables. No benchmark claim is made unless the run actually completed.

---

## 6. Phase plan and approval gates

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | This plan, the literature matrix, the protocol | **Now: awaiting approval** |
| 1 | `git init`; `pyproject.toml` (uv, py3.12, pinned); Compose testbed; collector; ingestion verification test | Your approval before any image pull or build (~0.8–1.2 GB of images) |
| 2 | Fault injectors, independent ground-truth validator, leakage test, opaque case IDs, 1 smoke case per fault type | — |
| 3 | Evidence store, views/perturbations, B1–B3, verifier, EvidenceRCA no-LLM, unit tests | — |
| 4 | Provider abstraction, Ollama adapter, B4, EvidenceRCA-LLM; a timed 5-call benchmark of qwen3:4b before committing to the full LLM matrix | Your approval for any paid API |
| 5 | Smoke run, then the full capture and analysis matrix | Your approval of the final matrix size (see the protocol, §9) |
| 6 | Raw CSV/JSON, figures, statistics, report | — |

---

## 7. Estimated runtime and cost (planning estimates, not measurements)

| Item | Estimate | Basis |
|---|---|---|
| Testbed images | ~0.8–1.2 GB disk | python:3.12-slim, postgres:alpine, otel collector-contrib, one app image (sizes approximate) |
| Telemetry capture | ~5 min wall-clock per case; ~144 cases → **~12 h** (2 overnight batches) | 90 s warm-up/baseline + 120 s injection + 60 s recovery + ~30 s reset |
| Raw telemetry | ~20–40 MB per case uncompressed; ~0.3–0.6 GB gzip in total | ~10 req/s × ~6 spans plus logs and metrics. To be measured in Phase 1 |
| Deterministic analysis | Minutes for the whole matrix | Graph/PPR and FP-growth on 5 services |
| LLM (local qwen3:4b) | **Unmeasured.** Assumed 20–60 s per call on M1. The core LLM matrix (~2.3k calls) would take **~13–39 h** | To be benchmarked in Phase 4 before committing |
| LLM (paid API, optional) | Token volume ≈ 2.3k calls × (~5k input + ~0.8k output) ≈ **11.5 M input + 1.8 M output tokens** (full core matrix); the proposed paid subset is ~400 calls ≈ 2.3 M input + 0.3 M output | Dollar cost = tokens × the provider's current list price. I will not quote prices from memory; they will be checked when you approve |
| Development effort | Phases 1–2: ~2–3 working sessions; Phase 3: ~3–4; Phases 4–6: ~3–4 | Rough guess |

---

## 8. Blockers and risks requiring your decision

1. **Disk (8.4 GB free, 96 % full).** About 2–3 GB is the minimum for the testbed plus our data. RCAEval RE2 does not fit. Options: (a) you free space manually, or (b) I run `docker image prune` / `docker builder prune` (~4 GB reclaimable) **after you confirm** that the 40 existing images are disposable.
2. **RAM (8 GB).** Capture (Docker) and LLM analysis (Ollama) will run in separate stages, never at the same time. The architecture already decouples them, because RCA runs on stored evidence.
3. **No paid LLM credentials.** The default LLM is local qwen3:4b. A stronger model for a subset needs a key from you. `~/.aws/credentials` exists, but I have not checked whether it has Bedrock access, and I will not without your instruction.
4. **Not a git repo.** I propose `git init` at the start of Phase 1 (no remote, no push).
5. **The `Literature/` folder is capitalised.** On this case-insensitive filesystem `literature/` resolves to it, so I will not rename it. The empty `Project3_Setup/` folder will be left untouched.
6. **Statistical power.** About 48 held-out single-fault test cases give 95 % CIs of roughly ±13–14 pp on accuracy. Differences under ~15 pp are unlikely to be significant. Increasing seeds raises capture time linearly (see the protocol, §9).
7. **Novelty risk** (§3.3). A literature sweep is recommended before paper drafting. EviRCA, PACE-LM, GALA and the abstention study should be read in full.
