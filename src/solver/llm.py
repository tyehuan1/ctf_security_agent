"""Duke AI Gateway client (OpenAI-compatible /v1/chat/completions) with native tool calling.

Returns a ModelReply the Agent translates into an Action + usage. Tokens come from the response
`usage` object; cost from the gateway's `x-litellm-response-cost` header (both parsed by
common.usage.usage_from_gateway). Every call is appended to the per-attempt transcript.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from common import config
from common.usage import NO_USAGE, usage_from_gateway

NOT_RETRYABLE = {400, 401, 403, 404, 422}


class LLMError(RuntimeError):
    """The model service could not produce a reply (after retry). .waits lists any 429 waits."""

    def __init__(self, message: str, waits: Optional[list] = None):
        super().__init__(message)
        self.waits = list(waits or [])


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: object                   # dict normally; raw string if the model sent unparseable JSON
    id_generated: bool = False


@dataclass
class ModelReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: dict(NO_USAGE))
    raw: dict = field(default_factory=dict)
    finish_reason: Optional[str] = None
    latency_ms: int = 0


class GatewayClient:
    def __init__(self, settings: dict, transcript_path: Optional[Path] = None):
        self.settings = settings
        self.model = settings["model"]
        self.transcript = Path(transcript_path) if transcript_path else None
        self.calls = 0

    @property
    def url(self) -> str:
        return self.model["base_url"].rstrip("/") + "/v1/chat/completions"

    def _token(self) -> str:
        key = os.getenv("LITELLM_TOKEN") or config.secret("LITELLM_TOKEN")
        if not key:
            raise LLMError("LITELLM_TOKEN not set")
        return key

    def chat(self, messages: list[dict], *, tools: Optional[list[dict]] = None,
             timeout_s: Optional[float] = None, purpose: str = "") -> ModelReply:
        """One chat completion that must finish within timeout_s seconds (all tries and waits).

        Ordinary failures: up to retry.attempts tries, retry.backoff_s apart.
        HTTP 429 (the gateway's per-key TPM/RPM limit): wait Retry-After, or
        rate_limit.default_wait_s when absent, then try again, at most rate_limit.max_429_retries
        times, and only when the wait still fits the remaining time. Each wait is returned in
        usage["rate_limit_waits"] (or on LLMError.waits) for the controller to trace. A 429 that
        survives every retry raises LLMError, which the controller records as infrastructure_error.
        """
        budget = float(timeout_s or self.model.get("timeout_s", 200))
        body = {"model": self.model["name"], "messages": messages,
                "temperature": self.model.get("temperature", 0)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        attempts = int(self.settings.get("retry", {}).get("attempts", 2))
        backoff = float(self.settings.get("retry", {}).get("backoff_s", 2))
        rl = self.settings.get("rate_limit", {})
        max_429 = int(rl.get("max_429_retries", 3))
        default_wait = float(rl.get("default_wait_s", 65))
        pricing = self.model.get("pricing")
        request_timeout = float(self.model.get("request_timeout_s", budget))
        headers = {"Authorization": f"Bearer {self._token()}"}
        last_err = None
        started = time.monotonic()
        deadline = started + budget
        # Retries count toward the attempt's usage (Appendix A6). A failed try adds whatever usage
        # its body reports; a timeout or 5xx may have consumed tokens the gateway never reported,
        # so it marks the call's usage as incomplete (traced; totals are then known subtotals).
        # A 429 is rejected by the gateway before the model runs, so it consumes no tokens.
        failed_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        incomplete = False
        tries = failures = rate_retries = 0
        waits: list[dict] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                last_err = (last_err or "no time left") + f" (call budget of {budget:.0f}s used up)"
                break
            tries += 1
            try:
                # One hung request may not use up the whole turn: cap each HTTP request so a retry
                # still fits inside the turn's budget.
                r = requests.post(self.url, headers=headers, json=body, timeout=min(remaining, request_timeout))
                if r.status_code < 400:
                    data = r.json()
                    latency = int((time.monotonic() - started) * 1000)
                    reply = self._parse(data, r.headers, latency)
                    reply.usage = self._merge_failed(reply.usage, failed_usage, attempts=tries, incomplete=incomplete)
                    reply.usage["rate_limit_waits"] = waits
                    self._log({"call_index": self.calls, "purpose": purpose, "request": body,
                               "response": data, "cost_header": r.headers.get("x-litellm-response-cost"),
                               "latency_ms": latency, "attempt": tries, "usage": reply.usage})
                    return reply
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code == 429:
                    wait, wait_source = self._retry_after(r.headers), "retry-after"
                    if wait is None:
                        wait, wait_source = default_wait, "default"
                    if rate_retries >= max_429:
                        last_err += f" (rate limited; {max_429} waits already spent)"
                        break
                    if time.monotonic() + wait + 5 > deadline:
                        last_err += (f" (rate limited; a {wait:.0f}s wait does not fit the remaining "
                                     f"{deadline - time.monotonic():.0f}s)")
                        break
                    rate_retries += 1
                    waits.append({"purpose": purpose, "wait_s": wait, "wait_source": wait_source,
                                  "retry": rate_retries, "detail": r.text[:200]})
                    time.sleep(wait)
                    continue
                try:
                    fu = usage_from_gateway(r.json(), r.headers, pricing)
                except ValueError:
                    fu = dict(NO_USAGE)
                for k in failed_usage:
                    if fu.get(k) is not None:
                        failed_usage[k] += fu[k]
                if r.status_code >= 500:
                    incomplete = True
                failures += 1
                if r.status_code in NOT_RETRYABLE:
                    break
            except requests.RequestException as err:
                last_err = f"{type(err).__name__}: {err}"
                if isinstance(err, (requests.Timeout, requests.ConnectionError)):
                    incomplete = True
                failures += 1
            if failures >= attempts:
                break
            time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))
        self._log({"purpose": purpose, "request": body, "error": last_err, "tries": tries,
                   "rate_limit_waits": waits})
        raise LLMError(f"gateway call '{purpose or 'chat'}' failed after {tries} tries: {last_err}", waits=waits)

    @staticmethod
    def _retry_after(headers) -> Optional[float]:
        """Seconds from a numeric Retry-After header; None when absent, zero, or not a number."""
        raw = (headers or {}).get("retry-after")
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    @staticmethod
    def _merge_failed(usage: dict, failed: dict, *, attempts: int, incomplete: bool) -> dict:
        merged = dict(usage)
        for k, extra in failed.items():
            if extra and merged.get(k) is not None:
                merged[k] = merged[k] + extra
        merged["attempts"] = attempts
        merged["usage_incomplete"] = incomplete
        return merged

    def _parse(self, data: dict, headers, latency_ms: int) -> ModelReply:
        self.calls += 1
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    pass                    # keep raw; the tool runner reports it as invalid
            cid = tc.get("id")
            calls.append(ToolCall(id=cid or f"gen_{self.calls}_{i}",
                                  name=fn.get("name", ""), arguments=args, id_generated=not cid))
        return ModelReply(content=msg.get("content") or "", tool_calls=calls,
                          usage=usage_from_gateway(data, headers, self.model.get("pricing")), raw=data,
                          finish_reason=choice.get("finish_reason"), latency_ms=latency_ms)

    def _log(self, entry: dict) -> None:
        if not self.transcript:
            return
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        with self.transcript.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")


def verify_one_tool_call(settings: Optional[dict] = None) -> dict:
    """Smoke test: one native tool-call cycle through the gateway (harmless echo tool).

    Fails if the listed model does not actually support native tool calling or quota is exhausted.
    """
    s = settings or config.load_settings()
    client = GatewayClient(s)
    tool = {"type": "function", "function": {"name": "echo",
            "description": "Echo back the given text.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"], "additionalProperties": False}}}
    msgs = [{"role": "user",
             "content": "Call the echo tool with text 'ping'. Then, after seeing the result, reply with just the tool's output."}]
    r1 = client.chat(msgs, tools=[tool], purpose="smoke-test-1")
    if not r1.tool_calls:
        raise LLMError(f"model returned no native tool_calls; finish_reason={r1.finish_reason}")
    tc = r1.tool_calls[0]
    args = tc.arguments if isinstance(tc.arguments, dict) else {"text": ""}
    msgs += [
        {"role": "assistant", "content": r1.content or "",
         "tool_calls": [{"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": tc.id, "content": args.get("text", "")},
    ]
    r2 = client.chat(msgs, tools=[tool], purpose="smoke-test-2")
    return {"tool_called": tc.name, "tool_args": args, "final_content": r2.content,
            "finish_reason": r2.finish_reason, "usage_turn1": r1.usage, "usage_turn2": r2.usage}
