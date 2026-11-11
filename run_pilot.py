"""Run each pilot challenge once (trial 1), sequentially, into separate pilot files.

    python run_pilot.py --tag pilot3 [--ids pilot_ids.txt] [--plan]

Writes results_<tag>.jsonl and traces_<tag>/ (gitignored). Refuses any scored key.
Pilot results are verification runs, not benchmark scores.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import run_attempt                  # noqa: E402  (sets up sys.path for src/)


def load_pilot(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            dataset, cid = line.split()
            if dataset not in ("dev", "ctftiny"):
                raise SystemExit(f"unknown pilot dataset {dataset!r} for {cid}")
            if cid in run_attempt.SCORED:
                raise SystemExit(f"{cid!r} is a scored key and cannot be in the pilot")
            rows.append((dataset, cid))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", required=True, help="pilot name, e.g. pilot3")
    ap.add_argument("--ids", type=Path, default=ROOT / "pilot_ids.txt")
    ap.add_argument("--plan", action="store_true", help="print the plan and exit")
    a = ap.parse_args(argv)
    if not a.tag.startswith("pilot"):
        raise SystemExit("--tag must start with 'pilot' so the outputs stay gitignored")
    rows = load_pilot(a.ids)
    for dataset, cid in rows:
        print(f"{dataset:8} {cid} trial 1")
    if a.plan:
        return 0
    for dataset, cid in rows:
        print(f"===== {cid} 1  ({datetime.now():%H:%M:%S})", flush=True)
        args = [cid, "1", "--pilot", "--results", f"results_{a.tag}.jsonl",
                "--traces", f"traces_{a.tag}", "--runs", "runs"]
        run_attempt.main(args + (["--dev"] if dataset == "dev" else []))
    print(f"===== done ({datetime.now():%H:%M:%S})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
