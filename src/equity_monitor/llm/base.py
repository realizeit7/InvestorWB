"""Provider-neutral LLM interface.

The application never lets model output execute actions: responses are parsed into strict
schemas (extra fields rejected), claims are verified against stored evidence, and only the
deterministic policy engine turns validated inputs into recommendations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

# USD per million tokens (input, output). Source: Anthropic pricing as cached 2026-09-25 in the
# Claude API reference. Update when pricing changes; unknown models record cost as unknown.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
}


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMRequest:
    purpose: str
    prompt_version: str
    system: str
    user: str
    json_schema: dict
    max_output_tokens: int = 16000


@dataclass
class LLMResponse:
    provider: str
    model: str
    text: str | None
    status: str                         # OK | ERROR | REFUSED
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def cost_usd(self) -> Decimal | None:
        p = PRICING.get(self.model)
        if p is None or self.input_tokens is None or self.output_tokens is None:
            return None
        return (Decimal(self.input_tokens) * p[0] + Decimal(self.output_tokens) * p[1]) / Decimal(1_000_000)

    def json(self) -> Any:
        return json.loads(self.text or "")


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, req: LLMRequest) -> LLMResponse: ...


class NoLLM:
    """Default: no inference. Features degrade to deterministic-only output."""
    name = "none"
    model = "none"

    def complete(self, req: LLMRequest) -> LLMResponse:
        raise LLMUnavailable("LLM provider is 'none'. Configure llm.provider and credentials to enable drafting "
                             "(see README 'Credentials and settings'). Deterministic features still work.")


class FixtureLLM:
    """Deterministic responses for tests/demos: a callable (request -> JSON-able object or text)."""
    name = "fixture"

    def __init__(self, responder: Callable[[LLMRequest], Any], model: str = "fixture-model"):
        self.responder = responder
        self.model = model

    def complete(self, req: LLMRequest) -> LLMResponse:
        out = self.responder(req)
        text = out if isinstance(out, str) else json.dumps(out, default=str)
        return LLMResponse(self.name, self.model, text, "OK", input_tokens=len(req.user) // 4,
                           output_tokens=len(text) // 4)
