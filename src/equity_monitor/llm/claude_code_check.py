"""`eqm llm claude-check`: prove, on the INSTALLED Claude Code version, that InvestorWB's ``claude -p`` call is isolated.

Offline and credential-free by default — nothing is sent to Anthropic and no plan usage is consumed:
- a temporary HOME holds a user-level ``settings.json`` with hooks, a user ``CLAUDE.md`` and a user MCP server, and
  the working directory holds a project ``.claude/settings.json`` with hooks and a project ``CLAUDE.md``; every hook
  and the MCP server only ``touch`` a marker file;
- the CLI is pointed at a local fake Messages API (127.0.0.1) with a dummy key, so the whole pipeline runs, including
  the structured answer;
- a CONTROL run without the isolation flags must fire the hooks (otherwise the test proves nothing); the ISOLATED run
  (exact flags used for real calls) must fire none, send our system prompt without either CLAUDE.md, offer only the
  CLI's StructuredOutput tool, and return the structured answer.
Then ``claude auth status`` is checked (non-secret fields only). With ``live=True`` (owner-authorized only) one tiny
real call is made under the owner's login. Results are stored append-only in ``llm_provider_check``; unattended
judging requires a PASS for the installed CLI version.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from ..app import App
from ..db.core import insert, one
from ..util import new_id, to_json
from .claude_code_provider import cli_version, isolation_args, missing_flags, preflight

MARK_SYSTEM = "EQM-ISOLATION-CHECK-SYSTEM"
MARK_USER_MD = "EQM-USER-CLAUDE-MD"
MARK_PROJECT_MD = "EQM-PROJECT-CLAUDE-MD"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"]}


def _sse(ev: str, data: dict) -> bytes:
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()


class _FakeAPI(BaseHTTPRequestHandler):
    """Minimal Messages API: first turn answers with a StructuredOutput tool call, then ends the turn."""
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            b = {}
        if "count_tokens" in self.path or not self.path.startswith("/v1/messages"):
            return self._send(b'{"input_tokens": 1}', "application/json")
        msgs = b.get("messages", [])
        answered = any(isinstance(m.get("content"), list) and any(c.get("type") == "tool_result" for c in m["content"])
                       for m in msgs)
        self.requests.append({"tools": [t.get("name") for t in b.get("tools", [])], "system": json.dumps(b.get("system")),
                              "messages": json.dumps(msgs)})
        content = ([{"type": "text", "text": "done"}] if answered else
                   [{"type": "tool_use", "id": "toolu_check", "name": "StructuredOutput", "input": {"ok": "isolated"}}])
        stop = "end_turn" if answered else "tool_use"
        model = b.get("model", "fake")
        if not b.get("stream"):
            return self._send(json.dumps({"id": "msg_check", "type": "message", "role": "assistant", "model": model,
                                          "content": content, "stop_reason": stop, "stop_sequence": None,
                                          "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(), "application/json")
        out = _sse("message_start", {"type": "message_start", "message": {
            "id": "msg_check", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})
        for i, c in enumerate(content):
            if c["type"] == "text":
                out += _sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}})
                out += _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "text_delta", "text": c["text"]}})
            else:
                out += _sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}})
                out += _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(c["input"])}})
            out += _sse("content_block_stop", {"type": "content_block_stop", "index": i})
        out += _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"output_tokens": 1}})
        out += _sse("message_stop", {"type": "message_stop"})
        self._send(out, "text/event-stream")

    def do_GET(self):  # noqa: N802
        self._send(b"", "text/plain", 404)

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _hooks(marker_prefix: Path) -> dict:
    return {"hooks": {ev: [{"hooks": [{"type": "command", "command": f"touch '{marker_prefix}_{ev}'"}]}]
                      for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")}}


def isolation_test(bin_path: str, timeout_s: int = 120) -> dict:
    """Run the control and isolated invocations against a local fake API. Returns findings (no secrets involved)."""
    _FakeAPI.requests = []
    server = HTTPServer(("127.0.0.1", 0), _FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="eqm_ccchk_") as root:
            rootp = Path(root)
            home, work, marks = rootp / "home", rootp / "work", rootp / "markers"
            for d in (home / ".claude", work / ".claude", marks):
                d.mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(json.dumps(_hooks(marks / "user")))
            (home / ".claude" / "CLAUDE.md").write_text(f"Always mention {MARK_USER_MD}.\n")
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {"eqm_check": {
                "command": "touch", "args": [str(marks / "user_mcp")]}}}))
            (work / ".claude" / "settings.json").write_text(json.dumps(_hooks(marks / "project")))
            (work / "CLAUDE.md").write_text(f"Always mention {MARK_PROJECT_MD}.\n")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "ANTHROPIC_API_KEY": "eqm-offline-dummy-key",
                   "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}", "NO_PROXY": "127.0.0.1,localhost",
                   "no_proxy": "127.0.0.1,localhost", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

            def run(args):
                try:
                    p = subprocess.run([bin_path] + args, input="Return ok.", capture_output=True, text=True, cwd=work,
                                       env=env, timeout=timeout_s)
                    return p.returncode, p.stdout
                except subprocess.TimeoutExpired:
                    return None, ""
            control_rc, _ = run(["-p", "--output-format", "json", "--tools", ""])
            control_marks = sorted(p.name for p in marks.iterdir())
            for p in marks.iterdir():
                p.unlink()
            n_before = len(_FakeAPI.requests)
            control_reqs = list(_FakeAPI.requests)
            iso_rc, iso_out = run(isolation_args(MARK_SYSTEM, SCHEMA))
            iso_marks = sorted(p.name for p in marks.iterdir())
            iso_reqs = _FakeAPI.requests[n_before:]
    finally:
        server.shutdown()
    try:
        result = json.loads(iso_out)
    except json.JSONDecodeError:
        result = {}
    sent = " ".join(r["system"] + r["messages"] for r in iso_reqs)
    control_sent = " ".join(r["system"] + r["messages"] for r in control_reqs)
    findings = {
        "control_hooks_fired": control_marks,
        "control_claude_md_sent": [m for m in (MARK_USER_MD, MARK_PROJECT_MD) if m in control_sent],
        "isolated_hooks_fired": iso_marks,
        "isolated_requests": len(iso_reqs),
        "isolated_tools_offered": sorted({t for r in iso_reqs for t in r["tools"]}),
        "system_prompt_is_ours": bool(iso_reqs) and all(MARK_SYSTEM in r["system"] for r in iso_reqs),
        "user_claude_md_sent": MARK_USER_MD in sent,
        "project_claude_md_sent": MARK_PROJECT_MD in sent,
        "structured_output": result.get("structured_output"),
        "exit_codes": {"control": control_rc, "isolated": iso_rc},
    }
    problems = []
    if not control_marks:
        problems.append("control run fired no hook: the isolation test cannot demonstrate anything on this install")
    if iso_marks:
        problems.append(f"isolated run executed user/project hooks or MCP servers: {iso_marks}")
    if not iso_reqs:
        problems.append("isolated run made no request to the fake API")
    if set(findings["isolated_tools_offered"]) - {"StructuredOutput"}:
        problems.append(f"isolated run offered tools {findings['isolated_tools_offered']}")
    if not findings["system_prompt_is_ours"]:
        problems.append("isolated run did not use InvestorWB's system prompt")
    if findings["user_claude_md_sent"] or findings["project_claude_md_sent"]:
        problems.append("a CLAUDE.md file was loaded into the isolated run")
    if findings["structured_output"] != {"ok": "isolated"}:
        problems.append(f"structured answer not returned (got {findings['structured_output']!r})")
    findings["problems"] = problems
    return findings


def run_check(app: App, *, live: bool = False, isolation: Callable | None = None) -> dict:
    """Full check; records an append-only ``llm_provider_check`` row. ``live`` makes ONE tiny real call (owner only)."""
    s = app.settings.llm
    bin_path = s.claude_code_bin
    details: dict = {"cli_version": cli_version(bin_path)}
    problems: list[str] = []
    if details["cli_version"] is None:
        problems.append(f"Claude Code CLI not found or not runnable ({bin_path})")
    else:
        gone = missing_flags(bin_path)
        if gone:
            problems.append(f"installed CLI lacks isolation flags {gone}")
        iso = (isolation or isolation_test)(bin_path)
        details["isolation"] = iso
        problems += iso["problems"]
        pf = preflight(bin_path, s.claude_code_auth_methods)
        details["auth"] = pf.auth
        problems += [r for r in pf.reasons if "lacks isolation flags" not in r]
        if live and not problems:
            from .base import LLMRequest
            from .claude_code_provider import ClaudeCodeProvider
            from .service import call
            prov = ClaudeCodeProvider(bin_path, s.model, s.claude_code_timeout_s, allowed_auth_methods=s.claude_code_auth_methods)
            cid, parsed = call(app, prov, LLMRequest("provider_check", "claude-check-v1", "Answer with JSON only.",
                                                     "Return ok set to the word yes.", SCHEMA, 200), {"check": True})
            details["live"] = {"llm_call_id": cid, "parsed": parsed}
            if not parsed or "ok" not in parsed:
                problems.append("live call did not return the structured answer")
    details["problems"] = problems
    status = "PASS" if not problems else "FAIL"
    insert(app.conn, "llm_provider_check", {"id": new_id("lpc"), "provider": "claude_code",
                                            "cli_version": details["cli_version"], "status": status, "live": int(live),
                                            "details_json": to_json(details), "checked_at": app.now_iso()})
    return {"status": status, **details}


def validated(app: App, version: str | None) -> tuple[bool, str]:
    """Unattended claude_code judging needs a PASS check for exactly the installed CLI version."""
    if version is None:
        return False, "Claude Code CLI not found"
    r = one(app.conn, "SELECT status, checked_at FROM llm_provider_check WHERE provider='claude_code' AND cli_version=? "
                      "ORDER BY checked_at DESC, rowid DESC LIMIT 1", (version,))
    if r is None:
        return False, f"no `eqm llm claude-check` has been run for Claude Code {version}"
    if r["status"] != "PASS":
        return False, f"the latest `eqm llm claude-check` for Claude Code {version} FAILED ({r['checked_at']})"
    return True, f"isolation check passed for Claude Code {version} at {r['checked_at']}"
