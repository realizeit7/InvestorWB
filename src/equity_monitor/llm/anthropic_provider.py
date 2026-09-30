"""Anthropic (Claude) provider using the official ``anthropic`` Python SDK.

Install with ``uv sync --extra llm`` and provide credentials the SDK can resolve
(``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile). No key is needed for tests.

Request shape (per the Claude API reference, 2026-09):
- model default ``claude-opus-5-5``; thinking is adaptive by default on this model and cannot be
  disabled, so we control depth with ``output_config.effort``.
- structured JSON via ``output_config.format`` (``json_schema``); the application still validates
  the result with its own strict pydantic schema.
- server-side refusal fallback (``fallbacks: "default"``, beta ``server-side-fallback-2026-07-01``)
  is enabled by default and can be switched off with ``llm.use_server_fallbacks: false``.
"""

from __future__ import annotations

import copy

from .base import LLMRequest, LLMResponse, LLMUnavailable

_UNSUPPORTED_KEYS = {"maxLength", "minLength", "maxItems", "minItems", "pattern", "default", "title", "format",
                     "exclusiveMinimum", "exclusiveMaximum", "minimum", "maximum"}


def simplify_schema(schema: dict) -> dict:
    """Inline $defs and drop validation keywords the structured-output grammar may not accept.

    Dropped constraints are re-checked locally by pydantic after the response arrives.
    """
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node.pop("$ref").split("/")[-1]
                node.update(walk(copy.deepcopy(defs[ref])))
            for k in list(node):
                if k in _UNSUPPORTED_KEYS:
                    node.pop(k)
                else:
                    node[k] = walk(node[k])
            if node.get("type") == "object":
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high", use_fallbacks: bool = True):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise LLMUnavailable("the 'anthropic' package is not installed; run `uv sync --extra llm`") from exc
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.use_fallbacks = use_fallbacks

    def complete(self, req: LLMRequest) -> LLMResponse:
        anthropic = self._anthropic
        params = dict(
            model=self.model,
            max_tokens=req.max_output_tokens,
            system=req.system,
            messages=[{"role": "user", "content": req.user}],
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": simplify_schema(req.json_schema)}},
        )
        try:
            if self.use_fallbacks:
                msg = self.client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                                       fallbacks="default")
            else:
                msg = self.client.messages.create(**params)
        except anthropic.BadRequestError as e:
            return LLMResponse(self.name, self.model, None, "ERROR", error=f"bad request: {e.message}")
        except anthropic.AuthenticationError:
            return LLMResponse(self.name, self.model, None, "ERROR", error="authentication failed (check credentials)")
        except anthropic.RateLimitError as e:
            return LLMResponse(self.name, self.model, None, "ERROR", error=f"rate limited: {e.message}")
        except anthropic.APIStatusError as e:
            return LLMResponse(self.name, self.model, None, "ERROR", error=f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            return LLMResponse(self.name, self.model, None, "ERROR", error="network error")
        usage = getattr(msg, "usage", None)
        in_t = getattr(usage, "input_tokens", None)
        out_t = getattr(usage, "output_tokens", None)
        if msg.stop_reason == "refusal":
            return LLMResponse(self.name, msg.model, None, "REFUSED", in_t, out_t, error="model declined the request")
        text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), None)
        status = "OK" if msg.stop_reason != "max_tokens" else "ERROR"
        return LLMResponse(self.name, msg.model, text, status, in_t, out_t,
                           error="output truncated at max_tokens" if status == "ERROR" else None,
                           extra={"request_id": getattr(msg, "_request_id", None)})
