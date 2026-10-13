# Baseline Implementation (Phase 3)

Code is in `evidencerca/methods/baselines.py`, and the tests are in `tests/test_baselines.py`. All baselines receive the **same View and the same analysis/baseline windows** as EvidenceRCA, and emit the protocol's Diagnosis JSON. They produce ranked candidates only, with no claims.

**Status: B2 and B3 are "inspired by" their papers. They have not been validated against the authors' code and must not be described as reproductions.**
- MicroRCA's paper states no code.
- TraceRCA's repository (`github.com/NetManAIOps/TraceRCA`) was **not** inspected; the RCAEval implementation is unchecked.
- The papers' method sections were read directly from `Literature/` on 2026-10-12. Every "as published" item below is quoted from those PDFs.

## Shared inputs
- **Windows:**
  - analysis window: [alert − 60 s, alert + 60 s];
  - baseline window: the 90 s before it, clipped to skip the first 30 s of capture;
  - for the sensitivity analysis, both are anchored at the injection time instead ("oracle").
- **Response time / latency:** `span_duration` of CLIENT spans for a caller→callee pair. DB statement spans have callee `postgres`. DB `connect` spans (pool acquisition) are local to the caller, not edges.
- **Resource metrics:** docker_stats `container_cpu` (%) and `container_memory` (bytes), per service.

## B1: robust statistical anomaly ranking (our own design; no paper claimed)
- Features per service:
  - median latency of its SERVER spans (for postgres, of DB statement spans);
  - error fraction of the same spans;
  - container CPU;
  - container memory.

  Each is computed per 5 s bucket.
- For each feature: z = (median of analysis buckets − median of baseline buckets) / max(1.4826·MAD(baseline), floor). The floors are fixed a priori: 1 ms, 0.01, 1 %, 1 MB.
- Score = max over features of max(0, z). Only increases count.
- A feature is skipped when its modality is unavailable, it has fewer than 3 baseline buckets, or it has no analysis data.

## B2: MicroRCA-inspired (Wu et al., NOMS 2020)

| Element | Paper | Our implementation | Status |
|---|---|---|---|
| Graph nodes and edges | Service nodes plus host nodes; edge si→sj if si sends requests to sj; si→hj if si runs on hj | Services from observed CLIENT spans (plus parent/child pairs); one host node (single Colima VM) with an edge from every service | **Same structure**; host degenerates to one node |
| Graph time frame | "metrics of a certain time frame before the anomaly was detected" | Edges first seen before the analysis window starts | Same principle |
| Anomaly detection | BIRCH on edge response times ("slow response time"); threshold 0.045, chosen by the authors' sensitivity study | `sklearn.cluster.Birch(threshold=0.045, n_clusters=None)` on per-5 s mean response times **in seconds**, baseline plus analysis buckets. An edge is anomalous if ≥ 2 analysis buckets fall in clusters with no baseline bucket. | **Approximation.** The paper does not specify the input scaling, window or cluster-to-anomaly rule. |
| Anomalous nodes | Paper example: anomalous edges (s1,s2), (s2,s3), (s2,s4) give anomalous nodes s2, s3, s4, i.e. the callees (the text calls them "origins") | Callees of anomalous edges | Follows the worked example |
| rt_a | Average anomalous response time of the node | Mean of the node's anomalous in-edge series | As published |
| Edge weights (Eq. 1–2, Alg. 1) | Anomalous edge α. In-edge (i,j): corr(rt_a(j), rt(i,j)). Out-edge to a service: corr(rt_a(j), rt(j,k)). Out-edge to a host: avg(w_in(j)) × max corr(rt_a(j), U_h). | Same. Pearson |r|. Host U_h = summed container CPU and memory, since no host metrics exist. When an edge would be assigned twice, the first assignment is kept, except that anomalous edges always get α. | As published, with host and order caveats |
| α | 0.55 (best in the paper's study) | 0.55, fixed a priori | As published |
| Anomaly score (Eq. 3) | AS(sj) = w(sj) · max corr(U_c(sj), rt_a(sj)) | Same. w(sj) = mean weight of its weighted edges; U_c = container CPU and memory | As published |
| Localization (Eq. 4) | v = (1−c)·P·v + c·u; P_ij = w_ij / Σ_j w_ij; edges reversed before running; c = 0.15; u = anomaly scores; host nodes removed from the list | Exactly this, on reversed edges (callee→caller, host→service), u normalized to sum 1, power iteration to 1e−12 | As published (converges on cycles and disconnected graphs, since (1−c) < 1; tested) |
| Inputs | Istio response times, Prometheus container/host metrics, 13-service Sock-shop | OTel span latencies, docker_stats, 5 services | **Different data source** |

Expected weaknesses, which are inherent and not tuned away:
- B2 detects *slow* responses only. A crash makes calls fail fast, so it often finds no anomalous edge and abstains.
- B2 requires traces. Under MT it abstains.

## B3: TraceRCA-inspired (Li et al., IWQoS 2021)

| Element | Paper | Our implementation | Status |
|---|---|---|---|
| Invocation and features | Invocations between microservice pairs; features include latency and status (plus optional resource metrics) | CLIENT spans (caller→callee, including DB statement spans). Features: latency (ms) and status (error = 1) | Resource-metric features **not used** |
| Normal model | Per pair: mean and standard deviation from "last slot" plus "same slot of the last period"; updated per minute | Per pair: mean and SD from the **baseline window only** (no last-period history exists). σ floor of 1 ms / 0.05 for constant histories | **Approximation**: no last-period data; the floor is ours |
| Severity | α = \|v − μ\| / σ | Same; an unseen pair gives ∞ | As published |
| Feature selection | Useful if α_after − α_before > δ_fs·α_before (mean severities), δ_fs = 10 % | Same, per pair | As published |
| Abnormal invocation and trace | Abnormal if α > δ_ad on any useful feature, δ_ad = 1; a trace is abnormal if any invocation is | Same | As published |
| Frequent sets | FP-growth over the microservice sets of abnormal traces (the services a trace passes through), support δ_spt = 10 % | **Exhaustive enumeration** of all subsets (≤ 5 services). This yields the identical frequent itemsets; FP-growth only changes efficiency. | Equivalent result |
| Set ranking | Jaccard index between traces containing the set and abnormal traces; top k = 100 | Same | As published |
| In-set score | \|#traces with incoming abnormal invocations on m − #traces with outgoing abnormal invocations on m\| among traces containing the set | Same | As published |
| Final score | max over sets containing m of JI × in-set score | Same | As published |

Expected weaknesses:
- B3 needs traces.
- The in-set score is 0 for a service with equal incoming and outgoing abnormal invocations.
- Traces through a crashed service are truncated, since the callee emits no server span.

## Tests (`tests/test_baselines.py`)
- **Eq. 4 PPV:**
  - it converges on a cyclic graph;
  - an isolated disconnected component gets 0;
  - the score flows from callers to the callee;
  - empty input and zero preference are handled.
- **Behaviour:**
  - every method abstains on an empty view, with and without forced windows;
  - every method runs with each missing-modality combination, and B2/B3 abstain with a reason when traces are missing;
  - methods terminate on a cyclic service graph and on a disconnected graph (no CLIENT spans);
  - output is deterministic.

## Dev-driven changes (documented because they were made after looking at dev data)
1. **B2/B3 were re-aligned to the papers' text** (graph window, host weight factor, Eq. 4 operator, TraceRCA's relative feature selection, trace-level sets, in-set score). This was a correctness fix after re-reading the PDFs, not performance tuning; no parameter was changed.
2. **DB `connect` spans** are no longer treated as calls to postgres (they include pool wait). This affects every method equally, since it is part of evidence normalization.

No baseline hyperparameter was tuned on any data. All values are the papers' defaults or the fixed floors stated above.
