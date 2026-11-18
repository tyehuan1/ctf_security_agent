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


def _challenge_has_no_container(start_response) -> bool:
    """True when start_container reports the challenge has no docker image — i.e. a file-only
    challenge (reversing/crypto/...). Those have no live service: the solver works offline from the
    downloaded files, so we skip the forwarder/endpoint instead of polling for a host/port forever."""
    err = (start_response or {}).get("error", "") if isinstance(start_response, dict) else str(start_response)
    return "docker image" in str(err).lower()


class McpControlPlane:
    """HTB MCP-driven control plane. The controller's MCP client starts the instance, reads its
    connection info (host/port) for the forwarder, and downloads the challenge files (controller has
    egress; the solver does not) which are staged the same way the manual backend stages a local dir.
    A file-only challenge (no docker image) is run offline, with no forwarder and no endpoint."""
    platform = "mcp"

    def __init__(self, challenge_id, client=None, flag_format: str = "HTB{...}",
                 category: str = "hackthebox"):
        self.challenge_id = challenge_id        # HTB numeric challenge id
        self._client = client
        self.flag_format = flag_format
        self.category = category or "hackthebox"

    def provision(self, run_id: str, settings: dict) -> Provisioned:
        client = self._client or build_mcp_client()
        if client is None:
            raise HTBNotWired(
                "platform 'mcp' needs MCP_TOKEN in .env (controller-side). Run with --platform manual "
                "--files-dir <dir> [--target IP:PORT] for a fully offline run.")
        cid = self.challenge_id
        start = client.start_instance(cid)               # start the container (or report: no image)
        # Download the challenge files (controller egress) and stage them like a local dir. The signed
        # URL is fetched cleanly (no MCP bearer header) with retry; a download hiccup must not abort an
        # otherwise-solvable attempt, but a genuinely empty staging is surfaced loudly below.
        files_dir = Path(tempfile.mkdtemp(prefix=f"htb-mcp-{cid}-"))
        url = client.get_download_link(cid)
        download_detail = None
        if url:
            safe_url = url.split("?")[0]                  # log the path, not the signature query
            print(f"[mcp] challenge {cid}: get_download_link -> {safe_url}")
            try:
                staged = download_challenge_files(url, files_dir)
                print(f"[mcp] challenge {cid}: staged {len(staged)} file(s) from the HTB download")
            except Exception as e:  # noqa: BLE001 - controller-side; keep the attempt alive, flag below
                download_detail = f"{type(e).__name__}: {str(e)[:160]}"
                print(f"[mcp] challenge {cid}: download FAILED ({download_detail})")
        else:
            download_detail = "get_download_link returned no URL"
            print(f"[mcp] challenge {cid}: {download_detail}")
        chal = ManualChallenge(str(cid), files_dir, self.category)
        no_container = _challenge_has_no_container(start)
        if not chal.files:
            self._warn_no_files(cid, has_service=not no_container, detail=download_detail)

        if no_container:
            # File-only challenge: no service. Work offline from the downloaded files (no forwarder,
            # no endpoint, no egress check) — exactly the manual file-only path.
            print(f"[mcp] challenge {cid}: no docker image — file-only challenge, running offline")
            target = OfflineTarget(run_id, settings)
            desc = self._description(None)
        else:
            info = client.connection_info(cid)           # polls until the container is ready
            if not info:
                raise HTBNotWired(
                    f"could not read host/port from HTB for challenge {cid}. Check the raw "
                    "container_status response field names (parse_connection_info is defensive).")
            target = ForwarderTarget(info["host"], int(info["port"]), run_id, settings,
                                     on_stop=lambda: client.stop_container(cid))   # teardown/respawn
            desc = self._description({"host": settings["target"]["local_host"], "port": target.listen_port})
        provider = _manual_context_provider(chal, str(cid), target, desc, self.flag_format)
        return chal, target, provider

    def _description(self, endpoint_hint):
        return default_description(endpoint_hint, self.flag_format)

    @staticmethod
    def _warn_no_files(cid, *, has_service: bool, detail) -> None:
        """Loudly flag a challenge that staged ZERO files, so a web/rev/crypto run is never silently
        working blind. Prominent banner in the run output (visible in manual/interactive runs too)."""
        bar = "!" * 70
        print(bar)
        print(f"!!  MCP challenge {cid}: staged ZERO files (workspace files: [])")
        if detail:
            print(f"!!  download: {detail}")
        if has_service:
            print("!!  The challenge has a live service, so it MAY be service-only (no source files).")
            print("!!  But if it ships source (web/rev/crypto), THE SOLVER IS WORKING BLIND.")
        else:
            print("!!  The challenge has NO service AND no files — the solver has nothing to work from.")
        print("!!  To stage files by hand: download them, then run")
        print(f"!!    python run_htb.py --platform manual --files-dir <dir> [--target IP:PORT] --name {cid}")
        print(bar)


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
        return McpControlPlane(cid, client=mcp_client, flag_format=flag_format,
                               category=category or "hackthebox")
    raise ValueError(f"unknown platform {platform!r}; known: {PLATFORMS}")
