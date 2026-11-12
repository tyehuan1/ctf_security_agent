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
SECRET_KEYS = ("LITELLM_TOKEN",)

# Configuration arms. Each is one fixed config_id="agent" configuration; they differ only in the
# solver prompt variant and the sandbox image. The active arm is CTF_ARM (env) / the --prompt launch
# flag if set, else config/settings.json's agent_version. The frozen behavior of each arm is never
# changed. "1.0.0" is the selected (default) configuration; "baseline" is the pre-improvement prompt
# for comparison; "1.1.0" is the post-freeze imaging+OCR arm.
ARMS = {
    "baseline": {"image": "ctf-solver-sandbox:v1",   "prompt": "baseline"},
    "1.0.0":    {"image": "ctf-solver-sandbox:v1",   "prompt": "frozen"},
    "1.1.0":    {"image": "ctf-solver-sandbox:v1.1", "prompt": "imaging"},
}


def active_arm(settings_agent_version: str | None = None) -> str:
    """The arm this process runs: CTF_ARM if set, else the given/settings agent_version."""
    arm = os.getenv("CTF_ARM") or settings_agent_version
    if arm is None:
        arm = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("agent_version")
    if arm not in ARMS:
        raise ValueError(f"unknown configuration arm {arm!r}; known arms: {sorted(ARMS)}")
    return arm


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
    if os.getenv("LITELLM_MODEL"):
        s["model"]["name"] = os.environ["LITELLM_MODEL"]
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
