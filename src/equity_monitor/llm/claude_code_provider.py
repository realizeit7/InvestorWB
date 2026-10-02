"""LLM provider that runs the local Claude Code CLI non-interactively (``claude -p``) under the owner's logged-in
Claude account — no API key; usage counts toward the owner's plan limits (not unlimited, and the owner must check
their plan, authentication route and billing settings).

Isolation (the model reads untrusted filing text, so it must not be able to act, and the owner's own Claude Code
customizations must not run):
- ``--safe-mode``             disables CLAUDE.md, skills, installed plugins, hooks, MCP servers, custom commands and
                              agents, output styles (admin-managed policy settings still apply — never bypassed);
- ``--restricted`` + ``--setting-sources ""``  user, project and local settings files are not loaded;
- ``--tools ""``              no built-in tools (no shell, no file access, no web); with ``--json-schema`` the CLI adds
                              only its own ``StructuredOutput`` tool, which returns the answer;
- ``--strict-mcp-config``     no MCP servers; ``--disable-slash-commands`` no skills;
- ``--permission-mode dontAsk --permission-prompts none``  anything that would need permission is denied;
- ``--system-prompt``         our system prompt replaces Claude Code's default agent prompt;
- an empty temporary working directory, ``--no-session-persistence``.
(``--bare`` is NOT used: it accepts only API keys.)

Before any call, ``preflight`` refuses to run unless the installed CLI supports every flag above and
``claude auth status`` reports a first-party login whose method is in ``llm.claude_code_auth_methods`` (default
``claude.ai``) with no API key source; provider-routing variables (Bedrock/Vertex/Foundry/base URL) are refused, and
API-key variables are removed from the subprocess. ``claude_code_check.run_check`` proves the isolation on the
installed version with a harmless offline test (fake API server, temporary home with a hook that must not fire).

Call volume is capped per day by atomic slot reservations (``llm.max_subscription_calls_per_day``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from .base import LLMRequest, LLMResponse

_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# configurations that silently change which account / provider is billed: refuse rather than guess
_AMBIGUOUS_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL")
REQUIRED_FLAGS = ("--print", "--output-format", "--json-schema", "--tools", "--strict-mcp-config", "--safe-mode",
                  "--restricted", "--setting-sources", "--disable-slash-commands", "--permission-mode",
                  "--permission-prompts", "--system-prompt", "--no-session-persistence")


class ClaudeCodeRefused(RuntimeError):
    pass


@dataclass
class Preflight:
    ok: bool
    cli_version: str | None
    reasons: list[str] = field(default_factory=list)
    auth: dict = field(default_factory=dict)          # non-secret fields of `claude auth status` only


def isolation_args(system_prompt: str, json_schema: dict) -> list[str]:
    return ["-p", "--output-format", "json", "--json-schema", json.dumps(json_schema), "--tools", "",
            "--strict-mcp-config", "--safe-mode", "--restricted", "--setting-sources", "",
            "--disable-slash-commands", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--system-prompt", system_prompt, "--no-session-persistence"]


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    return {k: v for k, v in (os.environ if base is None else base).items() if k not in _STRIP_ENV}


def cli_version(bin_path: str, runner=subprocess.run) -> str | None:
    try:
        p = runner([bin_path, "--version"], capture_output=True, text=True, timeout=60, env=subprocess_env())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\d+\.\d+\.\d+", p.stdout or "")
    return m.group(0) if p.returncode == 0 and m else None


def missing_flags(bin_path: str, runner=subprocess.run) -> list[str]:
    p = runner([bin_path, "--help"], capture_output=True, text=True, timeout=60, env=subprocess_env())
    text = (p.stdout or "") + (p.stderr or "")
    return [f for f in REQUIRED_FLAGS if not re.search(re.escape(f) + r"\b", text)]


_AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "apiKeySource", "subscriptionType")


def preflight(bin_path: str, allowed_auth_methods: list[str], runner=subprocess.run,
              environ: dict[str, str] | None = None) -> Preflight:
    environ = dict(os.environ if environ is None else environ)
    version = cli_version(bin_path, runner)
    if version is None:
        return Preflight(False, None, [f"Claude Code CLI not found or not runnable ({bin_path}); install it and run "
                                       "`claude` once to log in"])
    reasons = []
    gone = missing_flags(bin_path, runner)
    if gone:
        reasons.append(f"installed CLI {version} lacks isolation flags {gone}: update Claude Code")
    amb = [k for k in _AMBIGUOUS_ENV if environ.get(k)]
    if amb:
        reasons.append(f"environment routes Claude Code to another provider/endpoint ({', '.join(amb)}): refusing an "
                       "ambiguous billing route; unset these for InvestorWB")
    try:
        p = runner([bin_path, "auth", "status", "--json"], capture_output=True, text=True, timeout=60,
                   env=subprocess_env(environ))
        raw = json.loads(p.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        raw = {}
    auth = {k: raw.get(k) for k in _AUTH_FIELDS if k in raw}      # never keep email, org or token fields
    if not auth.get("loggedIn"):
        reasons.append("not logged in: run `claude` once and log in with your Claude account")
    if auth.get("apiProvider") != "firstParty":
        reasons.append(f"API provider is {auth.get('apiProvider')!r}, expected 'firstParty' (Anthropic)")
    if auth.get("authMethod") not in allowed_auth_methods:
        reasons.append(f"auth method {auth.get('authMethod')!r} is not in llm.claude_code_auth_methods "
                       f"{allowed_auth_methods} (refused: cannot confirm the subscription route)")
    if auth.get("apiKeySource"):
        reasons.append(f"an API key source is configured ({auth['apiKeySource']}): calls could be billed per token")
    return Preflight(not reasons, version, reasons, auth)


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, bin_path: str = "claude", model: str | None = None, timeout_s: int = 900,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 allowed_auth_methods: list[str] | None = None, require_preflight: bool = True):
        self.bin_path, self.timeout_s, self.runner = bin_path, timeout_s, runner
        self.model = model or "claude-code-default"
        self._model_arg = model
        self.allowed_auth_methods = allowed_auth_methods or ["claude.ai"]
        self.require_preflight = require_preflight
        self._preflight: Preflight | None = None

    def ensure_ready(self) -> Preflight:
        if self._preflight is None:
            self._preflight = preflight(self.bin_path, self.allowed_auth_methods, self.runner)
        return self._preflight

    def command(self, req: LLMRequest) -> list[str]:
        cmd = [self.bin_path] + isolation_args(req.system, req.json_schema)
        if self._model_arg:
            cmd += ["--model", self._model_arg]
        return cmd

    def complete(self, req: LLMRequest) -> LLMResponse:
        if self.require_preflight:
            pf = self.ensure_ready()
            if not pf.ok:
                return LLMResponse(self.name, self.model, None, "ERROR",
                                   error="Claude Code preflight refused: " + "; ".join(pf.reasons))
        return self._run(req, subprocess_env())

    def _run(self, req: LLMRequest, env: dict[str, str]) -> LLMResponse:
        with tempfile.TemporaryDirectory(prefix="eqm_cc_") as cwd:
            try:
                proc = self.runner(self.command(req), input=req.user, capture_output=True, text=True, cwd=cwd, env=env,
                                   timeout=self.timeout_s)
            except FileNotFoundError:
                return LLMResponse(self.name, self.model, None, "ERROR",
                                   error=f"Claude Code CLI not found ({self.bin_path}); install it and run `claude` once to log in")
            except subprocess.TimeoutExpired:
                return LLMResponse(self.name, self.model, None, "ERROR", error=f"claude -p timed out after {self.timeout_s}s")
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
                return LLMResponse(self.name, self.model, None, "ERROR", error=f"claude -p exit {proc.returncode}: {tail[0][:300]}")
            return LLMResponse(self.name, self.model, None, "ERROR", error="claude -p did not return JSON output")
        usage = out.get("usage") or {}
        in_t = usage.get("input_tokens")
        out_t = usage.get("output_tokens")
        model = next(iter(out.get("modelUsage") or {}), None) or self.model
        extra = {"api_equivalent_cost_usd": out.get("total_cost_usd"), "session_id": out.get("session_id"),
                 "num_turns": out.get("num_turns"), "cli_version": (self._preflight.cli_version if self._preflight else None)}
        if proc.returncode != 0 or out.get("is_error") or out.get("subtype") not in (None, "success"):
            return LLMResponse(self.name, model, None, "ERROR", in_t, out_t,
                               error=f"claude -p reported an error (exit {proc.returncode}, {out.get('subtype')}): "
                                     f"{str(out.get('result'))[:300]}", extra=extra)
        structured = out.get("structured_output")
        text = json.dumps(structured) if structured is not None else out.get("result")
        return LLMResponse(self.name, model, text, "OK", in_t, out_t, extra=extra)
