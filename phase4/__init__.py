"""Phase 4 (post-study): local-LLM conditions B4 and E1.

This package is separate from the frozen deterministic study (freeze commit 912085c). It imports the frozen
modules read-only and never modifies them; nothing here feeds back into E0, B1-B3, the verifier or tau.

  phase4/agent/  agent side: evidence pack, prompts, output schema, bounded LLM client, B4/E1 (no ground truth)
  phase4/eval/   evaluation side: development benchmark and structured-output tests (may read ground truth
                 after a call, to score it; never before or during)
"""
