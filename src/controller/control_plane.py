"""Control-plane source toggle: where the challenge instance and endpoint come from.

Two platforms, chosen with ``--platform`` and independent of the flag-submitter toggle
(``--flag-mode``), so everything can be set to manual for a fully offline-from-the-platform run,
MCP for the fully automated path, or mixed (MCP endpoint + manual flag surfacing):

  - ``manual`` : operator supplies a local files directory and a remote ``IP:PORT``. No MCP calls:
                 no enumerate, start, get-endpoint, or submit. Built for game-day when MCP is down.
  - ``mcp``    : the controller's HTB MCP client enumerates/starts the instance and returns its
                 endpoint (scaffold; not wired yet).

Both feed the same shared plumbing: a ForwarderTarget that makes the remote reachable as ``target``.
The manual platform supplies the endpoint from the operator's IP:PORT; the MCP platform supplies it
from its start call. The solver side is identical either way.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from controller.forwarder import ForwarderTarget, OfflineTarget
from controller.htb_mcp import HTBNotWired, build_mcp_client
from controller.manual_env import (ManualChallenge, default_description, download_challenge_files,
                                   stage_manual_context)

PLATFORMS = ("manual", "mcp")

# What a control plane hands the attempt: a challenge object, a Target-like endpoint provider, and a
# context provider (workspace -> (context, inventory)) that stages files once the endpoint is up.
Provisioned = Tuple[object, object, Callable[[Path], tuple]]


def parse_target(spec: str) -> Tuple[str, int]:
    """Parse an operator 'IP:PORT' (or 'host:port'). Raises ValueError on a malformed value."""
    spec = spec.strip()
    if spec.count(":") != 1:
        raise ValueError(f"target must be IP:PORT, got {spec!r}")
    host, port = spec.rsplit(":", 1)
    if not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise ValueError(f"target must be IP:PORT with a valid port, got {spec!r}")
    return host, int(port)


def _manual_context_provider(chal, challenge_id, target, description, flag_format):
    def provide(workspace: Path):
        return stage_manual_context(chal, challenge_id, workspace, target.endpoint,
                                    description=description, flag_format=flag_format)
    return provide


class ManualControlPlane:
    platform = "manual"

    def __init__(self, files_dir, target: Optional[str] = None, *, name: Optional[str] = None,
                 category: str = "hackthebox", description: Optional[str] = None,
                 flag_format: str = "HTB{...}"):
        self.files_dir = Path(files_dir)
        if not self.files_dir.is_dir():
            raise ValueError(f"files-dir does not exist: {self.files_dir}")
        self.challenge_id = name or self.files_dir.name
        # No target => a file-only challenge (no live service). With one, a forwarder is used.
        self.remote = parse_target(target) if target else None
        self.category = category
        self.flag_format = flag_format
        self._description = description

    def provision(self, run_id: str, settings: dict) -> Provisioned:
        chal = ManualChallenge(self.challenge_id, self.files_dir, self.category)
        if self.remote is None:                       # file-only: no forwarder, no endpoint
            target = OfflineTarget(run_id, settings)
            endpoint_hint = None
        else:
            target = ForwarderTarget(self.remote[0], self.remote[1], run_id, settings)
            endpoint_hint = {"host": settings["target"]["local_host"], "port": target.listen_port}
        desc = self._description or default_description(endpoint_hint, self.flag_format)
        provider = _manual_context_provider(chal, self.challenge_id, target, desc, self.flag_format)
        return chal, target, provider


class McpControlPlane:
    """HTB MCP-driven control plane. The controller's MCP client starts the instance, reads its
    connection info (host/port) for the forwarder, and downloads the challenge files (controller has
    egress; the solver does not) which are staged the same way the manual backend stages a local dir.
    No MCP enumerate/start is needed in manual mode; this is the automated path."""
    platform = "mcp"

    def __init__(self, challenge_id, client=None, flag_format: str = "HTB{...}"):
        self.challenge_id = challenge_id        # HTB numeric challenge id
        self._client = client
        self.flag_format = flag_format

    def provision(self, run_id: str, settings: dict) -> Provisioned:
        client = self._client or build_mcp_client()
        if client is None:
            raise HTBNotWired(
                "platform 'mcp' needs MCP_TOKEN in .env (controller-side). Run with --platform manual "
                "--files-dir <dir> [--target IP:PORT] for a fully offline run.")
        cid = self.challenge_id
        client.start_instance(cid)                       # start the HTB container
        info = client.connection_info(cid)               # {host, port} from container_status/start
        if not info:
            raise HTBNotWired(
                f"could not read host/port from HTB for challenge {cid}. Check the raw container_status "
                "response field names (parse_connection_info in htb_mcp is defensive on purpose).")
        # Download the challenge files (controller egress) and stage them like a local dir. Best
        # effort: many challenges are service-only (no files — the solver works from the live target),
        # and a download hiccup must not abort an otherwise-solvable attempt. If files exist and the
        # download fails, the solver still has the target; it just won't have the local artifacts.
        files_dir = Path(tempfile.mkdtemp(prefix=f"htb-mcp-{cid}-"))
        url = client.get_download_link(cid)
        if url:
            try:
                download_challenge_files(url, files_dir)
            except Exception as e:  # noqa: BLE001 - controller-side; keep the attempt alive
                print(f"[mcp] challenge {cid}: file download failed ({type(e).__name__}: "
                      f"{str(e)[:120]}); continuing with the live service only")
        chal = ManualChallenge(str(cid), files_dir)
        target = ForwarderTarget(info["host"], int(info["port"]), run_id, settings,
                                 on_stop=lambda: client.stop_container(cid))   # teardown/respawn
        desc = default_description(
            {"host": settings["target"]["local_host"], "port": target.listen_port}, self.flag_format)
        provider = _manual_context_provider(chal, str(cid), target, desc, self.flag_format)
        return chal, target, provider


def build_control_plane(platform: str, *, files_dir=None, target=None, challenge_id=None,
                        name=None, description=None, flag_format="HTB{...}", category="hackthebox",
                        mcp_client=None):
    if platform == "manual":
        if not files_dir:
            raise ValueError("platform 'manual' requires files_dir (and target IP:PORT for a live service)")
        return ManualControlPlane(files_dir, target, name=name, category=category or "hackthebox",
                                  description=description, flag_format=flag_format)
    if platform == "mcp":
        cid = challenge_id or name
        if not cid:
            raise ValueError("platform 'mcp' requires --challenge-id (the HTB numeric challenge id)")
        return McpControlPlane(cid, client=mcp_client, flag_format=flag_format)
    raise ValueError(f"unknown platform {platform!r}; known: {PLATFORMS}")
