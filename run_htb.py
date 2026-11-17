"""Run one Hack The Box challenge end-to-end through the controller (no CTFTiny dataset).

Fully offline-from-the-platform (MCP entirely disabled):

    python run_htb.py --platform manual --flag-mode manual \
        --files-dir external/hackthebox/<name> --target IP:PORT

The controller stages the operator's files into the solver workspace, starts the target forwarder so
the remote is reachable as `target`, runs the air-gapped solver (only `target`, no general egress;
proven by a runtime egress check), and surfaces the candidate flag (console block +
htb_candidates.txt + htb_candidates.jsonl). --platform only changes where the endpoint/files come
from (manual = your IP:PORT + files-dir; mcp = the HTB MCP client, scaffold); the solver, its
workspace, and its five tools are byte-identical in both. --flag-mode is independent: manual =
surface the candidate for you to submit; auto = submit via the controller MCP client.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from common import config  # noqa: E402
from controller.control_plane import PLATFORMS, build_control_plane  # noqa: E402
from controller.pacing import wait_for_rate_window  # noqa: E402
from controller.runner import run_attempt  # noqa: E402
from run_batch import make_solver  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", choices=PLATFORMS, default="manual",
                    help="control-plane source: manual (files-dir + IP:PORT, no MCP) | mcp (scaffold)")
    ap.add_argument("--flag-mode", choices=("manual", "auto"), default="manual",
                    help="candidate handling: manual (surface for you) | auto (submit via MCP)")
    ap.add_argument("--files-dir", type=Path, default=None, help="operator challenge files (e.g. external/hackthebox/<name>)")
    ap.add_argument("--target", default=None, help="remote challenge endpoint IP:PORT")
    ap.add_argument("--name", default=None, help="challenge id/name (default: the files-dir name)")
    ap.add_argument("--description", default=None, help="override the challenge description shown to the solver")
    ap.add_argument("--trial", type=int, default=1)
    ap.add_argument("--prompt", choices=sorted(config.ARMS), default=None,
                    help="solver arm (sets CTF_ARM); 1.1.0 adds imaging/OCR, useful for image-bearing challenges")
    ap.add_argument("--results", type=Path, default=ROOT / "hackthebox_runs" / "htb_results.jsonl")
    ap.add_argument("--traces", type=Path, default=ROOT / "hackthebox_runs" / "traces")
    ap.add_argument("--runs", type=Path, default=ROOT / "hackthebox_runs" / "runs")
    ap.add_argument("--no-wait", action="store_true", help="skip the pre-attempt rate-limit window check")
    ap.add_argument("--max-turns", type=int, default=None, help="override budget.max_turns for this run only")
    ap.add_argument("--token-cap", type=int, default=None, help="override budget.attempt_token_cap for this run only")
    a = ap.parse_args(argv)

    if a.prompt:
        os.environ["CTF_ARM"] = a.prompt
    os.environ["CTF_FLAG_MODE"] = a.flag_mode     # HTB is never the local answer-key mode
    settings = config.load_settings()
    if a.max_turns is not None:
        settings["budget"]["max_turns"] = a.max_turns
    if a.token_cap is not None:
        settings["budget"]["attempt_token_cap"] = a.token_cap

    cp = build_control_plane(a.platform, files_dir=a.files_dir, target=a.target,
                             name=a.name, description=a.description)
    challenge_id = cp.challenge_id

    if not a.no_wait:
        wait_for_rate_window(settings)
    rec = run_attempt(challenge_id, a.trial, make_solver, settings=settings, label=f"htb-{a.platform}",
                      results_path=a.results, traces_dir=a.traces, runs_dir=a.runs,
                      provision=cp.provision)

    print(f"\n{rec['run_id']}: status={rec['status']} candidate={rec.get('candidate')!r} "
          f"verified={rec.get('verified')} elapsed_s={rec['elapsed_s']}")
    print(f"trace: {rec['trace_file']}")
    print(f"candidates: {a.results.parent / 'htb_candidates.txt'}  (+ .jsonl)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
