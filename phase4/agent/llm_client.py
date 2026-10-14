"""Bounded local-LLM interface (Ollama on 127.0.0.1 only; no cloud provider, no API keys).

Every call records: model name and digest, Ollama version, all decoding options (temperature, seed, context,
token limit, thinking off), prompt version and hashes, timeout, each attempt with its outcome, Ollama's token
counts and durations (load, prompt evaluation, generation), wall time, and the raw response text.

Retries are bounded (`max_retries`) and only for transport-level failures (timeout, connection error,
HTTP 5xx). A malformed or non-compliant answer is a result, not something to retry until it looks good.
Call outcomes: ok | timeout | model_failure; parsing (malformed_json / schema_noncompliant / valid) is
done by phase4.agent.output_schema.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field

import httpx

HOST = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class LLMSettings:
    model: str = "qwen3:4b"
    temperature: float = 0.0
    seed: int = 20261014
    num_ctx: int = 6144
    num_predict: int = 1024
    think: bool = False
    timeout_s: float = 300.0
    max_retries: int = 1
    keep_alive: str = "15m"

    def options(self) -> dict:
        return {"temperature": self.temperature, "seed": self.seed, "num_ctx": self.num_ctx,
                "num_predict": self.num_predict}


@dataclass
class CallRecord:
    settings: dict
    model_digest: str | None
    ollama_version: str | None
    prompt_meta: dict
    request_sha256: str
    attempts: list = field(default_factory=list)
    outcome: str = "model_failure"      # ok | timeout | model_failure
    raw_text: str | None = None
    done_reason: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    load_s: float | None = None
    prompt_eval_s: float | None = None
    eval_s: float | None = None
    total_s: float | None = None
    wall_s: float | None = None
    started_at: float | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["prompt_tokens_per_s"] = round(self.prompt_tokens / self.prompt_eval_s, 2) \
            if self.prompt_tokens and self.prompt_eval_s else None
        d["output_tokens_per_s"] = round(self.output_tokens / self.eval_s, 2) if self.output_tokens and self.eval_s else None
        d["context_overflow_risk"] = (self.prompt_tokens or 0) + self.settings["num_predict"] > self.settings["num_ctx"]
        d["truncated_at_token_limit"] = self.done_reason == "length"
        return d


class OllamaClient:
    def __init__(self, settings: LLMSettings = LLMSettings(), host: str = HOST, transport=None):
        assert host.startswith("http://127.0.0.1") or host.startswith("http://localhost"), "local Ollama only"
        self.s, self.host = settings, host
        self.http = httpx.Client(base_url=host, transport=transport,
                                 timeout=httpx.Timeout(settings.timeout_s, connect=5.0))
        self.version, self.digest = self._describe()

    def _describe(self):
        try:
            ver = self.http.get("/api/version").json().get("version")
            tags = self.http.get("/api/tags").json().get("models", [])
            dig = next((m["digest"] for m in tags if m["name"] == self.s.model), None)
            return ver, dig
        except (httpx.HTTPError, ValueError):
            return None, None

    def chat(self, messages: list[dict], schema: dict, prompt_meta: dict) -> CallRecord:
        payload = {"model": self.s.model, "messages": messages, "format": schema, "stream": False,
                   "think": self.s.think, "options": self.s.options(), "keep_alive": self.s.keep_alive}
        body = json.dumps(payload, sort_keys=True)
        rec = CallRecord(settings=asdict(self.s), model_digest=self.digest, ollama_version=self.version,
                         prompt_meta=prompt_meta, request_sha256=hashlib.sha256(body.encode()).hexdigest(),
                         started_at=time.time())
        t_all = time.perf_counter()
        for attempt in range(self.s.max_retries + 1):
            t0 = time.perf_counter()
            try:
                r = self.http.post("/api/chat", content=body, headers={"Content-Type": "application/json"})
                wall = time.perf_counter() - t0
                if r.status_code >= 500:
                    rec.attempts.append({"attempt": attempt, "outcome": "model_failure", "http": r.status_code,
                                         "error": r.text[:300], "wall_s": round(wall, 3)})
                    continue
                if r.status_code != 200:
                    rec.attempts.append({"attempt": attempt, "outcome": "model_failure", "http": r.status_code,
                                         "error": r.text[:300], "wall_s": round(wall, 3)})
                    break   # client errors (bad request, unknown model) are not transient
                j = r.json()
                ns = lambda k: round(j[k] / 1e9, 4) if j.get(k) is not None else None
                rec.raw_text = (j.get("message") or {}).get("content")
                rec.done_reason = j.get("done_reason")
                rec.prompt_tokens, rec.output_tokens = j.get("prompt_eval_count"), j.get("eval_count")
                rec.load_s, rec.prompt_eval_s = ns("load_duration"), ns("prompt_eval_duration")
                rec.eval_s, rec.total_s = ns("eval_duration"), ns("total_duration")
                rec.outcome = "ok"
                rec.attempts.append({"attempt": attempt, "outcome": "ok", "http": 200, "wall_s": round(wall, 3),
                                     "thinking_chars": len((j.get("message") or {}).get("thinking") or "")})
                break
            except httpx.TimeoutException as exc:
                rec.outcome = "timeout"
                rec.attempts.append({"attempt": attempt, "outcome": "timeout", "error": type(exc).__name__,
                                     "wall_s": round(time.perf_counter() - t0, 3)})
            except (httpx.HTTPError, ValueError) as exc:
                rec.outcome = "model_failure"
                rec.attempts.append({"attempt": attempt, "outcome": "model_failure", "error": f"{type(exc).__name__}: "
                                     f"{str(exc)[:200]}", "wall_s": round(time.perf_counter() - t0, 3)})
        if rec.attempts and rec.attempts[-1]["outcome"] != "ok":
            rec.outcome = rec.attempts[-1]["outcome"]
        rec.wall_s = round(time.perf_counter() - t_all, 3)
        return rec

    def unload(self):
        try:
            self.http.post("/api/generate", json={"model": self.s.model, "keep_alive": 0})
        except httpx.HTTPError:
            pass

    def loaded(self) -> list[dict]:
        try:
            return self.http.get("/api/ps").json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []
