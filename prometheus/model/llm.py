"""LLM providers.

Both adapters below are ADAPTER_ONLY in this deployment: no credential and no
reachable endpoint was present at build time, so neither has successfully
communicated with its provider. They are shipped complete and activate purely
from configuration.

The contract both enforce:

  * the model receives only precomputed deterministic features;
  * it is told, explicitly, which evidence keys exist -- and that citing anything
    else causes rejection;
  * its reply must be a single JSON object matching ``ModelSignal``;
  * a malformed or unparseable reply is retried exactly once, then fails closed.

There is no provider fallback. If the configured model fails twice, generation
fails; silently answering with a different model would make ``model_provider`` on
the passport a lie.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError

from ..provenance import Status
from ..signals.schema import ModelSignal
from .base import ModelProvider, ModelRequest, ModelResponse, ProviderError, PROMPT_VERSION

_SYSTEM = """You are the analysis stage of PROMETHEUS, an automated market intelligence service.

You will be given deterministic features that were already computed from verified
Binance spot market data. You must not perform your own arithmetic on raw prices and
you must not invent any number that is not in the feature list.

Return exactly one JSON object and nothing else. No prose, no markdown fence.

Schema:
{
  "direction": "LONG" | "SHORT" | "NEUTRAL" | "STAND_DOWN",
  "confidence": number between 0 and 1,
  "thesis": string, 24-1200 chars,
  "claims": [ { "statement": string, "evidence_keys": [string, ...] } ],
  "risk_factors": [string, ...],
  "invalidation": { "condition": string, "reference_price": number, "evidence_keys": [string, ...] }
}

Hard rules:
- Every evidence key you cite MUST appear in AVAILABLE_EVIDENCE_KEYS verbatim.
  Citing any other key causes the entire signal to be rejected and discarded.
- A LONG invalidation reference_price must be BELOW the entry reference.
  A SHORT invalidation reference_price must be ABOVE it.
- The invalidation must sit between 1 and 2000 basis points from the entry reference.
- If the evidence does not support a directional call, return STAND_DOWN with
  confidence at or below 0.5. Declining is a valid and respected outcome.
- You do not set price, you do not score yourself, and you do not decide payment.
"""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Recover the outermost balanced object if the model wrapped it in prose.
        start = text.find("{")
        if start == -1:
            raise ProviderError("model response contained no JSON object")
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError as exc:
                        raise ProviderError(f"model JSON did not parse: {exc}") from exc
        raise ProviderError("model response contained an unterminated JSON object")


def _user_prompt(request: ModelRequest) -> str:
    feats = {k: v for k, v in request.features.items() if v is not None}
    missing = sorted(k for k, v in request.features.items() if v is None)
    return json.dumps(
        {
            "asset": request.asset,
            "venue": request.venue,
            "horizon": request.horizon,
            "horizon_seconds": request.horizon_seconds,
            "entry_reference": request.entry_reference,
            "AVAILABLE_EVIDENCE_KEYS": request.available_keys,
            "UNAVAILABLE_KEYS_DO_NOT_CITE": missing,
            "features": feats,
        },
        indent=2,
        sort_keys=True,
    )


class _JsonLLMProvider(ModelProvider):
    """Shared retry-once-then-fail-closed logic."""

    def _complete(self, system: str, user: str) -> tuple[str, str]:
        """Return (text, model_version_reported_by_provider)."""
        raise NotImplementedError

    def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.time()
        system, user = _SYSTEM, _user_prompt(request)
        last_error: Exception | None = None

        for attempt in (1, 2):
            try:
                text, reported_model = self._complete(system, user)
                payload = _extract_json(text)
                signal = ModelSignal.model_validate(payload)
                return ModelResponse(
                    signal=signal,
                    provider=self.name,
                    model_version=reported_model,
                    prompt_version=PROMPT_VERSION,
                    raw={"attempt": attempt, "response": payload},
                    latency_ms=int((time.time() - started) * 1000),
                )
            except (ProviderError, ValidationError, httpx.HTTPError, KeyError, TypeError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                # One retry, then fail closed. No provider substitution.
                user = (
                    f"{user}\n\nYour previous reply was rejected: {exc}\n"
                    "Return only the corrected JSON object."
                )

        raise ProviderError(f"{self.name} failed after retry: {last_error}")


class AnthropicProvider(_JsonLLMProvider):
    name = "anthropic"
    DEFAULT_MODEL = "claude-sonnet-5"

    def __init__(self, api_key: str, model: str = "", base_url: str = "https://api.anthropic.com") -> None:
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")

    def model_version(self) -> str:
        return self.model

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "status": Status.ADAPTER_ONLY.value if not self.api_key else Status.UNVERIFIED.value,
            "is_llm": True,
            "note": (
                "no ANTHROPIC_API_KEY present; adapter has never contacted the provider"
                if not self.api_key
                else "key present; status becomes VERIFIED_LIVE only after a successful call"
            ),
        }

    def _complete(self, system: str, user: str) -> tuple[str, str]:
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        resp = httpx.post(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1600,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=60.0,
        )
        if resp.status_code != 200:
            raise ProviderError(f"anthropic HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        if not parts:
            raise ProviderError("anthropic response contained no text block")
        return "".join(parts), body.get("model", self.model)


class OllamaProvider(_JsonLLMProvider):
    name = "ollama"
    DEFAULT_MODEL = "llama3.1"

    def __init__(self, host: str, model: str = "") -> None:
        self.host = host.rstrip("/")
        self.model = model or self.DEFAULT_MODEL

    def model_version(self) -> str:
        return self.model

    def status(self) -> dict[str, Any]:
        if not self.host:
            return {
                "provider": self.name,
                "model": self.model,
                "status": Status.ADAPTER_ONLY.value,
                "is_llm": True,
                "note": "no PROM_OLLAMA_HOST configured; adapter has never contacted a server",
            }
        try:
            resp = httpx.get(f"{self.host}/api/tags", timeout=5.0)
            if resp.status_code == 200:
                names = [m.get("name") for m in resp.json().get("models", [])]
                return {
                    "provider": self.name,
                    "model": self.model,
                    "status": Status.VERIFIED_LIVE.value,
                    "is_llm": True,
                    "note": f"endpoint reachable; models present: {names}",
                }
            return {
                "provider": self.name, "model": self.model, "status": Status.BROKEN.value,
                "is_llm": True, "note": f"HTTP {resp.status_code} from /api/tags",
            }
        except httpx.HTTPError as exc:
            return {
                "provider": self.name, "model": self.model, "status": Status.UNAVAILABLE.value,
                "is_llm": True, "note": f"{type(exc).__name__}: {exc}",
            }

    def _complete(self, system: str, user: str) -> tuple[str, str]:
        if not self.host:
            raise ProviderError("PROM_OLLAMA_HOST is not set")
        resp = httpx.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=120.0,
        )
        if resp.status_code != 200:
            raise ProviderError(f"ollama HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        content = body.get("message", {}).get("content")
        if not content:
            raise ProviderError("ollama response contained no message content")
        return content, body.get("model", self.model)
