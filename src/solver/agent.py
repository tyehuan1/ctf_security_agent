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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from solver.llm import GatewayClient, LLMError, ModelReply, ToolCall
from solver.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from solver.sandbox import Sandbox
from tools.runner import ToolRunner
from tools.specs import tool_specs
from common.usage import NO_USAGE

MAX_NO_TOOL_NUDGES = 2
CONTROLLER_MARGIN_S = 5            # finish each turn this far inside the controller's per-call deadline
KEEP_TOOL_RESULTS = 6              # tool results sent verbatim; older ones are elided in the payload
ELIDED_STUB = json.dumps({"note": "earlier output elided to save context; re-run the tool if you need it"})
NO_TOOL_NUDGE = (
    "You replied without calling a tool. You must act by calling one of the tools "
    "(run_shell, inspect_file, python_exec, connect_endpoint) or submit_flag. "
    "Do not reply with prose alone — call a tool now."
)

def _add_usage(total: Optional[dict], u: dict) -> dict:
    """Sum usage across the gateway calls of one turn.

    A counter is None (unknown) if any call left it unknown, matching AttemptBudget's rule that
    unavailable counters stay null rather than being treated as 0.
    """
    if total is None:
        return dict(u)
    out = dict(total)
    for k in ("input_tokens", "output_tokens", "cost_usd"):
        a, b = total.get(k), u.get(k)
        out[k] = None if a is None or b is None else a + b
    a, b = total.get("cached_tokens"), u.get("cached_tokens")
    out["cached_tokens"] = None if a is None or b is None else a + b
    out["attempts"] = (total.get("attempts") or 0) + (u.get("attempts") or 0)
    out["usage_incomplete"] = bool(total.get("usage_incomplete") or u.get("usage_incomplete"))
    out["rate_limit_waits"] = list(total.get("rate_limit_waits") or []) + list(u.get("rate_limit_waits") or [])
    sources = {total.get("cost_source"), u.get("cost_source")}
    out["cost_source"] = sources.pop() if len(sources) == 1 else "mixed"
    return out


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
        self.max_turns = int(settings["budget"]["max_turns"])   # read-only copy, for pacing hints only
        self.turns_used = 0

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
        # After a submit_flag, the controller appended the verification observation. A verified
        # submit ends the loop, so only a failed verification reaches here; hand it back as the
        # tool result.
        if self._awaiting and self._awaiting.name == "submit_flag" and observations:
            last = observations[-1]
            verified = bool(getattr(last, "verified", False) or (isinstance(last, dict) and last.get("verified")))
            self.messages.append({
                "role": "tool", "tool_call_id": self._awaiting.id,
                "content": json.dumps({"verified": verified, **self._pacing()}),
            })
            self._awaiting = None

        self.turns_used += 1
        # One controller turn may take several gateway calls (nudges) and 429 waits. They share
        # one time budget, kept a few seconds under the controller's per-call deadline
        # (model timeout_s) so the agent fails cleanly before the controller cuts it off.
        budget_s = float(self.settings["model"].get("timeout_s", 200)) - CONTROLLER_MARGIN_S
        started = time.monotonic()
        usage_total: Optional[dict] = None
        reply = None
        for nudge in range(MAX_NO_TOOL_NUDGES + 1):
            left = budget_s - (time.monotonic() - started)
            if left < 5:                                     # no time for another call this turn
                break
            n_assistant = len([m for m in self.messages if m["role"] == "assistant"])
            try:
                reply = self.client.chat(self.payload(), tools=self.specs, timeout_s=left,
                                         purpose=f"turn_{self.turns_used}_call_{n_assistant + 1}")
            except LLMError as err:                          # keep waits from earlier calls this turn
                err.waits = (usage_total or {}).get("rate_limit_waits", []) + err.waits
                raise
            self._append_assistant(reply)
            usage_total = _add_usage(usage_total, reply.usage)
            if reply.tool_calls:
                break
            if nudge < MAX_NO_TOOL_NUDGES:
                self.messages.append({"role": "user", "content": NO_TOOL_NUDGE})
        usage = usage_total or dict(NO_USAGE)
        if reply is None or not reply.tool_calls:
            return Action(kind="give_up"), usage

        tc = reply.tool_calls[0]                             # one action per turn (first tool_call wins)
        for extra in reply.tool_calls[1:]:                   # satisfy the API's tool_call accounting
            self.messages.append({"role": "tool", "tool_call_id": extra.id,
                                  "content": json.dumps({"status": "skipped",
                                                         "note": "one tool call per turn; call again if needed"})})

        args = tc.arguments if isinstance(tc.arguments, dict) else {}
        self._awaiting = tc
        if tc.name == "submit_flag":
            return Action(kind="submit_flag", flag=_strip_candidate(args.get("flag")),
                          tool_call_id=tc.id), usage
        return Action(kind="tool", tool=tc.name, arguments=args, tool_call_id=tc.id), usage

    def execute(self, action: Action, timeout_s: float) -> dict:
        assert self.runner is not None, "reset() must run before execute()"
        result = self.runner.execute(action.tool, action.arguments, timeout_s)
        self.messages.append({"role": "tool", "tool_call_id": action.tool_call_id,
                              "content": json.dumps({**result, **self._pacing()}, ensure_ascii=False)})
        self._awaiting = None
        return result

    # ---------------------------------------------------------------- helpers

    def payload(self) -> list[dict]:
        """The message list sent to the gateway: a pruned copy of self.messages.

        Keeps the system prompt, the initial context, every assistant message and every nudge.
        Only the last KEEP_TOOL_RESULTS tool messages keep their content; older ones keep their
        tool_call_id (so every assistant tool_call still has a matching result) with a short stub
        as content. self.messages itself is never modified, so the transcript stays complete.
        """
        tool_idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool"]
        elide = set(tool_idx[:-KEEP_TOOL_RESULTS]) if len(tool_idx) > KEEP_TOOL_RESULTS else set()
        return [dict(m, content=ELIDED_STUB) if i in elide else m for i, m in enumerate(self.messages)]

    def _pacing(self) -> dict:
        """Turns left, shown to the model so it can submit a best guess before the cap.

        Informational only: the controller's AttemptBudget is what actually stops the loop.
        """
        left = max(0, self.max_turns - self.turns_used)
        note = {"turns_remaining": left}
        if left <= 2:
            note["budget_note"] = ("Almost out of turns: if you have any plausible candidate, "
                                   "call submit_flag now.")
        return note

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
