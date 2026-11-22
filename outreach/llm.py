"""Tiered LLM routing with per-model fallback chains.

Two tiers:
  fast    → structured fact extraction, angle synthesis
  writer  → cold-email drafting and the QA critic

Each tier is an ordered chain of (provider, model). Only providers with a key are used. When a
model is out of quota (e.g. Gemini free tier: 20 requests/day *per model*), rate-limited past its
retries, overloaded or failing, the call moves to the next model in the chain, and that model is
skipped for the rest of the run.

  fast:   claude-haiku-5-5 → groq gpt-oss-20b → gemini-3.1-flash-lite → gemini-flash-lite-latest → gemini-3.5-flash-lite
  writer: claude-opus-5-5 → gemini-3.6-flash → gemini-3.5-flash → gemini-3-flash-preview → gemini-3.7-flash
          → groq gpt-oss-120b

LLM_PROVIDER restricts both chains to one provider; LLM_FAST_MODEL / LLM_WRITER_MODEL put a model first.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from .http import Http, ProviderError
from .settings import Settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "writer"]
T = TypeVar("T", bound=BaseModel)

# Gemini free-tier availability verified 2026-11-22: Pro models have 0 free quota, 2.5-* is closed to new users.
CHAINS: dict[str, list[tuple[str, str]]] = {
    "fast": [("anthropic", "claude-haiku-5-5"), ("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-3.1-flash-lite"),
             ("gemini", "gemini-flash-lite-latest"), ("gemini", "gemini-3.5-flash-lite")],
    "writer": [("anthropic", "claude-opus-5-5"), ("gemini", "gemini-3.6-flash"), ("gemini", "gemini-3.5-flash"),
               ("gemini", "gemini-3-flash-preview"), ("gemini", "gemini-3.7-flash"), ("groq", "openai/gpt-oss-120b")],
}
NEXT_MODEL_ON = {"quota", "rate_limit", "circuit_open", "server_error", "timeout", "network", "auth", "parse",
                 "truncated", "refusal", "unavailable"}


class LLMError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(f"[llm:{category}] {message}")
        self.category = category


def _provider_of(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gemini-"):
        return "gemini"
    return "groq"


class LLM:
    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.s = settings
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.model_calls: dict[str, int] = {}
        self.exhausted: dict[str, str] = {}
        self._anthropic = None
        self.chains = {tier: self._chain(tier) for tier in ("fast", "writer")}

    def _has_key(self, provider: str) -> bool:
        return bool(getattr(self.s.keys, provider, ""))

    def _keys(self, provider: str) -> list[str]:
        """GEMINI_API_KEY may hold several comma-separated keys (one per Google Cloud project):
        free-tier quota is per project per model, so each key multiplies the daily budget."""
        return [k.strip() for k in getattr(self.s.keys, provider, "").split(",") if k.strip()]

    def _chain(self, tier: Tier) -> list[tuple[str, str]]:
        chain = list(CHAINS[tier])
        override = self.s.llm_fast_model if tier == "fast" else self.s.llm_writer_model
        if override:
            chain = [(_provider_of(override), override)] + [c for c in chain if c[1] != override]
        if self.s.llm_provider != "auto":
            forced = [c for c in chain if c[0] == self.s.llm_provider]
            if forced and self._has_key(self.s.llm_provider):
                chain = forced
            else:
                log.warning("LLM_PROVIDER=%s has no key; using all configured providers", self.s.llm_provider)
        out = []
        for provider, model in chain:
            for i, _ in enumerate(self._keys(provider) if provider == "gemini" else [getattr(self.s.keys, provider, "")]):
                if _:
                    out.append((provider, model, i))
        # Model-major order: the best model is tried on every key before falling back to a weaker model
        return out


    def _live(self, tier: Tier) -> list[tuple[str, str, int]]:
        return [c for c in self.chains[tier] if f"{c[0]}:{c[1]}#{c[2]}" not in self.exhausted]

    @property
    def available(self) -> bool:
        return bool(self._live("fast") and self._live("writer"))

    @property
    def provider(self) -> str:
        live = self._live("writer") or self.chains["writer"]
        return live[0][0] if live else ""

    def model(self, tier: Tier) -> str:
        live = self._live(tier) or self.chains[tier]
        return live[0][1] if live else ""

    def used_models(self) -> str:
        return ", ".join(f"{m}×{n}" for m, n in self.model_calls.items())

    def describe(self) -> str:
        if not self.chains["writer"]:
            return "none (no ANTHROPIC_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)"
        fmt = lambda t: " → ".join(dict.fromkeys(m for _, m, _i in self.chains[t]))  # noqa: E731
        nkeys = len(self._keys("gemini"))
        extra = f" (×{nkeys} Gemini keys)" if nkeys > 1 else ""
        return f"fast: {fmt('fast')} | writer: {fmt('writer')}{extra}"

    # ── public API ──────────────────────────────────────────────────────────
    def structured(self, tier: Tier, system: str, prompt: str, schema: type[T], max_tokens: int = 4000,
                   effort: str = "medium") -> T:
        last: LLMError | None = None
        for provider, model, ki in self._live(tier):
            self.calls += 1
            self.model_calls[model] = self.model_calls.get(model, 0) + 1
            try:
                if provider == "anthropic":
                    return self._anthropic_structured(model, tier, system, prompt, schema, max_tokens, effort)
                return self._validated(provider, model, ki, system, prompt, schema, max_tokens)
            except LLMError as e:
                last = e
                if e.category not in NEXT_MODEL_ON:
                    raise
                if e.category not in ("parse", "truncated", "refusal"):
                    # quota / auth / overloaded (5xx) / timeouts: skip this model for the rest of the run
                    self.exhausted[f"{provider}:{model}#{ki}"] = e.category
                log.warning("%s/%s failed (%s); trying next model in the %s chain", provider, model, e.category, tier)
        raise last or LLMError("unavailable", "no LLM provider configured")

    def _validated(self, provider, model, ki, system, prompt, schema, max_tokens):
        raw = self._json_mode(provider, model, ki, system, prompt, schema, max_tokens)
        try:
            return schema.model_validate(raw)
        except ValidationError as e:
            fix = (f"{prompt}\n\nYour previous JSON failed validation:\n{str(e)[:1200]}\n"
                   f"Previous output:\n{json.dumps(raw)[:3000]}\nReturn corrected JSON only.")
            raw = self._json_mode(provider, model, ki, system, fix, schema, max_tokens)
            try:
                return schema.model_validate(raw)
            except ValidationError as e2:
                raise LLMError("parse", f"schema validation failed twice: {str(e2)[:300]}")

    # ── Anthropic (official SDK) ────────────────────────────────────────────
    def _client(self):
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=self.s.keys.anthropic, max_retries=3, timeout=180.0)
        return self._anthropic

    def _anthropic_structured(self, model, tier, system, prompt, schema, max_tokens, effort):
        import anthropic
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": prompt}], output_format=schema,
                      output_config={"effort": "low" if tier == "fast" else effort})
        try:
            if model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")):
                # Server-side refusal fallback (opt-in): a policy decline is retried on a fallback model.
                resp = self._client().beta.messages.parse(betas=["server-side-fallback-2026-07-01"],
                                                          fallbacks="default", **kwargs)
            else:
                resp = self._client().messages.parse(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("auth", str(e)[:200])
        except anthropic.PermissionDeniedError as e:
            raise LLMError("auth", str(e)[:200])
        except anthropic.RateLimitError as e:
            raise LLMError("rate_limit", str(e)[:200])
        except anthropic.BadRequestError as e:
            raise LLMError("client_error", str(e)[:300])
        except anthropic.APIStatusError as e:
            raise LLMError("server_error" if e.status_code >= 500 else "client_error", str(e)[:200])
        except anthropic.APIConnectionError as e:
            raise LLMError("network", str(e)[:200])
        usage = getattr(resp, "usage", None)
        if usage:
            self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            raise LLMError("refusal", "model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("truncated", "hit max_tokens")
        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            raise LLMError("parse", "no parsed output")
        return parsed

    # ── JSON-mode REST providers ────────────────────────────────────────────
    def _json_mode(self, provider, model, ki, system, prompt, schema, max_tokens) -> dict:
        schema_txt = json.dumps(schema.model_json_schema())[:6000]
        sys = f"{system}\n\nReturn ONLY one JSON object matching this JSON Schema:\n{schema_txt}"
        pid = f"llm:{provider}:{model}#{ki}"
        try:
            if provider == "groq":
                data = self.http.request(
                    "POST", "https://api.groq.com/openai/v1/chat/completions", provider=pid,
                    headers={"Authorization": f"Bearer {self.s.keys.groq}"}, timeout=(8, 90),
                    json={"model": model, "max_completion_tokens": max_tokens * 2,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": sys}, {"role": "user", "content": prompt}]})
                u = data.get("usage") or {}
                self.usage["input_tokens"] += u.get("prompt_tokens", 0)
                self.usage["output_tokens"] += u.get("completion_tokens", 0)
                text = data["choices"][0]["message"]["content"]
            elif provider == "gemini":
                data = self.http.request(
                    "POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    provider=pid, headers={"x-goog-api-key": self._keys("gemini")[ki]}, timeout=(8, 90),
                    json={"systemInstruction": {"parts": [{"text": sys}]},
                          "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          # thinking tokens count toward the output budget on Gemini 3.x — leave headroom
                          "generationConfig": {"responseMimeType": "application/json",
                                               "maxOutputTokens": max(max_tokens * 3, 8192)}})
                cand = data["candidates"][0]
                if cand.get("finishReason") == "MAX_TOKENS":
                    raise LLMError("truncated", "hit maxOutputTokens")
                u = data.get("usageMetadata") or {}
                self.usage["input_tokens"] += u.get("promptTokenCount", 0)
                self.usage["output_tokens"] += u.get("candidatesTokenCount", 0)
                text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
            else:
                raise LLMError("unavailable", f"unknown provider {provider}")
        except ProviderError as e:
            raise LLMError(e.category, str(e))
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("parse", f"unexpected response shape: {e}")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
            raise LLMError("parse", "model did not return JSON")
