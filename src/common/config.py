"""Run configuration: non-secret settings from config/settings.json, secrets from .env.

Adapted from the triage agent's config.py. Paths in settings.json are relative to the repository
root so the project runs from any working directory.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "config" / "settings.json"
SECRET_KEYS = ("LITELLM_TOKEN",)


def path(rel: str) -> Path:
    """Resolve a repo-relative path from settings.json."""
    return ROOT / rel


def load_settings(settings_path: Path = SETTINGS_PATH) -> dict:
    """Load settings.json, then the optional LITELLM_MODEL override from .env.

    There is deliberately no caller-override hook: one fixed configuration (config_id=agent).
    """
    load_dotenv(ROOT / ".env")
    s = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    if s.get("config_id") != "agent":
        raise ValueError("config_id must be 'agent' (one fixed configuration)")
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
