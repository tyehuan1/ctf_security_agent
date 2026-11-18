"""Run configuration: non-secret settings from config/settings.json, secrets from .env.

Adapted from the triage agent's config.py. Paths in settings.json are relative to the repository
root so the project runs from any working directory.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "config" / "settings.json"
# Secrets live only in .env; redacted from traces/exports and never given to the solver sandbox.
#   LITELLM_TOKEN   - model gateway key for the frozen CTFTiny scored path.
#   CHALLENGE_TOKEN - Duke AI gateway key for the HTB competition (its own budget); the default
#                     model token for HTB runs (run_htb.py). Falls back to LITELLM_TOKEN if unset.
#   MCP_TOKEN       - HTB MCP server auth (controller-side only). Unrelated to the two gateway keys.
SECRET_KEYS = ("LITELLM_TOKEN", "CHALLENGE_TOKEN", "MCP_TOKEN")

# How the controller handles a solver's candidate flag. Controller-side only; the solver never
# participates (it reports a candidate through submit_flag exactly as before, and never sees any token).
#   local  : CTFTiny scored runs -- exact-match against the answer key (verify_privately). Unchanged.
#   manual : HTB -- no local key; capture the candidate and surface it for a human to submit. No auto-submit.
#   auto   : HTB -- submit the candidate to HTB via the controller's MCP client; HTB scoring is the result.
FLAG_MODES = ("local", "manual", "auto")
HTB_FLAG_MODES = ("manual", "auto")

# Configuration arms. Each is one fixed config_id="agent" configuration; they differ only in the
# solver prompt variant, the sandbox image, and (optionally) a pinned model. The active arm is
# CTF_ARM (env) / the --prompt launch flag if set, else config/settings.json's agent_version. The
# frozen behavior of each arm is never changed. "1.0.0" is the selected CTFTiny configuration;
# "baseline" is the pre-improvement prompt for comparison; "1.1.0" adds imaging+OCR; "2.1.0" adds
# sigrok logic-analyzer + gerbv; "2.2.0" adds broad challenge tooling (radare2, z3, tshark, foremost,
# libc-database, ...). "2.3.0" adds a Ghidra-based decompiler (r2ghidra, callable as `pdg` inside
# radare2) plus a scoped reversing/analysis-workflow prompt block. "2.3.1" adds two SSRF prompt blocks
# (scheme variation + establish the read primitive first) on top of 2.3.0. "2.3.2" strengthens the
# SSRF block so file:// is a mandatory first move (read local files/source before HTTP enumeration) —
# all three 2.3.x arms share the v2.3 image (no rebuild). "2.3.2" is the default (Haiku 5.5, from
# settings) for HTB runs (run_htb.py). The "-sonnet" arms are HTB escalation/backup arms:
# byte-identical sandbox and prompt as their base, but the model is pinned to Claude Sonnet 5.5 for
# the harder challenges Haiku can't crack. CTFTiny's scored default stays "1.0.0". An arm with no
# "model" key runs on settings.model.name (Haiku).
ARMS = {
    "baseline":     {"image": "ctf-solver-sandbox:v1",   "prompt": "baseline"},
    "1.0.0":        {"image": "ctf-solver-sandbox:v1",   "prompt": "frozen"},
    "1.1.0":        {"image": "ctf-solver-sandbox:v1.1", "prompt": "imaging"},
    "2.1.0":        {"image": "ctf-solver-sandbox:v2.1", "prompt": "sigrok"},
    "2.2.0":        {"image": "ctf-solver-sandbox:v2.2", "prompt": "tools"},
    "2.2.0-sonnet": {"image": "ctf-solver-sandbox:v2.2", "prompt": "tools",  "model": "claude-sonnet-5-5"},
    "2.3.0":        {"image": "ctf-solver-sandbox:v2.3", "prompt": "decomp"},
    "2.3.0-sonnet": {"image": "ctf-solver-sandbox:v2.3", "prompt": "decomp", "model": "claude-sonnet-5-5"},
    "2.3.1":        {"image": "ctf-solver-sandbox:v2.3", "prompt": "websec"},
    "2.3.1-sonnet": {"image": "ctf-solver-sandbox:v2.3", "prompt": "websec", "model": "claude-sonnet-5-5"},
    "2.3.2":        {"image": "ctf-solver-sandbox:v2.3", "prompt": "websec2"},
    "2.3.2-sonnet": {"image": "ctf-solver-sandbox:v2.3", "prompt": "websec2", "model": "claude-sonnet-5-5"},
}

# Fallback per-token rates (USD per 1M tokens), used only when the gateway omits the
# x-litellm-response-cost header (see common/usage.py). First-party Anthropic list prices; a gateway
# may bill differently, so the cost header always wins. load_settings selects the entry matching the
# resolved model name so the Sonnet backup arm is costed at Sonnet rates, not Haiku's.
MODEL_PRICING = {
    "claude-haiku-5-5":  {"input_per_m": 0.10, "cached_input_per_m": 0.01, "output_per_m": 0.50},
    "claude-sonnet-5-5": {"input_per_m": 2.00, "cached_input_per_m": 0.10, "output_per_m": 10.00},
}


def active_arm(settings_agent_version: str | None = None) -> str:
    """The arm this process runs: CTF_ARM if set, else the given/settings agent_version."""
    arm = os.getenv("CTF_ARM") or settings_agent_version
    if arm is None:
        arm = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("agent_version")
    if arm not in ARMS:
        raise ValueError(f"unknown configuration arm {arm!r}; known arms: {sorted(ARMS)}")
    return arm


def flag_mode(settings_flag: dict | None = None) -> str:
    """The active flag-handling mode: CTF_FLAG_MODE env, else settings.flag.mode, else 'local'.

    See FLAG_MODES. 'local' keeps the frozen CTFTiny behavior; the HTB modes route the candidate
    to the controller's HTB flag gateway instead of the local answer-key verifier.
    """
    mode = os.getenv("CTF_FLAG_MODE") or (settings_flag or {}).get("mode") or "local"
    if mode not in FLAG_MODES:
        raise ValueError(f"unknown flag mode {mode!r}; known modes: {FLAG_MODES}")
    return mode


def code_version() -> dict:
    """Git commit the run executed at, for the trace (not the frozen A4 result object)."""
    def _git(*args):
        try:
            r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
    commit = _git("rev-parse", "HEAD")
    # Only tracked modifications count as "dirty"; untracked output/scratch dirs (runs/, traces.*,
    # results.*) are not code changes and must not flip the flag.
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def path(rel: str) -> Path:
    """Resolve a repo-relative path from settings.json."""
    return ROOT / rel


def load_settings(settings_path: Path = SETTINGS_PATH) -> dict:
    """Load settings.json, resolve the configuration arm, then the optional LITELLM_MODEL override.

    config_id is always "agent" (one fixed configuration). The arm (CTF_ARM env or agent_version)
    selects the sandbox image and whether the prompt carries imaging/OCR guidance; it is recorded
    in agent_version and the sandbox block so every trace self-identifies which arm ran.
    """
    load_dotenv(ROOT / ".env")
    s = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    if s.get("config_id") != "agent":
        raise ValueError("config_id must be 'agent' (one fixed configuration)")
    arm = active_arm(s.get("agent_version"))
    s["agent_version"] = arm
    s["sandbox"] = {**s.get("sandbox", {}), "image": ARMS[arm]["image"], "prompt": ARMS[arm]["prompt"]}
    s["flag"] = {**s.get("flag", {}), "mode": flag_mode(s.get("flag"))}
    # Model routing: an arm may pin a model (e.g. the Sonnet 5.5 backup arm); otherwise the
    # settings.json model (Haiku 5.5) is the default. An explicit LITELLM_MODEL env always wins last.
    if ARMS[arm].get("model"):
        s["model"] = {**s["model"], "name": ARMS[arm]["model"]}
    if os.getenv("LITELLM_MODEL"):
        s["model"]["name"] = os.environ["LITELLM_MODEL"]
    # Cost-accounting fallback: price the resolved model correctly when the gateway omits the cost
    # header. Known rates win over any stale pricing block in settings.json.
    if s["model"]["name"] in MODEL_PRICING:
        s["model"]["pricing"] = {**s["model"].get("pricing", {}), **MODEL_PRICING[s["model"]["name"]]}
    return s


def secret(name: str) -> str | None:
    """Read a secret. Returns None when unset so callers can fail with a clear message."""
    load_dotenv(ROOT / ".env")
    return os.getenv(name) or None


def secret_values() -> list[str]:
    """Current secret values, for redaction of traces and exports."""
    load_dotenv(ROOT / ".env")
    return [v for v in (os.getenv(k) for k in SECRET_KEYS) if v]


def redacted(settings: dict) -> dict:
    """Settings safe to save with a run (settings never hold secrets, but guard anyway)."""
    s = copy.deepcopy(settings)
    blob = json.dumps(s)
    for val in secret_values():
        if val in blob:
            raise ValueError("a secret value was found in settings; secrets belong in .env only")
    return s
