"""Evidence records: results.jsonl (A4 result objects) and per-attempt JSONL traces.

Tracer follows the triage agent's RunLog/transcript pattern: one JSON object per line, appended
with a timestamp. Secret values from .env are redacted before anything is written.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from common import config
from common.schemas import TRACE_EVENTS, validate_result
from controller.budget import truncate

REDACTED = "[REDACTED]"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class Tracer:
    """Append-only JSONL trace for one attempt (traces/<run_id>.jsonl)."""

    def __init__(self, path: Path, run_id: str, max_chars: int):
        self.path = Path(path)
        self.run_id = run_id
        self.max_chars = max_chars
        self.seq = 0
        self._secrets = config.secret_values()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: str, **fields) -> dict:
        if event not in TRACE_EVENTS:
            raise ValueError(f"unknown trace event {event!r}")
        self.seq += 1
        entry = {"ts": _now(), "run_id": self.run_id, "seq": self.seq, "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        for s in self._secrets:
            line = line.replace(s, REDACTED)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
        return entry

    def capture(self, text: Optional[str], where: str) -> Optional[str]:
        """Cap output to max_chars (head+tail) and log a truncation event when it was cut."""
        capped, info = truncate(text, self.max_chars)
        if info:
            self(event="truncation", where=where, **info)
        return capped


def load_results(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_result(path: Path, rec: dict) -> dict:
    """Validate an A4 result and append it. Refuses a second live record for the same trial slot."""
    validate_result(rec)
    if rec["sample_type"] == "live":
        slot = (rec["config_id"], rec["challenge_id"], rec["trial"])
        for old in load_results(path):
            if old.get("sample_type") == "live" and (old["config_id"], old["challenge_id"], old["trial"]) == slot:
                raise ValueError(f"trial slot already recorded: {slot} (run_id {old['run_id']}); do not silently retry")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
