"""Target forwarder: the shared plumbing that lets the air-gapped solver reach a remote challenge.

Both control-plane modes feed this. The MCP platform supplies the remote endpoint from its start
call; the manual platform supplies it from an operator ``IP:PORT`` parameter. Either way the result
is identical from the solver's side: a container aliased ``target`` on the per-attempt internal
network, so the solver's network path and its five tools are byte-identical to a local CTFTiny
service. Nothing in the sandbox changes.

The forwarder is multi-homed: the internal network (how the solver reaches it, no egress) and a
per-attempt egress network (how it reaches the real challenge). It is the ONLY hop with egress; the
solver sandbox is on the internal network only, so general outbound from the sandbox still fails and
only ``target`` connects. The forwarder runs a tiny stdlib TCP proxy in the existing sandbox image
(no extra packages, no new image), drops all capabilities, publishes no ports, and mounts nothing.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from controller.target import TargetError, _run, project_name

PROXY_SRC = (Path(__file__).with_name("_forwarder_proxy.py")).read_text(encoding="utf-8")
DEFAULT_FORWARDER_IMAGE = "ctf-solver-sandbox:v1"   # has python3 + netstat; always built


class OfflineTarget:
    """A Target-compatible object for a file-only challenge (no live service, no IP:PORT).

    It owns only the per-attempt internal network so the solver sandbox can attach and stay
    air-gapped; it starts no container and exposes no endpoint. The solver gets the staged files and
    nothing to connect to, which is correct for offline crypto/rev/forensics challenges.
    """

    has_service = False
    removed: list = []
    compose_file = None

    def __init__(self, run_id: str, settings: dict):
        t = settings["target"]
        self.local_host = t["local_host"]
        self.project = project_name(t["network_prefix"], run_id)
        self.network = self.project + "-net"
        self.reset_id = f"fresh-{run_id}-{uuid.uuid4().hex[:8]}"
        self.endpoint = None

    def _network_exists(self, name: str) -> bool:
        return bool(_run(["docker", "network", "ls", "-q", "--filter", f"name=^{name}$"], 30).stdout.split())

    def start(self) -> None:
        if not self._network_exists(self.network):
            res = _run(["docker", "network", "create", "--internal", self.network], 60)
            if res.returncode != 0:
                raise TargetError(f"internal network create failed: {res.stderr.strip()[:300]}")
        return None

    def container_ids(self) -> list:
        return []

    def inspect_isolation(self) -> dict:
        return {"ok": True, "violations": [], "containers": []}

    def stop(self) -> dict:
        report = {"project": self.project, "down_rc": 0, "forced_containers": []}
        if self._network_exists(self.network):
            _run(["docker", "network", "rm", self.network], 30)
        report["network_removed"] = not self._network_exists(self.network)
        report["clean"] = report["network_removed"]
        if not report["clean"]:
            raise TargetError(f"network {self.network} survived teardown for {self.project}")
        return report


class ForwarderTarget:
    """A Target-compatible object backed by a TCP forwarder to a remote ``host:port``.

    Exposes the same surface the attempt loop uses on a CTFTiny Target: start()/stop(),
    inspect_isolation(), reset_id, has_service, network, removed, compose_file, container_ids().
    """

    has_service = True
    removed: list = []
    compose_file = None

    def __init__(self, remote_host: str, remote_port: int, run_id: str, settings: dict,
                 image: Optional[str] = None, on_stop=None):
        t = settings["target"]
        self.on_stop = on_stop         # optional callback at teardown (e.g. stop the HTB container)
        self.remote_host = remote_host
        self.remote_port = int(remote_port)
        self.run_id = run_id
        self.local_host = t["local_host"]
        self.timeout = float(t.get("compose_timeout_s", 120))
        self.stop_timeout = int(t.get("stop_timeout_s", 10))
        self.image = image or t.get("forwarder_image") or DEFAULT_FORWARDER_IMAGE
        self.project = project_name(t["network_prefix"], run_id)
        self.network = self.project + "-net"          # internal: solver <-> forwarder
        self.egress_network = self.project + "-egress"  # non-internal: forwarder <-> remote
        self.name = self.project + "-fwd"
        # Non-root in the sandbox image cannot bind <1024; map low remote ports to a high listen port.
        self.listen_port = self.remote_port if self.remote_port >= 1024 else 1337
        self.reset_id = f"fresh-{run_id}-{uuid.uuid4().hex[:8]}"
        self.container_id: Optional[str] = None
        self.endpoint: Optional[dict] = None

    # -- networks -----------------------------------------------------------------
    def _network_exists(self, name: str) -> bool:
        return bool(_run(["docker", "network", "ls", "-q", "--filter", f"name=^{name}$"], 30).stdout.split())

    def ensure_network(self) -> str:
        if not self._network_exists(self.network):
            res = _run(["docker", "network", "create", "--internal", self.network], 60)
            if res.returncode != 0:
                raise TargetError(f"internal network create failed: {res.stderr.strip()[:300]}")
        return self.network

    def ensure_egress_network(self) -> str:
        if not self._network_exists(self.egress_network):
            res = _run(["docker", "network", "create", self.egress_network], 60)
            if res.returncode != 0:
                raise TargetError(f"egress network create failed: {res.stderr.strip()[:300]}")
        return self.egress_network

    # -- lifecycle ----------------------------------------------------------------
    def start(self) -> dict:
        self.ensure_network()
        self.ensure_egress_network()
        # Start on the egress network first (so the proxy can reach the remote), then attach the
        # internal network with the `target` alias the solver resolves.
        res = _run([
            "docker", "run", "-d", "--rm",
            "--name", self.name,
            "--network", self.egress_network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=16m",
            "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
            self.image,
            "python3", "-c", PROXY_SRC,
            str(self.listen_port), self.remote_host, str(self.remote_port),
        ], 90)
        if res.returncode != 0:
            raise TargetError(f"forwarder run failed: {res.stderr.strip()[-500:]}")
        self.container_id = res.stdout.strip()
        attach = _run(["docker", "network", "connect", "--alias", self.local_host,
                       self.network, self.container_id], 30)
        if attach.returncode != 0:
            self.stop()
            raise TargetError(f"attach internal network failed: {attach.stderr.strip()[:300]}")
        self._await_listening()
        self.endpoint = {"host": self.local_host, "port": self.listen_port, "type": "nc",
                         "network": self.network, "remote": f"{self.remote_host}:{self.remote_port}"}
        return self.endpoint

    def _await_listening(self) -> None:
        import time
        for _ in range(50):
            res = _run(["docker", "exec", self.container_id, "netstat", "-ltn"], 10)
            if res.returncode == 0 and f":{self.listen_port}" in res.stdout:
                return
            time.sleep(0.1)
        raise TargetError(f"forwarder did not listen on {self.listen_port} within 5s")

    def container_ids(self) -> list:
        if not self.container_id:
            return []
        return [c for c in _run(["docker", "ps", "-aq", "--filter", f"id={self.container_id}"], 15).stdout.split()]

    def stop(self) -> dict:
        report = {"project": self.project, "down_rc": None, "forced_containers": [], "network_removed": None}
        if self.on_stop is not None:        # e.g. stop the remote HTB container (teardown/respawn)
            try:
                report["remote_stop"] = self.on_stop()
            except Exception as err:        # teardown of our own plumbing must still proceed
                report["remote_stop_error"] = f"{type(err).__name__}: {err}"
        leftovers = self.container_ids()
        if leftovers:
            _run(["docker", "rm", "-f", "-v", *leftovers], 60)
            report["forced_containers"] = leftovers
        for net in (self.network, self.egress_network):
            if self._network_exists(net):
                _run(["docker", "network", "rm", net], 30)
        report["network_removed"] = not (self._network_exists(self.network) or self._network_exists(self.egress_network))
        report["clean"] = not self.container_ids() and report["network_removed"]
        report["down_rc"] = 0 if report["clean"] else 1
        if not report["clean"]:
            raise TargetError(f"forwarder/network survived teardown for {self.project}")
        return report

    def inspect_isolation(self) -> dict:
        """The forwarder MAY sit on an egress network (that is its job); it must not be privileged,
        add capabilities, publish ports, bind-mount anything, or expose the docker socket. The
        solver sandbox's isolation (internal-only, no egress) is verified separately by the runtime
        egress check once the sandbox is up."""
        import json
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
            if self.network not in nets:
                violations.append(f"{name}: not attached to internal network {self.network}")
        return {"ok": not violations, "violations": violations, "containers": containers}
