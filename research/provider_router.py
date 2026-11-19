"""Provider-independent AI gateway with intelligent Stage 2/3 routing, caching, deduplication, fallback, and cost tracking."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Protocol

import httpx

from config import Settings
from research.cache import ResearchCache, sha256_text
from research.models import ResearchBudget, TokenUsageRecord

log = logging.getLogger("prospect_mcp.research.ai")

# Reference rates ($ per 1M tokens) used ONLY for transparent estimation when providers do not report cost.
MODEL_REFERENCE_RATES_PER_M: dict[str, tuple[float, float]] = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "mixtral-8x7b-32768": (0.24, 0.24),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}


class ProviderError(Exception):
    """Raised when an AI provider call fails or returns unusable output."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    name: str
    def is_configured(self) -> bool: ...
    async def complete_json(
        self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 1200,
    ) -> dict[str, Any]: ...


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ProviderError("Empty response text from AI provider.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError("AI provider did not return valid JSON.") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Invalid JSON in AI response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("Expected a JSON object from AI provider.")
    return parsed


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = MODEL_REFERENCE_RATES_PER_M.get((model or "").strip())
    if rates is None:
        return None
    in_rate, out_rate = rates
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0, 8)


class _OpenAICompatibleProvider:
    """Shared HTTP JSON adapter for Groq and OpenAI Chat Completions APIs."""

    def __init__(
        self,
        name: str,
        endpoint_url: str,
        api_key: str | None,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.name = name
        self._endpoint = endpoint_url
        self._api_key = api_key
        self._http = http_client

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def complete_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        if not self._api_key:
            raise ProviderError(f"{self.name} API key is not configured.")
        body = {
            "model": model,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = await self._http.post(self._endpoint, json=body, headers=headers)
        if resp.status_code in (429, 500, 502, 503, 504):
            raise ProviderError(f"{self.name} HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name} rejected request (HTTP {resp.status_code}).", retryable=False)

        payload = resp.json()
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError(f"{self.name} returned no choices.")
        content = ((choices[0].get("message") or {}).get("content")) or ""
        data = _extract_json_object(content)
        usage = payload.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        reported_cost = usage.get("total_cost") or payload.get("cost_usd")
        return {
            "data": data,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "reported_cost_usd": float(reported_cost) if reported_cost is not None else None,
        }


class AnthropicProvider:
    """HTTP adapter for Anthropic Claude Messages API."""

    name = "anthropic"
    ENDPOINT = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str | None, http_client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http_client

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def complete_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        if not self._api_key:
            raise ProviderError("Anthropic API key is not configured.")
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = await self._http.post(self.ENDPOINT, json=body, headers=headers)
        if resp.status_code in (429, 500, 502, 503, 504, 529):
            raise ProviderError(f"Anthropic HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError(f"Anthropic rejected request (HTTP {resp.status_code}).", retryable=False)

        payload = resp.json()
        blocks = payload.get("content") or []
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        data = _extract_json_object(text)
        usage = payload.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        return {
            "data": data,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "reported_cost_usd": None,
        }


class ProviderRouter:
    """Routes extraction tasks across Groq, Anthropic Claude, and OpenAI with caching, dedup, and budget caps."""

    def __init__(
        self,
        settings: Settings,
        cache: ResearchCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._s = settings
        self._cache = cache
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(max(15.0, settings.request_timeout_seconds)),
            follow_redirects=False,
            transport=transport,
        )
        self._providers: dict[str, Any] = {
            "groq": _OpenAICompatibleProvider(
                "groq",
                "https://api.groq.com/openai/v1/chat/completions",
                settings.groq_api_key,
                self._http,
            ),
            "openai": _OpenAICompatibleProvider(
                "openai",
                "https://api.openai.com/v1/chat/completions",
                settings.openai_api_key,
                self._http,
            ),
            "anthropic": AnthropicProvider(settings.anthropic_api_key, self._http),
        }
        self._inflight: dict[str, asyncio.Task[dict[str, Any] | None]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def enabled_providers(self) -> list[str]:
        return [name for name, p in self._providers.items() if p.is_configured()]

    def _default_model_for(self, provider: str, stage: str) -> str:
        if provider == "groq":
            if self._s.groq_model:
                return self._s.groq_model
            return self._s.research_primary_model if stage == "primary" else "llama-3.3-70b-versatile"
        if provider == "anthropic":
            if self._s.anthropic_model:
                return self._s.anthropic_model
            return self._s.research_escalation_model if stage == "escalation" else "claude-3-5-haiku-latest"
        if provider == "openai":
            if self._s.openai_model:
                return self._s.openai_model
            return self._s.research_fallback_model
        return self._s.research_primary_model

    def route_order(self, stage: str = "primary") -> list[tuple[str, str]]:
        """Determine ordered list of (provider, model) candidates for a routing stage."""
        enabled = self.enabled_providers()
        if not enabled:
            return []

        if stage == "escalation":
            preferred = [
                self._s.research_escalation_llm_provider,
                self._s.research_fallback_llm_provider,
                self._s.research_primary_llm_provider,
                "anthropic",
                "openai",
                "groq",
            ]
            explicit_first_model = self._s.research_escalation_model
        elif stage == "fallback":
            preferred = [
                self._s.research_fallback_llm_provider,
                self._s.research_primary_llm_provider,
                self._s.research_escalation_llm_provider,
                "openai",
                "groq",
                "anthropic",
            ]
            explicit_first_model = self._s.research_fallback_model
        else:
            preferred = [
                self._s.research_primary_llm_provider,
                self._s.research_fallback_llm_provider,
                self._s.research_escalation_llm_provider,
                "groq",
                "openai",
                "anthropic",
            ]
            explicit_first_model = self._s.research_primary_model

        seen: set[str] = set()
        candidates: list[tuple[str, str]] = []
        for idx, prov in enumerate(preferred):
            if prov in ("none", "auto") or prov in seen or prov not in enabled:
                continue
            seen.add(prov)
            if idx == 0 and explicit_first_model and not getattr(self._s, f"{prov}_model", None):
                model = explicit_first_model
            else:
                model = self._default_model_for(prov, stage)
            candidates.append((prov, model))
        return candidates

    async def complete_json(
        self,
        *,
        task_type: str,
        system_prompt: str,
        user_prompt: str,
        stage: str = "primary",
        budget: ResearchBudget | None = None,
        max_tokens: int = 1200,
        force_refresh: bool = False,
        exclude_providers: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Execute JSON completion with persistent cache, in-flight deduplication, routing, and budget enforcement."""
        candidates = [
            (p, m) for p, m in self.route_order(stage)
            if not exclude_providers or p not in exclude_providers
        ]
        if not candidates:
            return None

        prompt_digest = sha256_text(f"{task_type}\n{system_prompt}\n{user_prompt}")

        # 1. Check cache across candidate provider/model keys first (zero tokens!).
        if self._cache and not force_refresh:
            for prov, model in candidates:
                ck = sha256_text(f"{prov}:{model}:{prompt_digest}")
                cached = self._cache.get_provider_response(ck)
                if cached is not None:
                    if budget is not None:
                        budget.record_llm_usage(
                            TokenUsageRecord(
                                provider=cached["provider"],
                                model=cached["model"],
                                stage=stage,
                                task_type=task_type,
                                input_tokens=0,
                                output_tokens=0,
                                cache_hit=True,
                                latency_ms=0,
                                reported_cost_usd=None,
                                estimated_cost_usd=0.0,
                                cost_basis="zero_cache",
                            )
                        )
                    return cached["response"]

        if budget is not None and not budget.can_call_llm():
            return None

        inflight_key = f"{stage}:{prompt_digest}"
        existing_task = self._inflight.get(inflight_key)
        if existing_task is not None:
            return await existing_task

        async def _execute() -> dict[str, Any] | None:
            for prov_name, model in candidates:
                provider = self._providers[prov_name]
                t0 = time.monotonic()
                for attempt in range(2):
                    try:
                        res = await provider.complete_json(model, system_prompt, user_prompt, max_tokens=max_tokens)
                        latency = int((time.monotonic() - t0) * 1000)
                        in_tok = int(res.get("input_tokens") or 0)
                        out_tok = int(res.get("output_tokens") or 0)
                        rep_cost = res.get("reported_cost_usd")
                        est_cost = None if rep_cost is not None else estimate_cost_usd(model, in_tok, out_tok)
                        basis = (
                            "provider_reported" if rep_cost is not None
                            else ("estimated" if est_cost is not None else "unavailable")
                        )
                        if budget is not None:
                            budget.record_llm_usage(
                                TokenUsageRecord(
                                    provider=prov_name,
                                    model=model,
                                    stage=stage,
                                    task_type=task_type,
                                    input_tokens=in_tok,
                                    output_tokens=out_tok,
                                    cache_hit=False,
                                    latency_ms=latency,
                                    reported_cost_usd=rep_cost,
                                    estimated_cost_usd=est_cost,
                                    cost_basis=basis,
                                )
                            )
                        data = res["data"]
                        if self._cache:
                            ck = sha256_text(f"{prov_name}:{model}:{prompt_digest}")
                            self._cache.set_provider_response(
                                ck, prov_name, model, data, in_tok, out_tok,
                                self._s.research_field_ttl_seconds,
                            )
                        return data
                    except ProviderError as exc:
                        log.warning("AI call %s/%s failed (%s)", prov_name, model, exc)
                        if exc.retryable and attempt == 0:
                            await asyncio.sleep(0.35)
                            continue
                        break
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        log.warning("AI transport error %s/%s (%s)", prov_name, model, type(exc).__name__)
                        if attempt == 0:
                            await asyncio.sleep(0.35)
                            continue
                        break
            return None

        task = asyncio.create_task(_execute())
        self._inflight[inflight_key] = task
        try:
            return await task
        finally:
            self._inflight.pop(inflight_key, None)

    async def cross_verify(
        self,
        *,
        task_type: str,
        system_prompt: str,
        user_prompt: str,
        primary_provider: str | None = None,
        budget: ResearchBudget | None = None,
    ) -> dict[str, Any] | None:
        """Invoke a distinct configured AI provider ONLY for high-stakes conflict escalation."""
        exclude = {primary_provider} if primary_provider else None
        return await self.complete_json(
            task_type=f"{task_type}_cross_verify",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            stage="escalation",
            budget=budget,
            exclude_providers=exclude,
        )
