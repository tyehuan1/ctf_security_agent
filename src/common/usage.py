"""Usage accounting from Duke AI Gateway (LiteLLM, OpenAI-compatible) responses.

Tokens come from the response body's `usage` object. Cost comes from the gateway's
`x-litellm-response-cost` header when present; some responses omit it, so cost then falls back
to the published per-token rates in settings.model.pricing (cached prompt tokens priced at the
cached rate, from usage.prompt_tokens_details.cached_tokens). Checked 2026-11-09: the rate formula
reproduces the header exactly when cached_tokens is reported.

cost_source records which path produced the number and lives in the trace only, never in the
A4 result object:
  "header"          gateway-reported cost
  "rate"            computed from tokens at published rates, cached tokens known
  "rate_estimate"   computed, but cached_tokens missing, so all prompt tokens priced uncached
  None              cost unknown (tokens unknown or no pricing configured)
Anything absent is None, never 0, so the A4 record keeps null for unavailable counters.
"""

from __future__ import annotations

from typing import Mapping, Optional


def _int_or_none(v) -> Optional[int]:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def cost_from_rates(input_tokens: Optional[int], output_tokens: Optional[int],
                    cached_tokens: Optional[int], pricing: Optional[dict]) -> tuple[Optional[float], Optional[str]]:
    """(cost_usd, cost_source) from published per-million rates, or (None, None) if not computable."""
    if not pricing or input_tokens is None or output_tokens is None:
        return None, None
    cached = cached_tokens if cached_tokens is not None else 0
    cached = min(max(cached, 0), input_tokens)
    cost = ((input_tokens - cached) * float(pricing["input_per_m"])
            + cached * float(pricing.get("cached_input_per_m", pricing["input_per_m"]))
            + output_tokens * float(pricing["output_per_m"])) / 1_000_000
    return cost, ("rate" if cached_tokens is not None else "rate_estimate")


def usage_from_gateway(body: Optional[dict], headers: Optional[Mapping[str, str]] = None,
                       pricing: Optional[dict] = None) -> dict:
    u = (body or {}).get("usage") or {}
    inp = _int_or_none(u.get("prompt_tokens"))
    out = _int_or_none(u.get("completion_tokens"))
    cached = _int_or_none((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
    cost, source = None, None
    raw = (headers or {}).get("x-litellm-response-cost")
    if raw not in (None, ""):
        try:
            cost, source = float(raw), "header"
        except ValueError:
            cost = None
    if cost is None:
        cost, source = cost_from_rates(inp, out, cached, pricing)
    return {"input_tokens": inp, "output_tokens": out, "cost_usd": cost,
            "cached_tokens": cached, "cost_source": source}


NO_USAGE = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
