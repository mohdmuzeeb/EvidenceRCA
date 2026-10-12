# Literature Matrix (Phase 0)

Date: 2026-10-11. All six PDFs in `Literature/` were present and were read in full. Text was extracted with `pypdf` into a scratch directory, and nothing was installed into the project. Numbers below are **as reported by the authors**; none have been reproduced by us.

Additional related work found by web search on 2026-10-11 is listed in §3. For those items only the abstract or README was seen, unless stated otherwise. They must be read in full before any paper claims rely on them.

---

## 1. Core six papers: comparison matrix

| | MicroRCA | MicroCause | TraceRCA | DiagFusion | CauseLens | CausaLLM-Graph |
|---|---|---|---|---|---|---|
| **Venue** | NOMS 2020 | IWQoS 2020 | IWQoS 2021 | IEEE TSC 16(6) 2023 | IWQoS 2025 | COMPSAC 2026 |
| **Target granularity** | Service | Metric *within* one microservice | Service (sets → ranking) | Instance + failure type | Operation / entity (container, host) | Service → container → process |
| **Modalities** | Metrics only (app RT via Istio + container/host resource) | Metrics/KPIs (64 metrics + 4 KPIs) | Traces (+ per-service resource metrics as invocation features) | Traces, logs, metrics + deployment data | Metrics + traces (logs deliberately excluded) | Logs, metrics, traces |
| **Graph source** | Service-call edges from metrics + service→host edges; built from a pre-anomaly window | Learned causal graph (PCTS: lagged PCMCI-style PC) | None (set mining over traces) | Trace call graph (bidirectional edges) + co-deployment edges | Span parent–child → operation DAG; entity→operation affiliation edges | Static call graph from traces + PC init + learned STGNN dynamic graph with NOTEARS DAG penalty |
| **Inference** | BIRCH anomaly detection; weighted anomalous subgraph; personalized PageRank (c=0.15, α=0.55) | SPOT anomaly detection; temporal cause-oriented random walk (partial correlation); priority levels + anomaly time ordering | 3-sigma-style invocation severity with adaptive feature selection; FP-growth; Jaccard index; in-set in/out score | Event extraction (3-sigma, Drain) → fastText embeddings → TAGCN GNN, joint loss | GAT-autoencoder SCM trained on normal data; reconstruction error + counterfactual causal effect; α-weighted combination | LLM semantic encoding → STGNN graph → GBR residual + KS test per candidate → LLM report |
| **Supervision** | Unsupervised | Unsupervised | Unsupervised | **Supervised** (160 / 80 training cases) | Self-supervised on normal data | Supervised fine-tuning (15k examples) + learned graph |
| **Missing-data handling** | Not addressed | Not addressed | Robust to trace sampling (1/16 → ~80–90 % of best); anomaly-label noise study | **Yes**: 62 D1 cases lacking metrics; authors claim it works with any 2 of 3 modalities | Interpolation to pad missing data | Not evaluated |
| **Explanation output** | Ranked list | Ranked metrics | Ranked list + abnormal-trace intersection display | Ranked instances + failure type | Ranked candidates + **propagation path** + edge causal-effect scores | **Evidence-chain report** + propagation path |
| **Claim verification** | — | — | — | — | — | Yes, at a high level: `VerifyAndFilter`, where claims not traceable to `E_set` are "flagged or removed". No verifier metrics reported |
| **Abstention / selective prediction** | — | — | — | — | — | — |
| **Evaluation data** | Sock-shop on GKE; latency / CPU hog / memory leak; 95 cases | 86 production tickets (Alibaba-scale e-commerce) | Train-Ticket (200 faults incl. 11 multi-root) + production system B (22 faults) | GAIA (D1) + bank (D2, private) | AIOps Challenge 2022 (OB, 203 faults) + Train-Ticket (78) | Train-Ticket on 10-node cluster; 847 injections (156 concurrent) |
| **Metrics** | PR@k, MAP | AC@k, Avg@5 | A@k, MAR, MFR | A@k, Avg@5; weighted P/R/F1 | HR@k, Avg@k; overhead | F1, Acc@k, MAR, FAE-Score (expert), time, memory; "All Roots Found" |
| **Headline results (reported)** | PR@1 0.89, MAP 0.97 | AC@1 46.7 %, AC@5 98.7 % | A@1 0.82 (A), 0.88 (B) | Avg@5 0.75 / 0.76; F1 0.84 / 0.80 | HR@1 0.847 (A), 0.782 (B) | F1 0.94, Acc@1 0.92; dual-fault All-Roots 0.76 |
| **Code / data** | Not stated in paper | Not stated | Datasets + code: `github.com/NetManAIOps/TraceRCA` (cited in paper) | `anonymous.4open.science/r/DiagFusion-378D`; GAIA public | `github.com/hanpanda/CauseLens` | None stated |
| **Use in this project** | Basis for **B2** (MicroRCA-inspired) | Concept only (temporal ordering); not a baseline | Basis for **B3** (TraceRCA-inspired) | Cited for multimodality + missing-modality; not reproduced (supervised) | Cited for operation-level graph + paths; optional extension | Closest prior work for evidence reports + verification; conceptual comparison only |

---

## 2. Per-paper notes relevant to design decisions

### MicroRCA (Wu et al., NOMS 2020)
- The attributed graph has service and host nodes. An edge si→sj is added if si calls sj, and si→hj if si is hosted on hj. The graph is built from a window **before** the anomaly, on the assumption that it is a reliable period.
- Anomalous edges are those where the per-pair response time is flagged by BIRCH. Their origins become anomalous nodes, and the subgraph adds neighbours of those nodes.
- Weights (Eq. 2): anomalous edges get α. Normal service edges get the Pearson correlation with the node's average anomalous response time rt_a. Host edges get max-corr(rt_a, host resource) × the mean in-edge weight.
- Anomaly score (Eq. 3): AS = mean weight × max corr(rt_a, container resource). PPR uses preference vector = AS, teleport c=0.15 and reversed edges. Host nodes are removed from the final ranking.
- **Differences in our adaptation:** OTel span-derived pairwise latencies instead of Istio; the single Colima VM gives exactly one host node; container metrics come from docker_stats; there are 5 services. The paper's α=0.55 and BIRCH threshold 0.045 are fixed *a priori*, with no tuning on our test data.

### MicroCause (Meng et al., IWQoS 2020)
- The problem is **intra**-microservice: rank metrics (not services) given a failure ticket {service, KPI, time}.
- PCTS is a lagged PC variant (improved PC of Runge et al.) collapsed to an indicator graph. TCORW uses partial-correlation transition weights, a score combining visit frequency and anomaly degree, three metric priority levels, and a final ordering by anomaly onset time.
- **Takeaway for EvidenceRCA:** the **temporal ordering** of anomaly onsets is useful evidence. However, onset ordering is fragile under timestamp jitter, which is part of what RQ2 tests.

### TraceRCA (Li et al., IWQoS 2021)
- Insight: a service that more abnormal traces and fewer normal traces pass through is more suspicious (similar to spectrum-based fault localization).
- Invocation anomaly severity is α=|v−μ|/σ per microservice pair, using last-slot plus last-period history. A feature is useful if α_after − α_before > δ_fs·α_before (δ_fs=10 %). An invocation is abnormal if α > δ_ad=1 on any useful feature, and a trace is abnormal if any of its invocations is.
- Set mining uses FP-growth with support δ_spt=10 %, ranked by Jaccard index, top k=100 sets. The in-set score is |#traces with incoming abnormal − #traces with outgoing abnormal|. The final score is max over sets of (JI × in-set score).
- Its authors state limitations: it cannot detect structural anomalies, and it struggles when anomalies propagate both up- and downstream.
- **Differences in our adaptation:** we have no day-long "last-period" history (only an in-case baseline window); our features are span latency and status (plus optional container metrics).

### DiagFusion (Zhang et al., IEEE TSC 2023)
- Events are extracted per modality (trace 3-sigma per caller-callee and status-code count surges; Drain log templates; metric 3-sigma with direction), serialized per instance, embedded with fastText (with augmentation), and fed to a GNN over the trace + deployment graph. Two tasks: root-cause instance and failure type.
- Missing modality: on 62 D1 cases without metrics, its authors report graceful degradation versus PDiagnose. They claim the method works if any two of the three modalities are present.
- **Takeaway:** missing-modality robustness is **not novel**. Our contribution must be about verification and abstention under loss, not robustness per se.

### CauseLens (Liu et al., IWQoS 2025)
- The operation-level DAG comes from span parent–child links. Entity nodes link to operations. Node features are KPIs and system metrics; edge features are invocation counts. The design assumes no circular operation dependencies.
- An SCM is trained on normal data (GAT encoder + MLP decoder). RCA combines the normalized reconstruction error with a counterfactual cumulative causal effect (intervene upstream to normal, re-infer downstream), and outputs (node, score, path).
- Logs are excluded: its authors argue log keywords and template counts added little or hurt (Table VI).
- **Takeaway:** building graphs from observed spans and outputting propagation paths are **not novel**. Our edge-validation check (every claimed propagation edge must exist in observed topology) is a verification step, not a graph-learning contribution.

### CausaLLM-Graph (Meng, COMPSAC 2026)
- The closest prior work. An LLM textualizes and encodes the modalities. An STGNN learns a dynamic DAG. A residual KS-test per candidate produces scores. The LLM writes a report from an evidence set extracted by BFS from the root, with a prompt that says "Every claim must reference specific evidence". A post-hoc `VerifyAndFilter` removes untraceable claims.
- Not specified in the paper: what counts as a "claim", how claims are matched to evidence, the verifier's error rates, how many claims were removed, and whether removal changed the diagnosis. Explainability is evaluated with an expert FAE-Score on 50 reports.
- Things to be cautious about when citing:
  - No code, data or model is released.
  - The paper reports results that rely on a fine-tuned 8B model and a 4×A100 environment.
  - There are reference-list inconsistencies. For example, the text attributes DiagFusion to [18] and NOTEARS and RCD to [9], but reference [18] is "Graph of Thoughts" and [9] is Pham et al. (ASE 2024).
  - The text contains "Responding to reviewer concerns…" in §III-E.
- None of this changes the conceptual overlap. We must position EvidenceRCA as a *specified, audited* verifier plus abstention, not as "the first evidence-grounded LLM RCA".

---

## 3. Additional related work found by web search (2026-10-11): not yet fully read

| Work | Status | What it does | Relevance |
|---|---|---|---|
| **EviRCA**: Wang et al., arXiv 2609.19825 (17 Sep 2026) | Abstract and metadata read | Deterministic extraction into "faithful multimodal evidence cards"; the LLM reasons only over the cards via read-only tools; evaluated on OpenRCA (40.6–43.9 % correct vs ≤15.2 % prior; 15–26× fewer tokens) | **High overlap** with "deterministic evidence + LLM reasoning". The abstract does not mention claim-level verification, abstention, or missing telemetry. Must read in full |
| **risk-adjusted-abstention**: GitHub (KarthikeyaMogulluri), ~Sep 2026, MIT | README read | Pre-registered study of SLO- and cost-aware abstention for LLM RCA on RCAEval RE1 (OB+SS) and RCA100. Confidence was **simulated**, not from a live LLM. **Hypothesis refuted**: the derived threshold was ~40–45 % worse than a hindsight-best fixed threshold | Direct prior evidence that abstention gains are not guaranteed. Not peer reviewed. Motivates our "simple margin threshold" ablation |
| **PACE-LM**: arXiv 2309.05833 | Search snippet only | Calibrated confidence estimation for GPT-4 cloud incident RCA | Related to RQ4; must read before claiming anything about calibration |
| **GALA / GALA+**: arXiv 2608.08968 | Search snippet only | Graph-augmented LLM agents for microservice RCA | Related LLM+graph agent; read before submission |
| **Beyond Fault Localization** (trajectory-level study of LLM RCA agents): arXiv 2608.21310 | Search snippet only | Finds that answer correctness and diagnostic-process quality can diverge | Supports measuring claim support separately from top-1 accuracy |
| **SpecRCA** (hypothesize-then-verify): arXiv 2601.02736 | Search snippet only | Hypothesize-then-verify RCA | "Verify" here refers to hypothesis testing, not claim–evidence checking. Read to confirm |
| **RCAEval**: Pham et al., arXiv 2412.17015; `github.com/phamquiluan/RCAEval` | README read | 735 cases (RE1/RE2/RE3) over OB, Sock Shop, Train Ticket; 15+ baselines; MIT (code and data, per README) | External validation candidate (see the research plan, §5) |
| **OpenRCA** | Not checked | The benchmark used by EviRCA | Possible alternative external benchmark; licence and access not yet checked |

---

## 4. Gaps this project targets (to be confirmed experimentally)
1. A **specified, measured** claim-level verifier: verifier precision and recall against human labels, plus unsupported-claim rates before and after filtering. None of the six papers reports these.
2. **Abstention driven by verification and evidence sufficiency**, evaluated by risk–coverage on held-out cases with thresholds set on a separate calibration split.
3. **Matched, structured telemetry degradation** applied identically to every method (critical-span removal, root-cause-log removal, decoy conflicts, jitter), beyond naturally missing modalities.
