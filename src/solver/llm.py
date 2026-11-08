"""Model client for the Duke AI Gateway (LiteLLM, OpenAI-compatible /chat/completions). PHASE 1 STUB.

Shape harvested from the triage agent's llm.py: ToolCall / ModelReply dataclasses, tool requests
taken only from the structured tool_calls field, retry with NOT_RETRYABLE codes, and a per-call
JSONL transcript. Usage (prompt/completion tokens) must be returned so the controller can account
for it; missing counters stay None. LITELLM_TOKEN is read from the controller's secret env only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

NOT_RETRYABLE = {400, 401, 403, 404, 422}


class LLMError(RuntimeError):
    """The model service could not produce a reply (after retry)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: object             # dict normally; raw string if the model sent unparseable JSON
    id_generated: bool = False


@dataclass
class ModelReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": None, "output_tokens": None, "cost_usd": None})
    raw: dict = field(default_factory=dict)
    latency_ms: int = 0


class GatewayClient:
    def __init__(self, settings: dict, transcript_path=None):
        self.settings = settings

    def chat(self, messages: list[dict], *, tools: Optional[list[dict]] = None) -> ModelReply:
        raise NotImplementedError("Phase 1: implement after list_gateway_models.py confirms a tool-capable model id")
