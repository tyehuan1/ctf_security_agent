"""Solver sandbox: a sidecar container the solver's tools execute inside.

Attached only to the attempt's internal `ctfeval` network, so the only host it can reach is
`target` (when the challenge has a service). No bind mounts except the workspace, no capabilities,
no new privileges, read-only root filesystem with a small /tmp. Never gets .env or LITELLM_TOKEN.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_IMAGE = "ctf-solver-sandbox:v1.1"   # imaging+OCR arm (agent_version 1.1.0); baseline was :v1


class SandboxError(RuntimeError):
    """The sandbox could not be started, executed in, or stopped."""


def _run(args: list[str], timeout: float, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as err:
        raise SandboxError(f"timed out after {timeout}s: {' '.join(args[:6])}") from err


def image_exists(image: str) -> bool:
    res = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image], 20)
    return res.returncode == 0


@dataclass
class ExecResult:
    status: str                 # "ok" | "error" | "timeout"
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None


class Sandbox:
    def __init__(self, network: str, workspace: Path, run_id: str, image: str = DEFAULT_IMAGE,
                 memory: str = "1g", cpus: str = "1", tmpfs_size: str = "256m"):
        self.network = network
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.image = image
        self.memory, self.cpus, self.tmpfs_size = memory, cpus, tmpfs_size
        self.name = f"ctfsolver-{run_id}-{uuid.uuid4().hex[:6]}".lower()
        self.container_id: Optional[str] = None

    def start(self) -> None:
        if not image_exists(self.image):
            raise SandboxError(
                f"image {self.image!r} not built; run `docker build -t {self.image} sandbox/` first")
        # Windows path -> Docker bind mount translation handled by Docker Desktop.
        ws = str(self.workspace).replace("\\", "/")
        res = _run([
            "docker", "run", "-d", "--rm",
            "--name", self.name,
            "--network", self.network,
            "--network-alias", "solver",
            "--hostname", "solver",
            "-v", f"{ws}:/workspace:rw",
            "--workdir", "/workspace",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={self.tmpfs_size},exec",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", "256",
            self.image, "sleep", "infinity",
        ], 90)
        if res.returncode != 0:
            raise SandboxError(f"docker run failed: {res.stderr.strip()[-500:]}")
        self.container_id = res.stdout.strip()
        # Brief readiness poll.
        for _ in range(20):
            if _run(["docker", "exec", self.container_id, "true"], 10).returncode == 0:
                return
            time.sleep(0.1)
        raise SandboxError("sandbox did not become ready in 2s")

    def exec(self, argv: list[str], timeout_s: float, stdin: Optional[str] = None) -> ExecResult:
        """Run argv inside the sandbox under `timeout` so the in-container process is killed.

        The host-side subprocess is bounded by timeout_s + 5s as a backstop.
        """
        if not self.container_id:
            raise SandboxError("sandbox not started")
        cmd = ["docker", "exec", "-i", self.container_id,
               "timeout", "-k", "2", f"{int(timeout_s)}", *argv]
        try:
            proc = _run(cmd, timeout_s + 5, stdin=stdin)
        except SandboxError as err:
            self._kill_processes()           # backstop: the host client gave up
            return ExecResult("timeout", stderr=str(err))
        if proc.returncode == 124:           # coreutils timeout -> process killed
            return ExecResult("timeout", stdout=proc.stdout, stderr=proc.stderr or "process killed by timeout",
                              exit_code=124)
        status = "ok" if proc.returncode == 0 else "error"
        return ExecResult(status, stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)

    def _kill_processes(self) -> None:
        if self.container_id:
            _run(["docker", "exec", self.container_id, "pkill", "-KILL", "-u", "solver"], 15)

    def stop(self) -> dict:
        report = {"name": self.name, "stopped": False, "forced": False}
        if not self.container_id:
            return report
        res = _run(["docker", "stop", "-t", "3", self.container_id], 30)
        report["stopped"] = res.returncode == 0
        leftover = _run(["docker", "ps", "-aq", "--filter", f"id={self.container_id}"], 15).stdout.strip()
        if leftover:
            _run(["docker", "rm", "-f", "-v", self.container_id], 30)
            report["forced"] = True
        report["gone"] = not _run(["docker", "ps", "-aq", "--filter", f"id={self.container_id}"], 15).stdout.strip()
        return report
