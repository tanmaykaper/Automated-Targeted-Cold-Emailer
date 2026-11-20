"""Tiered LLM routing.

Two tiers, each mapped to whichever provider has a key (or LLM_PROVIDER forces one):
  fast    → structured extraction, classification, page parsing
  writer  → dossier synthesis, cold-email drafting, the QA critic

Provider defaults:
  anthropic  fast=claude-haiku-5-5   writer=claude-opus-5-5   (official SDK, structured outputs)
  groq       fast=openai/gpt-oss-20b writer=openai/gpt-oss-120b (OpenAI-compatible REST, JSON mode)
  gemini     fast=gemini-2.5-flash   writer=gemini-2.5-pro    (REST, JSON mode)
Override with LLM_FAST_MODEL / LLM_WRITER_MODEL.
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

DEFAULT_MODELS = {
    "anthropic": {"fast": "claude-haiku-5-5", "writer": "claude-opus-5-5"},
    "groq": {"fast": "openai/gpt-oss-20b", "writer": "openai/gpt-oss-120b"},
    "gemini": {"fast": "gemini-2.5-flash", "writer": "gemini-2.5-pro"},
}


class LLMError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(f"[llm:{category}] {message}")
        self.category = category


class LLM:
    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.s = settings
        self.provider = self._pick_provider()
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self._anthropic = None

    def _pick_provider(self) -> str:
        keys = self.s.keys
        avail = [p for p, k in (("anthropic", keys.anthropic), ("groq", keys.groq), ("gemini", keys.gemini)) if k]
        if self.s.llm_provider != "auto":
            if self.s.llm_provider in avail:
                return self.s.llm_provider
            log.warning("LLM_PROVIDER=%s has no key; falling back to auto", self.s.llm_provider)
        return avail[0] if avail else ""

    @property
    def available(self) -> bool:
        return bool(self.provider) and self.http.available(f"llm:{self.provider}")

    def model(self, tier: Tier) -> str:
        override = self.s.llm_fast_model if tier == "fast" else self.s.llm_writer_model
        return override or DEFAULT_MODELS.get(self.provider, {}).get(tier, "")

    def describe(self) -> str:
        if not self.provider:
            return "none (no ANTHROPIC_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)"
        return f"{self.provider} (fast={self.model('fast')}, writer={self.model('writer')})"

    # ── public API ──────────────────────────────────────────────────────────
    def structured(self, tier: Tier, system: str, prompt: str, schema: type[T], max_tokens: int = 4000,
                   effort: str = "medium") -> T:
        if not self.available:
            raise LLMError("unavailable", "no LLM provider configured")
        self.calls += 1
        if self.provider == "anthropic":
            return self._anthropic_structured(tier, system, prompt, schema, max_tokens, effort)
        raw = self._json_mode(tier, system, prompt, schema, max_tokens)
        try:
            return schema.model_validate(raw)
        except ValidationError as e:
            # one repair round-trip with the validation error
            fix = (f"{prompt}\n\nYour previous JSON failed validation:\n{str(e)[:1200]}\n"
                   f"Previous output:\n{json.dumps(raw)[:3000]}\nReturn corrected JSON only.")
            raw = self._json_mode(tier, system, fix, schema, max_tokens)
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

    def _anthropic_structured(self, tier, system, prompt, schema, max_tokens, effort):
        import anthropic
        model = self.model(tier)
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
            self.http.trip("llm:anthropic", "authentication failed")
            raise LLMError("auth", str(e)[:200])
        except anthropic.PermissionDeniedError as e:
            self.http.trip("llm:anthropic", "permission denied")
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
    def _json_mode(self, tier, system, prompt, schema, max_tokens) -> dict:
        schema_txt = json.dumps(schema.model_json_schema())[:6000]
        sys = f"{system}\n\nReturn ONLY one JSON object matching this JSON Schema:\n{schema_txt}"
        try:
            if self.provider == "groq":
                data = self.http.request(
                    "POST", "https://api.groq.com/openai/v1/chat/completions", provider="llm:groq",
                    headers={"Authorization": f"Bearer {self.s.keys.groq}"}, timeout=(8, 120),
                    json={"model": self.model(tier), "max_completion_tokens": max_tokens,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": sys}, {"role": "user", "content": prompt}]})
                u = data.get("usage") or {}
                self.usage["input_tokens"] += u.get("prompt_tokens", 0)
                self.usage["output_tokens"] += u.get("completion_tokens", 0)
                text = data["choices"][0]["message"]["content"]
            elif self.provider == "gemini":
                data = self.http.request(
                    "POST", f"https://generativelanguage.googleapis.com/v1beta/models/{self.model(tier)}:generateContent",
                    provider="llm:gemini", headers={"x-goog-api-key": self.s.keys.gemini}, timeout=(8, 120),
                    json={"systemInstruction": {"parts": [{"text": sys}]},
                          "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens}})
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            else:
                raise LLMError("unavailable", f"unknown provider {self.provider}")
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
