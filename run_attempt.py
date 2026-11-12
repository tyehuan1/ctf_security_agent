"""Run one attempt on one challenge: produces one results.jsonl line and one trace.

    python run_attempt.py CHALLENGE_ID TRIAL [--dev] [--pilot] [--results path] [--traces path] [--runs path]

--dev reads from external/NYU_CTF_Bench/development_dataset.json (for smoke tests on development
challenges only; never the 10 scored keys). Without --dev the CTFTiny evaluation index is used.
--pilot refuses the 10 scored keys on either dataset (e.g. CTFTiny rev-checker as a pilot baseline).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from common import config          # noqa: E402
from controller import loader      # noqa: E402
from controller.pacing import wait_for_rate_window  # noqa: E402
from controller.runner import run_attempt  # noqa: E402
from run_batch import make_solver  # noqa: E402

SCORED = {l.strip() for l in (ROOT / "evaluation_ids.txt").read_text(encoding="utf-8").splitlines()
          if l.strip() and not l.startswith("#")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("challenge_id")
    ap.add_argument("trial", type=int, choices=(1, 2, 3))
    ap.add_argument("--dev", action="store_true", help="use development_dataset.json")
    ap.add_argument("--pilot", action="store_true", help="verification run: refuse the scored keys")
    ap.add_argument("--results", type=Path, default=None)
    ap.add_argument("--traces", type=Path, default=None)
    ap.add_argument("--runs", type=Path, default=None)
    ap.add_argument("--no-wait", action="store_true", help="skip the pre-attempt rate-limit window check")
    ap.add_argument("--label", default=None,
                    help="tag woven into the run_id and trace filename so a run is easy to spot (e.g. arm-v1.1.0)")
    ap.add_argument("--prompt", choices=sorted(config.ARMS), default=None,
                    help="configuration arm to run (sets CTF_ARM): baseline | 1.0.0 (selected) | 1.1.0")
    a = ap.parse_args(argv)

    if a.prompt:
        os.environ["CTF_ARM"] = a.prompt           # select the arm before settings resolve it
    settings = config.load_settings()
    if a.dev:
        settings["data"]["dataset_json"] = settings["data"]["dev_dataset_json"]
        if a.challenge_id in SCORED:
            raise SystemExit(f"--dev is for development challenges only; {a.challenge_id!r} is a scored key")
    if a.pilot and a.challenge_id in SCORED:
        raise SystemExit(f"--pilot never runs a scored key; {a.challenge_id!r} is in evaluation_ids.txt")
    ds = loader.load_dataset(settings)
    if a.challenge_id not in ds.dataset:
        raise SystemExit(f"challenge_id {a.challenge_id!r} not in {settings['data']['dataset_json']}")

    if not a.no_wait:
        wait_for_rate_window(settings)            # don't inherit a previous run's rate-limit window
    rec = run_attempt(a.challenge_id, a.trial, make_solver, settings=settings, ds=ds,
                      results_path=a.results, traces_dir=a.traces, runs_dir=a.runs, label=a.label)
    print(f"{rec['run_id']}: {rec['status']} verified={rec['verified']} elapsed_s={rec['elapsed_s']} "
          f"input={rec['input_tokens']} output={rec['output_tokens']} cost={rec['cost_usd']}")
    print(f"trace: {rec['trace_file']}")
    return 0 if rec["verified"] or rec["status"] in ("no_flag", "wrong_flag", "budget_exhausted", "timeout") else 1


if __name__ == "__main__":
    sys.exit(main())
