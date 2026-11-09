"""OpenAI function specs and JSON-Schema validation for the solver's generic tool belt.

The tools are challenge-agnostic analysis utilities — no challenge-specific code:
  run_shell           : one shell command in the sandbox, stdout/stderr returned
  inspect_file        : file / strings / xxd / binwalk wrappers
  python_exec         : short Python snippet (pycryptodome/requests/sympy/binwalk available)
  connect_endpoint    : one TCP exchange to the 'target' host on the private network
  submit_flag         : routes a candidate to the controller's verify_privately

Every call is validated here before it reaches the sandbox; invalid requests are returned to the
model as status "error" instead of being executed.
"""

from __future__ import annotations

import jsonschema

MAX_SHELL_CMD = 4000
MAX_PYTHON_SRC = 8000
MAX_TARGET_INPUT = 8000


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False}}}


TOOL_SPECS: list[dict] = [
    _fn("run_shell",
        "Run one shell command inside the solver sandbox (POSIX /bin/sh -c). "
        "Returns stdout and stderr (combined output capped by the harness). Use for ls, grep, "
        "head, strings, cat and similar quick inspections; use python_exec for anything structured.",
        {"command": {"type": "string", "minLength": 1, "maxLength": MAX_SHELL_CMD,
                     "description": "The command string, e.g. \"ls -la /workspace\" or \"strings warmup | head -50\"."},
         "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300,
                       "description": "Per-call timeout; the controller clamps this to its own cap."}},
        ["command"]),
    _fn("inspect_file",
        "Standard file/binary inspection: file type, printable strings, hex view, or binwalk scan. "
        "Prefer this over run_shell for the obvious passes on a new file.",
        {"path": {"type": "string", "minLength": 1,
                  "description": "Path inside the sandbox, usually /workspace/<filename>."},
         "op": {"type": "string", "enum": ["file", "strings", "xxd", "binwalk"],
                "description": "Which inspection to perform."},
         "min_len": {"type": "integer", "minimum": 1, "maximum": 64,
                     "description": "strings: minimum run length (default 4)."},
         "offset": {"type": "integer", "minimum": 0,
                    "description": "xxd: byte offset to start from (default 0)."},
         "length": {"type": "integer", "minimum": 1, "maximum": 65536,
                    "description": "xxd: number of bytes to dump (default 512)."}},
        ["path", "op"]),
    _fn("python_exec",
        "Run a Python 3.11 snippet in the sandbox. Exits cleanly after stdout/stderr are captured. "
        "Available libraries: pycryptodome, requests, sympy, binwalk, and the standard library. "
        "The snippet runs with /workspace as cwd.",
        {"code": {"type": "string", "minLength": 1, "maxLength": MAX_PYTHON_SRC,
                  "description": "Python source. Print what you want to see; return values are not captured."},
         "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300,
                       "description": "Per-call timeout; the controller clamps this to its own cap."}},
        ["code"]),
    _fn("connect_endpoint",
        "Open a TCP connection to the local practice service (host 'target' on the private "
        "network) and exchange one payload. The only allowed host is 'target'; the only allowed "
        "port is the one the controller announced. Any other host or port is rejected.",
        {"host": {"type": "string", "const": "target",
                  "description": "Must be exactly 'target'."},
         "port": {"type": "integer", "minimum": 1, "maximum": 65535,
                  "description": "The target port the controller announced in the initial context."},
         "send": {"type": "string", "maxLength": MAX_TARGET_INPUT,
                  "description": "Optional bytes to send after connecting. Use \\n for newline; raw utf-8 only."},
         "read_bytes": {"type": "integer", "minimum": 1, "maximum": 65536,
                        "description": "How many bytes to read before closing (default 4096)."},
         "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 60,
                       "description": "Socket timeout; the controller clamps this to its own cap."}},
        ["host", "port"]),
    _fn("submit_flag",
        "Submit a candidate flag string to the controller. The controller verifies it privately "
        "and tells you only whether it matched. Stop making tool calls as soon as this returns "
        "verified: true.",
        {"flag": {"type": "string", "minLength": 1, "maxLength": 1024,
                  "description": "The exact candidate flag string."}},
        ["flag"]),
]

_SPECS_BY_NAME = {s["function"]["name"]: s["function"] for s in TOOL_SPECS}
TOOL_NAMES = tuple(_SPECS_BY_NAME)


class InvalidArguments(ValueError):
    """Arguments do not match the tool contract. Returned to the model as status=error."""


def tool_specs() -> list[dict]:
    """Function specs to send to the gateway."""
    return [dict(s, function=dict(s["function"])) for s in TOOL_SPECS]


def validate_arguments(name: str, args) -> dict:
    """Raise InvalidArguments on contract break; return the validated argument dict."""
    if name not in _SPECS_BY_NAME:
        raise InvalidArguments(f"unknown tool {name!r}; allowed: {list(TOOL_NAMES)}")
    if isinstance(args, str):
        raise InvalidArguments("arguments must be a JSON object, not a string")
    if not isinstance(args, dict):
        raise InvalidArguments(f"arguments must be an object, got {type(args).__name__}")
    schema = _SPECS_BY_NAME[name]["parameters"]
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(args), key=lambda e: list(e.path))
    if errors:
        raise InvalidArguments("; ".join(
            f"{'/'.join(map(str, e.path)) or 'arguments'}: {e.message}" for e in errors[:5]))
    return args
