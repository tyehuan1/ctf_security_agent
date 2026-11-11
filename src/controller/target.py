"""Challenge target lifecycle (controller only): start, stop, reset, and isolation inspection.

Service challenges run from the challenge's own docker-compose.yml, but never as-is. Upstream files
mount /var/run/docker.sock, set privileged, publish host ports, and join the plain `ctfnet` bridge
(Appendix A3: "Those defaults do not implement this project's controls"). sanitize_compose()
renders a per-attempt copy with:
  - no volumes / bind mounts (so no docker.sock), no privileged, cap_add, devices, pid/ipc/userns,
    network_mode, extra_hosts, published ports, or container_name;
  - one per-attempt `internal: true` network on which the target answers to settings.target.local_host.

Known conflict: msc-showdown spawns per-connection containers through the host docker.sock
(docker-on-docker). With the socket stripped it may not work; record infrastructure_error and ask
the instructor for the reviewed configuration rather than re-enabling host Docker access.

File-only challenges have no service: start() returns None and stop() is a no-op.

Every attempt gets a new compose project and network, `down --volumes`, and a teardown check that
no project container or network survives. A host-side client timeout does not prove the
in-container process stopped, so stop() force-removes anything left.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from nyuctf.challenge import CTFChallenge

STRIP_SERVICE_KEYS = ("privileged", "cap_add", "devices", "pid", "ipc", "userns_mode", "network_mode",
                      "extra_hosts", "ports", "volumes", "volumes_from", "container_name", "links",
                      "external_links", "dns", "sysctls")


class TargetError(RuntimeError):
    """The target could not be started, inspected, or torn down (infrastructure_error)."""


def _run(args: list[str], timeout: float, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as err:
        raise TargetError(f"timed out after {timeout}s: {' '.join(map(str, args[:6]))}") from err


def project_name(prefix: str, run_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", f"{prefix}-{run_id}".lower())


def sanitize_compose(cfg: dict, network_name: str, local_host: str) -> tuple[dict, list[str]]:
    """Return (sanitized compose config, list of removed settings). Pure; unit-tested."""
    removed: list[str] = []
    services = {}
    for name, svc in cfg.get("services", {}).items():
        svc = dict(svc)
        for key in STRIP_SERVICE_KEYS:
            if key in svc:
                removed.append(f"{name}.{key}={json.dumps(svc.pop(key), default=str)[:200]}")
        had_alias = any((n or {}).get("aliases") for n in (svc.get("networks") or {}).values()
                        if isinstance(n, dict))
        svc["networks"] = {"ctfeval": {"aliases": [local_host]} if had_alias else {}}
        services[name] = svc
    if len(services) == 1:  # a single-service target always answers to the local alias
        only = next(iter(services.values()))
        only["networks"]["ctfeval"] = {"aliases": [local_host]}
    for key in ("volumes", "configs", "secrets"):
        if cfg.get(key):
            removed.append(f"top-level {key}")
    out = {"services": services, "networks": {"ctfeval": {"name": network_name, "internal": True}}}
    return out, removed


class Target:
    def __init__(self, chal: CTFChallenge, run_id: str, settings: dict, run_dir: Path):
        t = settings["target"]
        self.chal = chal
        self.run_id = run_id
        self.local_host = t["local_host"]
        self.timeout = float(t["compose_timeout_s"])
        self.stop_timeout = int(t["stop_timeout_s"])
        self.project = project_name(t["network_prefix"], run_id)
        self.network = self.project + "-net"
        self.dir = Path(run_dir) / "target"
        self.compose_file = self.dir / "compose.json"
        self.removed: list[str] = []
        self.reset_id = f"fresh-{run_id}-{uuid.uuid4().hex[:8]}"

    @property
    def upstream_compose(self) -> Path:
        return self.chal.challenge_dir / "docker-compose.yml"

    @property
    def has_service(self) -> bool:
        return bool(self.chal.container) and self.upstream_compose.exists()

    def render(self) -> dict:
        """Merged upstream config (docker compose config) -> sanitized per-attempt compose file."""
        res = _run(["docker", "compose", "-f", str(self.upstream_compose), "config", "--format", "json"],
                   self.timeout, cwd=self.chal.challenge_dir)
        if res.returncode != 0:
            raise TargetError(f"docker compose config failed: {res.stderr.strip()[:500]}")
        cfg, self.removed = sanitize_compose(json.loads(res.stdout), self.network, self.local_host)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.compose_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8", newline="\n")
        return cfg

    def _compose(self, *args: str) -> subprocess.CompletedProcess:
        return _run(["docker", "compose", "-p", self.project, "-f", str(self.compose_file), *args], self.timeout)

    def ensure_network(self) -> str:
        """Create the per-attempt internal network if absent. Idempotent."""
        existing = _run(["docker", "network", "ls", "-q", "--filter", f"name=^{self.network}$"], 30).stdout.strip()
        if not existing:
            res = _run(["docker", "network", "create", "--internal", self.network], 60)
            if res.returncode != 0:
                raise TargetError(f"docker network create failed: {res.stderr.strip()[:300]}")
        return self.network

    def start(self) -> Optional[dict]:
        """Fresh target for this attempt. Returns the local endpoint, or None for file-only challenges.

        Always creates the per-attempt internal network so the solver sandbox can attach even for
        file-only challenges (sandbox sees no hosts in that case — which is correct).
        """
        self.ensure_network()
        if not self.has_service:
            return None
        self.render()
        res = self._compose("up", "-d", "--force-recreate", "--remove-orphans", "--renew-anon-volumes")
        if res.returncode != 0:
            self.stop()
            raise TargetError(f"docker compose up failed: {res.stderr.strip()[-800:]}")
        return {"host": self.local_host, "port": int(self.chal.port), "network": self.network}

    def container_ids(self) -> list[str]:
        res = _run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"], 30)
        return res.stdout.split()

    def _network_exists(self) -> bool:
        return bool(_run(["docker", "network", "ls", "-q", "--filter", f"name=^{self.network}$"], 30).stdout.split())

    def stop(self) -> dict:
        """Tear down and verify nothing from this attempt remains."""
        report = {"project": self.project, "down_rc": None, "forced_containers": [], "network_removed": None}
        if self.has_service:
            if self.compose_file.exists():
                try:
                    report["down_rc"] = self._compose("down", "--volumes", "--remove-orphans",
                                                      "--timeout", str(self.stop_timeout)).returncode
                except TargetError as err:
                    report["down_error"] = str(err)
            leftovers = self.container_ids()
            if leftovers:
                _run(["docker", "rm", "-f", "-v", *leftovers], 60)
                report["forced_containers"] = leftovers
        # The per-attempt network exists for file-only challenges too (ensure_network); the solver
        # sandbox must already be stopped, or Docker refuses to remove a network with endpoints.
        if self._network_exists():
            _run(["docker", "network", "rm", self.network], 30)
        report["network_removed"] = not self._network_exists()
        report["clean"] = not self.container_ids() and report["network_removed"]
        if not report["clean"]:
            raise TargetError(f"containers or network {self.network} survived teardown for {self.project}")
        return report

    def inspect_isolation(self) -> dict:
        """Check the running target: privileges, capabilities, mounts, published ports, networks."""
        violations, containers = [], []
        for cid in self.container_ids():
            info = json.loads(_run(["docker", "inspect", cid], 30).stdout)[0]
            hc = info.get("HostConfig", {})
            name = info.get("Name", cid).lstrip("/")
            mounts = [m.get("Source") for m in info.get("Mounts", [])]
            nets = list((info.get("NetworkSettings") or {}).get("Networks", {}))
            containers.append({"name": name, "privileged": hc.get("Privileged"), "cap_add": hc.get("CapAdd"),
                               "port_bindings": hc.get("PortBindings"), "mounts": mounts, "networks": nets})
            if hc.get("Privileged"):
                violations.append(f"{name}: privileged")
            if hc.get("CapAdd"):
                violations.append(f"{name}: cap_add {hc['CapAdd']}")
            if hc.get("PortBindings"):
                violations.append(f"{name}: published ports {hc['PortBindings']}")
            if any(m.get("Type") == "bind" for m in info.get("Mounts", [])):
                violations.append(f"{name}: bind mounts {mounts}")
            if any("docker.sock" in (m or "") for m in mounts):
                violations.append(f"{name}: docker.sock exposed")
            for n in nets:
                ninfo = json.loads(_run(["docker", "network", "inspect", n], 30).stdout or "[{}]")[0]
                if not ninfo.get("Internal"):
                    violations.append(f"{name}: network {n} is not internal")
        return {"ok": not violations, "violations": violations, "containers": containers}
