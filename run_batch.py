"""Run trials 1, 2, 3 for each challenge in the manifest: 3 x N attempts, one configuration.

    python run_batch.py --manifest evaluation_manifest.json            # run what is missing
    python run_batch.py --manifest evaluation_manifest.json --plan     # show the plan only
    python run_batch.py --manifest evaluation_manifest.json --max-attempts 3

Resumable and idempotent:
  - A trial slot (challenge_id, trial) already in results.jsonl is never run again.
  - A trace with no result (the controller crashed mid-attempt) is closed out as
    infrastructure_error, not silently retried; follow instructor guidance for incidents.
  - Every attempt starts with a new solver (fresh conversation and memory), a new workspace, and
    a fresh target; nothing is carried between attempts.
  - --max-attempts caps how many attempts this invocation runs; unrun slots stay unrecorded.
A lock file stops two batch processes from writing the same results.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from common import config  # noqa: E402
from common.schemas import CONFIG_ID, TRIALS, make_result  # noqa: E402
from controller.pacing import wait_for_rate_window  # noqa: E402
from controller.record import append_result, load_results  # noqa: E402

LOCK_NAME = ".run_batch.lock"


def recorded_slots(results: list[dict]) -> dict:
    return {(r["challenge_id"], r["trial"]): r for r in results
            if r.get("config_id") == CONFIG_ID and r.get("sample_type") == "live"}


def plan(manifest: dict, results: list[dict]) -> list[tuple[str, int]]:
    done = recorded_slots(results)
    return [(c["challenge_id"], t) for c in manifest["challenges"] for t in TRIALS
            if (c["challenge_id"], t) not in done]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def recover_orphans(traces_dir: Path, results_path: Path) -> list[dict]:
    """Close out live traces that have no result line (crash mid-attempt) as infrastructure_error."""
    results = load_results(results_path)
    known_runs = {r["run_id"] for r in results}
    done = recorded_slots(results)
    recovered = []
    for trace in sorted(Path(traces_dir).glob("*.jsonl")) if Path(traces_dir).exists() else []:
        events = [json.loads(l) for l in trace.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not events or events[0].get("event") != "attempt_started":
            continue
        first, run_id = events[0], events[0]["run_id"]
        if run_id in known_runs or first.get("config_id") != CONFIG_ID or first.get("sample_type") != "live":
            continue
        slot = (first["challenge_id"], first["trial"])
        if slot in done:
            continue
        elapsed = (_ts(events[-1]["ts"]) - _ts(first["ts"])).total_seconds()
        try:
            rel = trace.resolve().relative_to(config.ROOT).as_posix()
        except ValueError:
            rel = trace.as_posix()
        reset = next((e.get("reset_id") for e in events if e.get("event") == "target_started"), None)
        rec = make_result(run_id=run_id, challenge_id=slot[0], trial=slot[1], status="infrastructure_error",
                          elapsed_s=elapsed, input_tokens=None, output_tokens=None, cost_usd=None,
                          trace_file=rel, reset_id=reset or f"unknown-{run_id}", sample_type="live")
        append_result(results_path, rec)
        done[slot] = rec
        recovered.append(rec)
    return recovered


class BatchLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit(f"{self.path} exists: another batch is running (delete it only if none is)")
        os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}\n".encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def make_solver(settings, network, run_id, transcript_path):
    """A new solver per attempt (fresh conversation, memory, sandbox)."""
    from solver.agent import Agent
    return Agent(settings, network, run_id, transcript_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="3 x N attempts for config_id=agent")
    ap.add_argument("--manifest", type=Path, default=ROOT / "evaluation_manifest.json")
    ap.add_argument("--plan", action="store_true", help="print the remaining slots and exit")
    ap.add_argument("--max-attempts", type=int, default=None, help="cap attempts run by this invocation")
    a = ap.parse_args(argv)

    settings = config.load_settings()
    results_path = config.path(settings["paths"]["results"])
    traces_dir = config.path(settings["paths"]["traces_dir"])
    manifest = json.loads(a.manifest.read_text(encoding="utf-8"))
    n = len(manifest["challenges"])

    if a.plan:
        todo = plan(manifest, load_results(results_path))
        print(f"{3 * n - len(todo)}/{3 * n} slots recorded; {len(todo)} remaining")
        for cid, t in todo:
            print(f"  {cid} trial {t}")
        return 0

    from controller.runner import run_attempt
    with BatchLock(ROOT / LOCK_NAME):
        for rec in recover_orphans(traces_dir, results_path):
            print(f"recovered crashed attempt as infrastructure_error: {rec['run_id']}")
        todo = plan(manifest, load_results(results_path))
        if a.max_attempts is not None:
            todo = todo[:max(0, a.max_attempts)]
        print(f"running {len(todo)} attempt(s) of {3 * n} planned, strictly one at a time")
        last_tokens = None
        for cid, trial in todo:                       # sequential by design: never parallel attempts
            wait_for_rate_window(settings, last_tokens)
            rec = run_attempt(cid, trial, make_solver, settings=settings)
            last_tokens = (rec["input_tokens"] or 0) + (rec["output_tokens"] or 0) or None
            print(f"{cid} t{trial}: {rec['status']} verified={rec['verified']} {rec['elapsed_s']}s "
                  f"in={rec['input_tokens']} out={rec['output_tokens']} cost={rec['cost_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
