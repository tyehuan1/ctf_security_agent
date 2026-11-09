"""execute_scoped_tool_with_timeout(action): the only path from a validated tool call to a
process in the sandbox. Honors the per-tool timeout and output cap the controller supplies.

Four of the five tools run inside the sandbox container (run_shell, inspect_file, python_exec,
connect_endpoint). submit_flag is intercepted by the Agent and never reaches this runner.

An out-of-scope network attempt is a normal tool result with status="blocked" so it appears in
the trace rather than killing the attempt.
"""

from __future__ import annotations

import shlex
from typing import Optional

from solver.sandbox import ExecResult, Sandbox, SandboxError
from tools.specs import InvalidArguments, validate_arguments

MAX_INSPECT_BYTES_DEFAULT = 512
MAX_RECV = 65536


class ToolRunner:
    def __init__(self, sandbox: Sandbox, target_endpoint: Optional[dict], max_chars: int):
        self.sandbox = sandbox
        self.endpoint = target_endpoint              # {"host": "target", "port": n, ...} or None
        self.max_chars = int(max_chars)

    # ------------------------------------------------------------------ dispatch

    def execute(self, name: str, arguments, timeout_s: float) -> dict:
        try:
            args = validate_arguments(name, arguments)
        except InvalidArguments as err:
            return {"status": "error", "output": "", "error": f"invalid arguments: {err}"}
        timeout_s = max(1.0, float(timeout_s))
        requested = args.get("timeout_s")
        if requested is not None:                    # model can shorten, never extend
            timeout_s = min(timeout_s, float(requested))
        try:
            if name == "run_shell":
                return self._run_shell(args["command"], timeout_s)
            if name == "inspect_file":
                return self._inspect_file(args, timeout_s)
            if name == "python_exec":
                return self._python_exec(args["code"], timeout_s)
            if name == "connect_endpoint":
                return self._connect_endpoint(args, timeout_s)
            return {"status": "error", "output": "", "error": f"no runner for {name!r}"}
        except SandboxError as err:
            return {"status": "error", "output": "", "error": f"sandbox: {err}"}

    # ------------------------------------------------------------------ tools

    def _wrap(self, res: ExecResult) -> dict:
        out = res.stdout or ""
        if res.stderr:
            out = (out + ("\n--- stderr ---\n" if out else "") + res.stderr) if res.stderr.strip() else out
        payload = {"status": res.status, "output": out, "exit_code": res.exit_code}
        if res.status == "timeout":
            payload["error"] = "timed out"
        elif res.status == "error" and not res.stderr:
            payload["error"] = f"exit {res.exit_code}"
        return payload

    def _run_shell(self, command: str, timeout_s: float) -> dict:
        return self._wrap(self.sandbox.exec(["/bin/sh", "-c", command], timeout_s))

    def _inspect_file(self, args: dict, timeout_s: float) -> dict:
        path = args["path"]
        op = args["op"]
        if op == "file":
            argv = ["file", "-b", path]
        elif op == "strings":
            argv = ["strings", "-n", str(args.get("min_len", 4)), path]
        elif op == "xxd":
            offset = int(args.get("offset", 0))
            length = int(args.get("length", MAX_INSPECT_BYTES_DEFAULT))
            argv = ["xxd", "-s", str(offset), "-l", str(length), path]
        elif op == "binwalk":
            argv = ["python3", "-m", "binwalk", path]
        else:
            return {"status": "error", "output": "", "error": f"inspect_file: unknown op {op!r}"}
        res = self.sandbox.exec(argv, timeout_s)
        if res.status != "ok" and not res.stdout and not res.stderr:
            res = ExecResult("error", stderr=f"command not available or path missing: {' '.join(argv)}",
                             exit_code=res.exit_code)
        return self._wrap(res)

    def _python_exec(self, code: str, timeout_s: float) -> dict:
        # Code arrives on stdin, so no quoting issues and no file leftover in the workspace.
        res = self.sandbox.exec(["python3", "-I", "-"], timeout_s, stdin=code)
        return self._wrap(res)

    def _connect_endpoint(self, args: dict, timeout_s: float) -> dict:
        host = args["host"]
        port = int(args["port"])
        if host != "target":
            return {"status": "blocked", "output": "",
                    "error": f"host {host!r} not allowed; the only reachable host is 'target'"}
        if not self.endpoint:
            return {"status": "blocked", "output": "",
                    "error": "this challenge has no local service; 'target' is unreachable"}
        if port != int(self.endpoint["port"]):
            return {"status": "blocked", "output": "",
                    "error": f"port {port} not allowed; the only allowed port is {self.endpoint['port']}"}
        read_bytes = min(int(args.get("read_bytes", 4096)), MAX_RECV)
        sock_timeout = float(args.get("timeout_s") or min(timeout_s, 15))
        send = args.get("send") or ""
        # Run the connection from inside the sandbox so the internal network is what actually gates it.
        snippet = (
            "import socket,sys,json\n"
            f"host, port, send, read_bytes, t = 'target', {port}, {send!r}, {read_bytes}, {sock_timeout}\n"
            "s = socket.create_connection((host, port), timeout=t)\n"
            "s.settimeout(t)\n"
            "try:\n"
            "    if send: s.sendall(send.encode())\n"
            "    buf = b''\n"
            "    while len(buf) < read_bytes:\n"
            "        try:\n"
            "            chunk = s.recv(min(4096, read_bytes - len(buf)))\n"
            "        except socket.timeout:\n"
            "            break\n"
            "        if not chunk: break\n"
            "        buf += chunk\n"
            "    sys.stdout.write(buf.decode('utf-8', 'replace'))\n"
            "finally:\n"
            "    s.close()\n"
        )
        res = self.sandbox.exec(["python3", "-I", "-"], timeout_s, stdin=snippet)
        payload = self._wrap(res)
        if res.status == "error" and "ConnectionRefused" in (res.stderr or ""):
            payload["error"] = "connection refused by target"
        return payload


def execute_scoped_tool_with_timeout(runner: ToolRunner, action, timeout_s: float) -> dict:
    """Appendix step name. Delegates to ToolRunner.execute with the controller's timeout."""
    return runner.execute(action.tool, action.arguments, timeout_s)
