"""HTB control-plane client — trusted controller only.

The MCP client that talks to Hack The Box. It uses the secret MCP token (``.env``: ``MCP_TOKEN``) to
enumerate challenges, start/stop an instance, read its connection info (which the controller points
the target forwarder at), download challenge files, submit flags, and read the scoreboard. The token
is a controller-side secret: it is in ``config.SECRET_KEYS`` (redacted from every trace/export) and
is NEVER passed to, or reachable from, the solver sandbox. The sandbox still reaches only the
forwarded ``target`` and never calls HTB directly.

Transport is streamable HTTP via the official MCP Python SDK (``mcp>=2``:
``mcp.client.streamable_http.streamable_http_client`` + ``ClientSession``). Each client method is one
short-lived ``session.call_tool``. Response shapes are undocumented, so every parser is defensive:
it tries the likely field names and leaves unknown/None for a human rather than guessing.

Tool identifiers were confirmed from a live ``tools/list`` + argument probe (missing-arg validation
errors). The display→identifier mapping lives in ``TOOLS`` so a server rename is a one-line fix.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, Protocol, runtime_checkable

from common import config
from controller.flag_gateway import FlagSubmitter, SubmitResult

ENDPOINT = "https://mcp.hackthebox.ai/v1/ctf/mcp/"

# logical name -> real MCP tool identifier (confirmed from tools/list). Rename = one line here.
TOOLS = {
    "list_events":  "list_ctf_events",       # (no args)
    "retrieve_ctf": "retrieve_ctf",          # ctf_id        -> event detail incl. challenges
    "scores":       "retrieve_ctf_scores",   # ctf_id
    "start":        "start_container",        # challenge_id
    "stop":         "stop_container",         # challenge_id
    "status":       "container_status",       # challenge_id  -> host/port live here
    "submit":       "submit_flag",            # challenge_id, flag
    "download":     "get_download_link",      # challenge_id  -> temporary signed URL
}


class HTBNotWired(RuntimeError):
    """An HTB MCP operation was attempted before the MCP client was wired (no token / no event)."""


@runtime_checkable
class HTBClient(Protocol):
    """The control-plane surface the controller needs from HTB. Implemented by MCPHTBClient."""

    def connection_check(self) -> list: ...
    def enumerate_challenges(self, ctf_id) -> list: ...
    def start_instance(self, challenge_id) -> dict: ...
    def stop_container(self, challenge_id) -> dict: ...
    def container_status(self, challenge_id) -> dict: ...
    def connection_info(self, challenge_id) -> Optional[dict]: ...
    def get_download_link(self, challenge_id) -> Optional[str]: ...
    def submit_flag(self, challenge_id, flag) -> dict: ...
    def scoreboard(self, ctf_id) -> dict: ...


# ---------------------------------------------------------------- defensive response parsers

def _as_obj(raw):
    """A tool response's text content parsed as JSON, or {'_raw': text} when it is not JSON."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"_raw": raw}


def _is_error(obj) -> Optional[str]:
    """Return the error message if the response is an HTB error object, else None."""
    if isinstance(obj, dict) and "error" in obj:
        return f"{obj.get('error')} (status {obj.get('status_code')})"
    return None


def parse_challenges(ctf_detail) -> list:
    """Pull the challenge list out of a retrieve_ctf response, wherever it lives. Defensive."""
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in ("challenges", "challenge") and isinstance(v, list):
                    return v
            for v in o.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(o, list) and o:
            return walk(o[0])
        return None
    return walk(ctf_detail) or []


def parse_connection_info(status) -> Optional[dict]:
    """Extract {host, port} from a start/status response, trying the likely field names.

    Leaves the result None (not a guess) when nothing host/port-shaped is present, so a human can
    read the raw response instead of the solver being pointed somewhere wrong.
    """
    if not isinstance(status, dict):
        return None
    # search nested dicts for host/ip and port fields under common names. HTB's real
    # container_status is {"status": "ready", "hostname": "1.2.3.4", "ports": [33558]} — note the
    # port lives in a LIST under "ports", so the finder must unwrap a list to its first scalar.
    host_keys = ("ip", "host", "hostname", "server", "address", "connection")
    port_keys = ("port", "ports")

    def find(o, keys):
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in keys:
                    if isinstance(v, (str, int)) and not isinstance(v, bool):
                        return v
                    if isinstance(v, (list, tuple)) and v and isinstance(v[0], (str, int)):
                        return v[0]
            for v in o.values():
                got = find(v, keys)
                if got is not None:
                    return got
        return None

    host = find(status, host_keys)
    port = find(status, port_keys)
    # some servers return "ip:port" in one string
    if host and port is None and isinstance(host, str) and ":" in host and host.rsplit(":", 1)[1].isdigit():
        host, port = host.rsplit(":", 1)
    if host is None or port is None:
        return None
    try:
        return {"host": str(host), "port": int(port)}
    except (ValueError, TypeError):
        return None


def parse_download_url(obj) -> Optional[str]:
    """The signed URL from a get_download_link response, trying the likely field names."""
    if isinstance(obj, str) and obj.startswith("http"):
        return obj
    if isinstance(obj, dict):
        for k in ("url", "download_url", "link", "signed_url", "download"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
    return None


def interpret_submit_response(raw) -> SubmitResult:
    """Map HTB's submit_flag response to a SubmitResult. The success/failure field is undocumented,
    so read the common booleans and leave verified=None (captured for a human) when unclear."""
    obj = _as_obj(raw)
    err = _is_error(obj)
    if err:
        # "This flag is already owned/solved by your team" is an error (HTTP 400), but it means the
        # flag is CORRECT and the team already has the points — treat it as accepted, not undetermined.
        if any(w in err.lower() for w in ("already owned", "already solved", "already own")):
            return SubmitResult(verified=True, submitted=True, source="htb-mcp", detail=err)
        return SubmitResult(verified=None, submitted=True, source="htb-mcp", detail=err)
    if not isinstance(obj, dict):
        return SubmitResult(verified=None, submitted=True, source="htb-mcp", detail=f"unexpected: {obj!r}"[:200])
    message = str(obj.get("message") or obj.get("status") or obj.get("detail") or "")
    for key in ("correct", "solved", "success", "is_correct", "isCorrect"):
        if key in obj and isinstance(obj[key], bool):
            return SubmitResult(verified=obj[key], submitted=True, source="htb-mcp", detail=message)
    # some APIs signal correctness only through the message text. Check the negatives FIRST:
    # "incorrect flag" contains the substring "correct flag".
    low = message.lower()
    if any(w in low for w in ("incorrect", "wrong", "invalid flag", "not correct", "already submitted")):
        return SubmitResult(verified=False, submitted=True, source="htb-mcp", detail=message)
    if any(w in low for w in ("correct flag", "congrats", "accepted", "already solved", "solved the")):
        return SubmitResult(verified=True, submitted=True, source="htb-mcp", detail=message)
    return SubmitResult(verified=None, submitted=True, source="htb-mcp",
                        detail=message or "HTB response did not state correctness")


# ---------------------------------------------------------------- the MCP client

def _run_sync(coro):
    """Run an async coroutine from sync controller code, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


class MCPHTBClient:
    """Streamable-HTTP MCP client for HTB. One ``call_tool`` per method, fresh session each call."""

    def __init__(self, token: str, endpoint: str = ENDPOINT):
        self._token = token            # controller-side secret; never logged or sent to the solver
        self.endpoint = endpoint

    # -- transport ------------------------------------------------------------
    async def _acall(self, tool: str, args: dict):
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {self._token}"})
        try:
            async with streamable_http_client(self.endpoint, http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args)
                    text = " ".join(getattr(b, "text", "") for b in (getattr(result, "content", []) or [])
                                    if getattr(b, "text", None))
                    return text
        finally:
            await http_client.aclose()

    async def _alist_tools(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {self._token}"})
        try:
            async with streamable_http_client(self.endpoint, http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return [t.name for t in (await session.list_tools()).tools]
        finally:
            await http_client.aclose()

    def _call(self, logical: str, args: dict):
        return _as_obj(_run_sync(self._acall(TOOLS[logical], args)))

    # -- HTBClient surface ----------------------------------------------------
    def connection_check(self) -> list:
        """Verification #1: initialize + tools/list succeed (proves auth + transport)."""
        return _run_sync(self._alist_tools())

    def list_events(self) -> list:
        return self._call("list_events", {})

    def enumerate_challenges(self, ctf_id) -> list:
        detail = self._call("retrieve_ctf", {"ctf_id": ctf_id})
        err = _is_error(detail)
        if err:
            raise HTBNotWired(f"retrieve_ctf({ctf_id}) failed: {err}. Join the event via the HTB "
                              f"site/join_ctf_event so your MCP token can access it.")
        return parse_challenges(detail)

    def start_instance(self, challenge_id) -> dict:
        return self._call("start", {"challenge_id": challenge_id})

    def stop_container(self, challenge_id) -> dict:
        return self._call("stop", {"challenge_id": challenge_id})

    def container_status(self, challenge_id) -> dict:
        return self._call("status", {"challenge_id": challenge_id})

    def connection_info(self, challenge_id, *, tries: int = 20, delay_s: float = 3.0) -> Optional[dict]:
        """{host, port} from container_status, polling until the container is provisioned.

        A freshly started container is not immediately "ready" (no hostname/ports yet), so poll
        container_status a few times before giving up. Falls back to the start response once.
        """
        import time as _time
        for i in range(max(1, tries)):
            info = parse_connection_info(self.container_status(challenge_id))
            if info is not None:
                return info
            if i < tries - 1:
                _time.sleep(delay_s)
        return parse_connection_info(self.start_instance(challenge_id))

    def get_download_link(self, challenge_id) -> Optional[str]:
        return parse_download_url(self._call("download", {"challenge_id": challenge_id}))

    def submit_flag(self, challenge_id, flag) -> dict:
        return self._call("submit", {"challenge_id": challenge_id, "flag": flag})

    def scoreboard(self, ctf_id) -> dict:
        return self._call("scores", {"ctf_id": ctf_id})


# ---------------------------------------------------------------- auto-submit integration

class HTBMcpSubmitter:
    """FlagSubmitter backed by MCPHTBClient. Wired only when a token (and challenge_id) are present;
    otherwise ``ready()`` is False and the manual flag path is used instead."""

    def __init__(self, challenge_id: Optional[str] = None, client: Optional[HTBClient] = None):
        self.challenge_id = challenge_id
        self._client = client

    def ready(self) -> bool:
        return self._client is not None and self.challenge_id is not None

    def submit(self, flag: str) -> SubmitResult:
        if self._client is None:
            raise HTBNotWired(
                "HTB MCP client is not wired: set MCP_TOKEN in .env to enable auto-submit, or run "
                "with flag mode 'manual' to capture candidates for manual submission.")
        if self.challenge_id is None:
            raise HTBNotWired("auto-submit needs the HTB challenge_id (available in MCP mode).")
        raw = self._client.submit_flag(self.challenge_id, flag)
        return interpret_submit_response(raw)


def build_mcp_client() -> Optional[MCPHTBClient]:
    """A wired MCPHTBClient when MCP_TOKEN is set (controller-side), else None (manual stays offline)."""
    token = config.secret("MCP_TOKEN")
    return MCPHTBClient(token) if token else None


def build_default_submitter(challenge_id: Optional[str] = None) -> FlagSubmitter:
    """The production auto-submit submitter: wired when MCP_TOKEN is present, otherwise unwired so the
    manual flag path still works with no token."""
    return HTBMcpSubmitter(challenge_id=challenge_id, client=build_mcp_client())
