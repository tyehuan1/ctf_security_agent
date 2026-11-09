"""Solver Agent: observe->act loop over the Duke gateway with native tool calling.

Protocol used by controller.runner:
  reset(workspace, context)           -> start the sandbox, seed the conversation
  request_next_action(observations)   -> (Action, usage) from one chat completion
  execute(action, timeout_s)          -> run a non-submit tool inside the sandbox
  kill()                              -> best-effort teardown (sandbox + container)

The Agent never sees chal.flag or the challenge metadata object: the context argument carries
only the listed contestant files, the sanitized description, and the local endpoint.
submit_flag is intercepted here and delivered to the controller's bound verifier; it never
reaches the sandbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from solver.llm import GatewayClient, ModelReply, ToolCall
from solver.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from solver.sandbox import Sandbox
from tools.runner import ToolRunner
from tools.specs import tool_specs


@dataclass
class Action:
    kind: str                          # "tool" | "submit_flag" | "give_up"
    tool: Optional[str] = None
    arguments: dict = None
    flag: Optional[str] = None
    tool_call_id: Optional[str] = None

    def __post_init__(self):
        if self.arguments is None:
            self.arguments = {}

    @property
    def is_flag_submission(self) -> bool:
        return self.kind == "submit_flag"


def _strip_candidate(s) -> str:
    s = str(s or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1].strip()
    return s


class Agent:
    def __init__(self, settings: dict, network: str, run_id: str, transcript_path: Optional[Path] = None):
        self.settings = settings
        self.network = network
        self.run_id = run_id
        self.client = GatewayClient(settings, transcript_path)
        self.messages: list[dict] = []
        self.specs = tool_specs()
        self.sandbox: Optional[Sandbox] = None
        self.runner: Optional[ToolRunner] = None
        self._awaiting: Optional[ToolCall] = None       # last tool call waiting for its result message
        self._gave_up = False
        self._verified = False

    # ---------------------------------------------------------------- lifecycle

    def reset(self, workspace: Path, context: dict) -> None:
        self.sandbox = Sandbox(self.network, workspace, self.run_id)
        self.sandbox.start()
        self.runner = ToolRunner(self.sandbox, context.get("target"),
                                 int(self.settings["budget"]["max_chars"]))
        user = {
            "role": "user",
            "content": (
                "You are given a Capture-the-Flag practice puzzle in the sandbox at /workspace.\n\n"
                f"Challenge id : {context['challenge_id']}\n"
                f"Name         : {context.get('name')}\n"
                f"Category     : {context.get('category')}\n"
                f"Flag format  : {context.get('flag_format')}\n"
                f"Files        : {context.get('files') or '(none)'}\n"
                f"Description  : {context.get('description') or '(none)'}\n"
                f"Service      : {context.get('target') or '(no local service for this puzzle)'}\n\n"
                "Begin with the observe step. Call tools; do not describe what you will do without "
                "calling one. When you are confident, submit with submit_flag.\n"
                f"(prompt_version={PROMPT_VERSION})"
            ),
        }
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}, user]

    def kill(self) -> None:
        if self.sandbox:
            try:
                self.sandbox.stop()
            finally:
                self.sandbox = None

    # ---------------------------------------------------------------- loop API

    def request_next_action(self, observations: list) -> tuple[Action, dict]:
        # If the previous turn was a submit_flag, the controller just appended a verification
        # observation; wire it back to the model as the tool's result.
        if self._awaiting and self._awaiting.name == "submit_flag" and observations:
            last = observations[-1]
            verified = bool(getattr(last, "verified", False) or (isinstance(last, dict) and last.get("verified")))
            self.messages.append({
                "role": "tool", "tool_call_id": self._awaiting.id,
                "content": json.dumps({"verified": verified}),
            })
            self._awaiting = None
            if verified:
                self._verified = True

        remaining = int(self.settings["budget"]["max_turns"]) - len([m for m in self.messages if m["role"] == "assistant"])
        timeout = float(self.settings["model"].get("timeout_s", 120))
        reply = self.client.chat(self.messages, tools=self.specs, timeout_s=timeout,
                                 purpose=f"turn_{len([m for m in self.messages if m['role'] == 'assistant']) + 1}")
        self._append_assistant(reply)

        if self._verified:
            return Action(kind="give_up"), reply.usage       # stop; the loop will see no further work
        if not reply.tool_calls:
            self._gave_up = True
            return Action(kind="give_up"), reply.usage

        tc = reply.tool_calls[0]                             # one action per turn (first tool_call wins)
        for extra in reply.tool_calls[1:]:                   # satisfy the API's tool_call accounting
            self.messages.append({"role": "tool", "tool_call_id": extra.id,
                                  "content": json.dumps({"status": "skipped",
                                                         "note": "one tool call per turn; call again if needed"})})

        args = tc.arguments if isinstance(tc.arguments, dict) else {}
        self._awaiting = tc
        if tc.name == "submit_flag":
            return Action(kind="submit_flag", flag=_strip_candidate(args.get("flag")),
                          tool_call_id=tc.id), reply.usage
        return Action(kind="tool", tool=tc.name, arguments=args, tool_call_id=tc.id), reply.usage

    def execute(self, action: Action, timeout_s: float) -> dict:
        assert self.runner is not None, "reset() must run before execute()"
        result = self.runner.execute(action.tool, action.arguments, timeout_s)
        self.messages.append({"role": "tool", "tool_call_id": action.tool_call_id,
                              "content": json.dumps(result, ensure_ascii=False)})
        self._awaiting = None
        return result

    # ---------------------------------------------------------------- helpers

    def _append_assistant(self, reply: ModelReply) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
        if reply.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(c.arguments if isinstance(c.arguments, dict) else {})}}
                for c in reply.tool_calls
            ]
        self.messages.append(msg)
