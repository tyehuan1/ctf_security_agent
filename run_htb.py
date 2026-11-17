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

Model routing (competition-ready):
  - Default arm 2.2.0 runs on Claude Haiku 5.5 (cheap; ~$0.02/attempt). Run every challenge here first.
  - If Haiku can't crack it, escalate the same challenge with --prompt 2.2.0-sonnet (Claude Sonnet
    5.5, same sandbox/prompt/tools, ~$0.35-0.9/attempt). A failed Haiku pass is nearly free, so you
    spend the strong model only where it's needed.

Selective turn/timeout bump: --category picks the per-attempt allowance. pwn & reversing get 40
turns / 20 min / 600K tokens (they exhaust budget most often); every other category gets 30 turns /
15 min / 450K. Explicit --max-turns / --deadline-s / --token-cap still override. Both of these apply
to HTB runs only; the frozen CTFTiny scored path (run_batch.py) is never affected.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from common import config  # noqa: E402
from controller.budget import htb_budget_defaults, htb_is_long_category  # noqa: E402
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
    ap.add_argument("--category", default="hackthebox",
                    help="challenge category (pwn|rev|crypto|web|stego|hardware|misc|...). pwn/rev get "
                         "the longer 40-turn / 20-min / 600K-token allowance; others get 30-turn / "
                         "15-min / 450K. Any explicit --max-turns/--deadline-s/--token-cap overrides.")
    ap.add_argument("--challenge-id", default=None, help="HTB numeric challenge id (required for --platform mcp)")
    ap.add_argument("--description", default=None, help="override the challenge description shown to the solver")
    ap.add_argument("--trial", type=int, default=1)
    ap.add_argument("--prompt", choices=sorted(config.ARMS), default="2.2.0",
                    help="solver arm (sets CTF_ARM); default 2.2.0 = Haiku 5.5 with the full tooling "
                         "(imaging+OCR, sigrok, gerbv, radare2/z3/tshark/foremost/libc-database). Pass "
                         "'2.2.0-sonnet' to escalate a stuck challenge to Claude Sonnet 5.5 (same "
                         "sandbox/prompt, stronger model). Any other arm overrides.")
    ap.add_argument("--results", type=Path, default=ROOT / "hackthebox_runs" / "htb_results.jsonl")
    ap.add_argument("--traces", type=Path, default=ROOT / "hackthebox_runs" / "traces")
    ap.add_argument("--runs", type=Path, default=ROOT / "hackthebox_runs" / "runs")
    ap.add_argument("--token-env", default="CHALLENGE_TOKEN",
                    help="which .env var holds the model gateway key (default CHALLENGE_TOKEN, the Duke "
                         "AI competition key; falls back to LITELLM_TOKEN if unset).")
    ap.add_argument("--no-wait", action="store_true", help="skip the pre-attempt rate-limit window check")
    ap.add_argument("--max-turns", type=int, default=None, help="override budget.max_turns for this run only")
    ap.add_argument("--token-cap", type=int, default=None, help="override budget.attempt_token_cap for this run only")
    ap.add_argument("--deadline-s", type=int, default=None,
                    help="override budget.attempt_deadline_s (wall-clock) for this run only")
    a = ap.parse_args(argv)

    if a.prompt:
        os.environ["CTF_ARM"] = a.prompt
    os.environ["CTF_FLAG_MODE"] = a.flag_mode     # HTB is never the local answer-key mode
    settings = config.load_settings()
    # HTB runs bill the Duke AI competition key (CHALLENGE_TOKEN) by default, not LITELLM_TOKEN.
    settings["model"]["token_env"] = a.token_env
    # Selective turn/timeout/token bump: pwn & reversing get the longer allowance, other categories
    # stay leaner. Applied only here (HTB entrypoint); the scored CTFTiny path is never touched. Any
    # explicit CLI flag wins over the per-category default.
    defaults = htb_budget_defaults(a.category)
    settings["budget"]["max_turns"] = a.max_turns if a.max_turns is not None else defaults["max_turns"]
    settings["budget"]["attempt_token_cap"] = (
        a.token_cap if a.token_cap is not None else defaults["attempt_token_cap"])
    settings["budget"]["attempt_deadline_s"] = (
        a.deadline_s if a.deadline_s is not None else defaults["attempt_deadline_s"])

    cp = build_control_plane(a.platform, files_dir=a.files_dir, target=a.target, name=a.name,
                             description=a.description, challenge_id=a.challenge_id, category=a.category)
    challenge_id = str(cp.challenge_id)
    tier = "long (pwn/rev)" if htb_is_long_category(a.category) else "default"
    key_present = bool(config.secret(a.token_env))
    key_note = a.token_env if key_present else f"{a.token_env} MISSING -> LITELLM_TOKEN fallback"
    print(f"arm={a.prompt} model={settings['model']['name']} key={key_note} category={a.category!r} "
          f"({tier} budget: {settings['budget']['max_turns']} turns, "
          f"{settings['budget']['attempt_deadline_s']:.0f}s, {settings['budget']['attempt_token_cap']:,} tokens)")

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
