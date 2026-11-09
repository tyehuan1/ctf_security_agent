"""Usage accounting from Duke AI Gateway (LiteLLM, OpenAI-compatible) responses.

Tokens come from the response body's `usage` object; cost from the gateway's
`x-litellm-response-cost` header (checked 2026-11-08 on GPT 4.1 Mini). Anything absent is None,
never 0, so the A4 record keeps null for unavailable counters.
"""

from __future__ import annotations

from typing import Mapping, Optional


def _int_or_none(v) -> Optional[int]:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def usage_from_gateway(body: Optional[dict], headers: Optional[Mapping[str, str]] = None) -> dict:
    u = (body or {}).get("usage") or {}
    cost = None
    raw = (headers or {}).get("x-litellm-response-cost")
    if raw not in (None, ""):
        try:
            cost = float(raw)
        except ValueError:
            cost = None
    return {"input_tokens": _int_or_none(u.get("prompt_tokens")),
            "output_tokens": _int_or_none(u.get("completion_tokens")),
            "cost_usd": cost}


NO_USAGE = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
