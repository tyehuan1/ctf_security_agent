"""HTB control-plane client (scaffold) — trusted controller only.

The MCP client that talks to Hack The Box lives here, in the trusted controller. It will use the
secret MCP token (``.env``: ``MCP_TOKEN``) to enumerate challenges, start an instance, read its
connection info (which the controller then points the target forwarder at), submit flags, and watch
the scoreboard. The token is a controller-side secret: it is redacted from traces (it is in
``config.SECRET_KEYS``) and is NEVER passed to, or reachable from, the solver sandbox. The sandbox
still reaches only the forwarded target endpoint and never calls HTB directly.

This module is a deliberate SCAFFOLD. The transport and the exact HTB MCP tool names/response shapes
are not wired yet; every network boundary raises ``HTBNotWired`` with a clear message instead of
guessing a protocol. Wiring later is a localized change: implement ``MCPHTBClient`` against the real
server and have ``build_default_submitter`` return a submitter backed by it.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from controller.flag_gateway import FlagSubmitter, SubmitResult


class HTBNotWired(RuntimeError):
    """An HTB MCP operation was attempted before the MCP client was wired to a real server."""


@runtime_checkable
class HTBClient(Protocol):
    """The control-plane surface the controller needs from the HTB MCP server. One method per MCP
    tool. Implement this against the real server to wire the control plane; names map to the HTB MCP
    tools (confirm the exact tool names when wiring)."""

    def enumerate_challenges(self) -> list[dict]:
        ...

    def start_instance(self, challenge_id: str) -> dict:
        """Start the challenge instance; return its connection info (host/port) for the forwarder."""
        ...

    def connection_info(self, challenge_id: str) -> dict:
        ...

    def submit_flag(self, challenge_id: str, flag: str) -> dict:
        """Submit a flag; return HTB's raw scoring response."""
        ...

    def scoreboard(self) -> dict:
        ...


def interpret_submit_response(raw: dict) -> SubmitResult:
    """Map HTB's raw submit response to a SubmitResult.

    The exact HTB MCP response shape must be confirmed when wiring; this reads the common fields
    (``correct``/``solved``/``success`` and a ``message``) and leaves ``verified=None`` when the
    response does not clearly say, so an unclear verdict is captured for a human rather than guessed.
    """
    if not isinstance(raw, dict):
        return SubmitResult(verified=None, submitted=True, source="htb-mcp", detail=f"unexpected response: {raw!r}")
    message = str(raw.get("message") or raw.get("status") or "")
    for key in ("correct", "solved", "success", "is_correct"):
        if key in raw and isinstance(raw[key], bool):
            return SubmitResult(verified=raw[key], submitted=True, source="htb-mcp", detail=message)
    return SubmitResult(verified=None, submitted=True, source="htb-mcp",
                        detail=message or "HTB response did not state correctness")


class HTBMcpSubmitter:
    """FlagSubmitter backed by the controller's HTB MCP client. The single auto-submit integration
    point: ``submit`` performs the one real MCP call once a client is supplied. Until then it is not
    ``ready()`` and raises ``HTBNotWired`` so the auto path fails loudly instead of fabricating a result."""

    def __init__(self, challenge_id: Optional[str] = None, client: Optional[HTBClient] = None):
        self.challenge_id = challenge_id
        self._client = client

    def ready(self) -> bool:
        return self._client is not None

    def submit(self, flag: str) -> SubmitResult:
        if self._client is None:
            raise HTBNotWired(
                "HTB MCP client is not wired yet: provide the MCP server endpoint, transport, and the "
                "submit tool name to enable auto-submit. Run with flag mode 'manual' meanwhile.")
        raw = self._client.submit_flag(self.challenge_id, flag)   # the real MCP call, once wired
        return interpret_submit_response(raw)


def build_default_submitter(challenge_id: Optional[str] = None) -> FlagSubmitter:
    """The production auto-submit submitter. Returns an unwired HTBMcpSubmitter until the MCP client
    is implemented; wiring = construct an MCPHTBClient here and pass it in."""
    return HTBMcpSubmitter(challenge_id=challenge_id, client=None)
