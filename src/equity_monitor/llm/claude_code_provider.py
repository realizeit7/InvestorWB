"""LLM provider that runs the local Claude Code CLI non-interactively (``claude -p``) under the owner's logged-in
Claude account — no API key, no per-token API billing; usage counts toward the owner's plan limits.

Isolation (the model reads untrusted filing text, so it must not be able to act):
- ``--tools ""``            no built-in tools (no shell, no file access, no web);
- ``--strict-mcp-config``   no MCP servers;
- ``--system-prompt``       our system prompt replaces Claude Code's default agent prompt;
- an empty temporary working directory (no project CLAUDE.md or files are discovered);
- ``--no-session-persistence``;
- ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` are removed from the subprocess environment so the call always
  uses the logged-in account rather than API billing. (``--bare`` is NOT used: it accepts only API keys.)
- ``--json-schema`` asks for structured output; the result is still validated by our own strict schema.

Call volume is capped per day (``llm.max_subscription_calls_per_day``). Check your plan's terms for automated use.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Callable

from .base import LLMRequest, LLMResponse

_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, bin_path: str = "claude", model: str | None = None, timeout_s: int = 900,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.bin_path, self.timeout_s, self.runner = bin_path, timeout_s, runner
        self.model = model or "claude-code-default"
        self._model_arg = model

    def command(self, req: LLMRequest) -> list[str]:
        cmd = [self.bin_path, "-p", "--output-format", "json", "--json-schema", json.dumps(req.json_schema),
               "--tools", "", "--strict-mcp-config", "--system-prompt", req.system, "--no-session-persistence"]
        if self._model_arg:
            cmd += ["--model", self._model_arg]
        return cmd

    def complete(self, req: LLMRequest) -> LLMResponse:
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        with tempfile.TemporaryDirectory(prefix="eqm_cc_") as cwd:
            try:
                proc = self.runner(self.command(req), input=req.user, capture_output=True, text=True, cwd=cwd, env=env,
                                   timeout=self.timeout_s)
            except FileNotFoundError:
                return LLMResponse(self.name, self.model, None, "ERROR",
                                   error=f"Claude Code CLI not found ({self.bin_path}); install it and run `claude` once to log in")
            except subprocess.TimeoutExpired:
                return LLMResponse(self.name, self.model, None, "ERROR", error=f"claude -p timed out after {self.timeout_s}s")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            return LLMResponse(self.name, self.model, None, "ERROR", error=f"claude -p exit {proc.returncode}: {tail[0][:300]}")
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return LLMResponse(self.name, self.model, None, "ERROR", error="claude -p did not return JSON output")
        usage = out.get("usage") or {}
        in_t = usage.get("input_tokens")
        out_t = usage.get("output_tokens")
        model = next(iter(out.get("modelUsage") or {}), None) or self.model
        extra = {"api_equivalent_cost_usd": out.get("total_cost_usd"), "session_id": out.get("session_id"),
                 "num_turns": out.get("num_turns")}
        if out.get("is_error") or out.get("subtype") not in (None, "success"):
            return LLMResponse(self.name, model, None, "ERROR", in_t, out_t,
                               error=f"claude -p reported an error ({out.get('subtype')}): {str(out.get('result'))[:300]}",
                               extra=extra)
        structured = out.get("structured_output")
        text = json.dumps(structured) if structured is not None else out.get("result")
        return LLMResponse(self.name, model, text, "OK", in_t, out_t, extra=extra)
