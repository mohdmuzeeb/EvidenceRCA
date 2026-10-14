# Phase 4A Validation: local LLM setup, B4/E1 integration, five-call benchmark

Run on 2026-10-14, on branch `phase4a`. Scope approved by you: Phase 4A only. Phase 4B (the full LLM evaluation) has **not** started.

## 1. What was preserved

- The frozen study is untouched:
  - freeze commit `912085c`, re-verified by `evaluation.freeze verify` before the benchmark and in `tests/test_phase4.py`;
  - calibration commit `fb6a9fd`;
  - `docs/final_results.md`, all `results/final/` files, raw data, hashes and protocol deviations.
- No frozen file was edited. The Phase 4 code is a new package, `phase4/`, that imports frozen modules read-only.
- **Splits:** only dev seed 11 cases were opened. No calibration or test case was read in Phase 4A.
- **Untouched work:** no post-study fixes, no external benchmark, no Phase 4B matrix.

## 2. Environment and model

| Item | Value |
|---|---|
| Machine | MacBook Air M1 (MacBookAir10,1), 8 GB RAM, macOS 26.5.2, AC power during the benchmark |
| Docker / Colima | stopped. The Colima VM was not running; only its idle `limactl usernet` helper processes remained, and they were left alone |
| Ollama | 0.34.2, `ollama serve` bound to 127.0.0.1:11434, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`. Stopped after the benchmark |
| Model | `qwen3:4b`, Q4_K_M, 4.0 B parameters, digest `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`, Apache-2.0 |
| Download | 2.38 GiB on disk (`~/.ollama/models`), pulled in 38 s. This was the only download |
| Free disk | 26 GB before the pull, about 24 GB after |
| Decoding | temperature 0, seed 20261014, thinking off, `num_ctx` 6144, `num_predict` 1024, JSON-schema `format`, timeout 300 s, ≤ 1 retry (transport failures only), keep-alive 15 min, unloaded at the end |
| Prompt | version `p4a-v1`, system prompt sha256 `86edf896…` |
| Cost | USD 0. Local inference only; no paid API or cloud service |

## 3. What was built (`phase4/`)

| File | Side | Purpose |
|---|---|---|
| `agent/evidence_pack.py` | agent | Method-neutral, bounded evidence pack: per-service digest, call graph, ≤ 36-record table. E1's table leads with E0's retrieved records and has the same size |
| `agent/prompts.py` | agent | Versioned, case-invariant instructions; aliases E01… for evidence ids; B4 and E1 task lines |
| `agent/output_schema.py` | agent | JSON schema; independent validator (`malformed_json` / `schema_noncompliant` / `valid`); semantic issues; conversion to the frozen Diagnosis JSON with no editing of claims |
| `agent/llm_client.py` | agent | Bounded Ollama client. Records model and digest, options, prompt hashes, attempts, tokens, durations, wall time and raw text. Outcomes: `ok` / `timeout` / `model_failure` |
| `agent/conditions.py` | agent | B4 and E1. Frozen verifier on every generated claim; pre- and post-filter claim sets; frozen S computed descriptively |
| `eval/structured_tests.py` | eval | 17 mock-server scenarios + prompt leakage scan → `results/phase4a/structured_output_tests.json` |
| `eval/benchmark.py` | eval | The timed five-call benchmark → `results/phase4a/five_call_benchmark.json`, raw calls in `results/phase4a/raw/` |
| `tests/test_phase4.py` | test | Freeze intact; static check that `phase4/agent` never imports harness/evaluation or names ground truth; validator unit tests |

Definitions of B4 and E1 are in `docs/llm_evaluation_protocol.md` §2. In short:
- **B4:** the LLM produces the whole diagnosis.
- **E1:** E0 ranks candidates and retrieves evidence; the LLM writes the claims, path and explanation. E1's diagnosis-level outcome is E0's. The LLM's own pick is recorded but never overrides it.

## 4. Integration validation (no model calls)

All 17 scenarios passed against a mock Ollama server (`structured_output_tests.json`):

| Scenario | Result |
|---|---|
| Valid answer: B4 and E1 | Parsed; all claims kept; aliases mapped to view ids; handed to the frozen verifier. As a sanity check, E1 re-expressing E0's own claims gives 4/5 verified, the same as E0's frozen dev result for that case |
| Output truncated at the token limit | `malformed_json`, `truncated_at_token_limit` = true, no retry, ABSTAIN with no claims |
| Schema violations (5 kinds: missing key, bad enum, too many claims, unexpected key, string value) | `schema_noncompliant`, ABSTAIN |
| Invented citation (`E99`) | Kept verbatim; V1 fails and the claim is rejected |
| Wrong value | V4 fails. The value is preserved unchanged and the claim is absent from the post-filter set |
| Two timeouts | Retried once, then outcome `timeout` |
| HTTP 500 then 200 | Recovered on the retry |
| HTTP 404 | `model_failure`, no retry |
| DIAGNOSE with "none" | Semantic issue reported; ABSTAIN |
| E1 model disagrees with E0 | E0's outcome kept; disagreement recorded |
| No alert | No request sent |
| Request settings | Exactly as in the table above |

**Prompt leakage scan.** It covered 84 prompts: 21 dev primary cases × {C0, MT} × {B4, E1}.
- 0 label-token hits;
- no scenario key, injection time or case id in the case text;
- exactly one distinct system prompt across all cases;
- equal record counts for B4 and E1.

**Test suite:** 225 passed (see §7).

## 5. Five-call benchmark (dev seed 11)

**Call selection.** The calls were fixed in code before any call was made, using Phase 3 dev outcomes only. Ground truth was read only after each call returned, to score it.

| # | Condition | Case (dev) | Purpose | Wall s | Prompt / output tokens | Parse | Observed claims verified | Diagnosis outcome |
|---|---|---|---|---|---|---|---|---|
| 1 | E1 | processing_latency:inventory, C0 | successful | 82.8 (cold; load 13.2) | 3847 / 674 | valid | 3/6 | correct (E0); LLM agrees, fault type right |
| 2 | B4 | same view | successful, LLM-only | 53.4 | 3536 / 690 | valid | 3/6 | **correct**, fault type right |
| 3 | E1 | cascading_failure:postgres, C0 | difficult | 63.6 | 3954 / 661 | valid | **0/6**, flagged "DIAGNOSE without a verified observed claim" | wrong (E0's known failure); LLM agrees with E0 |
| 4 | B4 | service_crash:inventory, MT (no traces) | missing telemetry | 50.3 | 2674 / 623 | valid | 4/6 | **correct** service; fault type wrong (cascading_failure) |
| 5 | B4 | healthy control, C0, label-free window at the median record time | insufficient evidence; the correct answer is ABSTAIN | 59.3 | 3553 / 632 | valid | 1/6 | **wrong: DIAGNOSE inventory** (network_timeout) on a healthy system |

### Summary

| Measure | Result |
|---|---|
| Call outcomes | 5/5 ok, 0 timeouts, 0 model failures, 0 retries |
| JSON validity / schema compliance | 5/5 (100 %) / 5/5 (100 %). Grammar-constrained decoding |
| Truncation | None; the largest output was 690 of 1024 tokens |
| Context | Largest prompt was 3954 tokens; no context-overflow risk at 6144 |
| Citation validity | 48/48 cited aliases exist in the prompt and in the view. No invented ids |
| Claim verification | 11/30 observed claims verified (37 %), before filtering. Post-filter, only these 11 remain |
| Propagation edges | 2/9 verified |
| Load time | Cold load 13.2 s; warm load ≤ 0.66 s |
| Wall time | Warm-call mean 56.6 s per call; whole benchmark 349 s |
| Throughput | Generation 15.3–16.8 tokens/s; prompt evaluation 162–381 tokens/s (call 2 reused the cached system-prompt prefix) |
| Memory | Ollama resident peak 3.76 GB (model + KV at 6144 ≈ 3.49 GB reported by Ollama). Free memory 13–16 % during the calls, swap use 6.8 → 11.9 GB |
| Disk | Model 2.38 GiB. During the run, swap growth temporarily used about 3 GB more disk; free disk was 18 GB after the run |
| Abstention (descriptive, not thresholded) | E0 S: call 1 = 0.70, call 3 = 0.75. E1 S (frozen formula with E1 coverage): 0.625 and 0.50. B4 has no scores, so no S or margin |

### Failure analysis (from the 19 rejected observed claims and 7 rejected edges)

- **Unit misuse (V0, 4 claims).** The model used unit "1" for `call_failed` and `log_error_pattern` instead of `{call}` / `{record}`. The schema lets any unit pair with any predicate, so this is not caught at decoding time.
- **Subject/object confusion (V3, 11 claims).** The model wrote a latency claim with the caller as subject (for example, gateway) while citing the caller's CLIENT spans to inventory. These records belong to the callee, so V3 rejects the claim. It also filled `object_service` on non-onset claims.
- **Value errors (V4).** Some values were miscopied from the summary or table. For example, an error ratio of 0.832 was claimed where the recomputed value is 0.232.
- **Not above baseline (V5).** Some claims named deviations that the verifier's own baseline does not support.
- **Propagation direction (V6).** The model mostly listed edges caller→callee (gateway→order), the reverse of the requested root→affected-caller direction.
- **No abstention by B4.**
  - B4 DIAGNOSEd all 3 of its cases, including the healthy control.
  - Across all 5 calls, including the E1 calls, the model's own suspect was `inventory`. That was right in calls 1, 2 and 4, and wrong in calls 3 (postgres) and 5 (no fault).
  - n = 5 cannot separate a prior or position bias from chance, but it is a risk for Phase 4B.
  - On the control, the frozen verifier rejected 5/6 claims. It did **not** raise its DIAGNOSE flag, because one latency claim about inventory verified.
- **The verifier did its job on the difficult case.** E1 on the cascade got 0/6 verified and the "DIAGNOSE without verified claim" flag. The frozen E0 abstention gate would not have caught it (E0 S = 0.75).

**Interpretation.** These are observations from 5 dev calls only. They show that the integration and measurement work, and they show where qwen3:4b fails. They are not estimates of B4/E1 accuracy.

**No changes after the benchmark.** The prompt, schema and pack were not changed. The causes above are candidates for a prompt revision `p4a-v2`, to be developed on dev only and only with your approval:
- predicate-specific unit enums;
- an explicit subject rule for call records;
- edge direction stated with an example;
- an explicit abstention criterion.

## 6. Feasibility and estimates for Phase 4B

**Feasibility.** qwen3:4b runs on this machine with Docker stopped, but memory is the binding constraint.
- During the calls, the system ran at 13–16 % free memory with heavy swapping, because other apps (browser, VS Code) were open.
- Recommendation: close other apps for Phase 4B, keep `num_ctx` at 6144, and keep one model loaded.

**Time.** The proposed core matrix (protocol §8) is test seeds × {C0, R40, MT, ML, CF} × {B4, E1}:
- **Calls:** ≤ 470.
- **LLM time:** 470 × 57 s ≈ **7.4 h**.
- **Per-trial overhead:** view build, E0 and verification add about 0.1–2 s per trial, plus about 10 s per case to normalize.
- **Total:** about **8 h**, run unattended on AC with `caffeinate`.
- **Optional:** adding the dev and calibration seeds (about 80 more base cases) would add about 11 h.

**Memory:**
- Ollama about 3.8 GB peak;
- the evaluation process about 1 GB.

**Disk:** about 20 KB of raw output per call, so about 10 MB in total. The model takes 2.4 GB. Swap can temporarily use several GB of disk.

**Cost:** USD 0.

## 7. Reproduce

```bash
uv sync --frozen
# offline integration tests (no model needed)
uv run python -m phase4.eval.structured_tests
uv run pytest -q tests/test_phase4.py
# benchmark: Docker/Colima stopped, AC power
ollama pull qwen3:4b                                   # digest 359d7dd4bcda…
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &
caffeinate -dims uv run python -m phase4.eval.benchmark
pkill -f "ollama serve"
```

- **Decoding:** temperature 0 with a fixed seed. Repeat runs should give identical text, but Ollama/llama.cpp does not guarantee bit-exact repeatability across versions or hardware; compare `request_sha256` and the raw text.
- **Code version:** the benchmark ran on the working tree that the Phase 4A commit contains. No file under `phase4/agent/` or `phase4/eval/` changed after the run. `git_head` in the JSON is the parent commit `7b54850`, because the run preceded the commit.

**Full test suite:** 225 passed in 522 s, including 11 new Phase 4A tests. Leakage tests are parametrized over every captured case, so the count is higher than the 118 recorded before capture. Log: `results/phase4a/run_logs/pytest.log`.
