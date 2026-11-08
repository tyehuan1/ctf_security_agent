# CLAUDE.md — CTF benchmarking agent (CYBERSEC 511, Assignment 4)

Source of truth: **Appendix A Implementation Guide** (the course PDF). When code and this file
disagree with the appendix, the appendix wins. Match its identifiers, file names, and schema exactly.

## Invariants (apply to every phase)

1. **Two separated processes.**
   - **Trusted controller** (`src/controller/`, host side): holds the answer key, verifies flags
     (`verify_privately`), starts/resets/stops targets, owns *all* resource accounting (turns,
     tokens, wall-clock, output caps), writes `results.jsonl` and traces.
   - **Isolated solver** (`src/solver/`, `src/tools/`, runs in a container): proposes actions only.
     It never sees `chal.flag`, `chal.challenge`, `challenge.json`, the benchmark repo, or the
     verifier. It has **no web access** and no answer-lookup tools.
2. **`config_id` is always `"agent"`.** One fixed configuration. No second configuration, no
   second full batch, no paired/pilot comparison. Trials are 1, 2, 3 per challenge.
3. **Every attempt is recorded** in `results.jsonl` using the A4 result object (below).
   `sample_type="live"` only for real attempts. Use `null` for unavailable counters — never 0.
4. **Model output cannot change limits or widen target scope.** Budgets are frozen in the
   controller (`src/controller/budget.py`); there is no API by which solver output adjusts them.
5. **Appendix code is authoritative.** Use its names: `CTFDataset(dataset_json=...)`,
   `CTFChallenge(ds.get(key), ds.basedir)`, `verify_privately`, `reset_solver_and_target`,
   `start_attempt_timer`, `initial_contestant_context`, `attempt_budget_remains`,
   `request_next_action`, `record_model_usage`, `record_submission`,
   `execute_scoped_tool_with_timeout`, `log_tool_result_and_usage`,
   `stop_solver_and_record_terminal_status`, `make_manifest.py`, `ctf_single_agent_summary.py`,
   `evaluation_manifest.json`, `results.jsonl`, `summary.json`, `per_challenge.csv`.

## A4 result object (exact keys, exact order)

```json
{
  "run_id": "example-agent-01",
  "config_id": "agent", "challenge_id": "practice-0",
  "trial": 1, "status": "no_flag", "verified": false,
  "elapsed_s": 10, "input_tokens": null,
  "output_tokens": null, "cost_usd": null,
  "trace_file": "traces/example-agent-01.jsonl",
  "reset_id": "fresh-example-01", "sample_type": "synthetic"
}
```

Status vocabulary (`src/common/schemas.py`):
- success: `solved` (verified=true) — confirm against the kit's `ctf_single_agent_summary.py`.
- failures (count as 0): `no_flag`, `wrong_flag`, `timeout`, `budget_exhausted`, `tool_error`.
- incident (retain, mark challenge incomplete, follow instructor guidance):
  `infrastructure_error`, `scope_violation`. Never silently retry or invent outcomes.

## A3 isolation controls

- Controller exports only: the contestant files listed in `challenge.json["files"]`, a sanitized
  description (CSAW hostnames rewritten to the local target alias), and the local target endpoint.
  It keeps a SHA-256 inventory of exported files.
- `external/` (benchmark repos) and the verifier are never mounted into the solver.
- Upstream `docker-compose.yml` files are **not** run as-is (they mount `/var/run/docker.sock`,
  use `privileged`, publish host ports, and join the plain `ctfnet` bridge). `target.py` renders a
  sanitized per-attempt compose: no docker.sock / bind mounts, no privileged, no cap_add, no
  published ports, an `internal: true` per-attempt network.
- Check published ports, routes, mounts, capabilities, and Docker socket exposure in the running
  environment (`target.inspect_isolation`). No admin shell in the target.
- Each attempt: fresh conversation, memory, workspace, and target state. A container restart does
  not clear bind mounts/volumes; `down --volumes` + new workspace dir every attempt. A host-side
  timeout does not prove the in-container process stopped — always stop and verify teardown.

## Layout

- `config/settings.json` — non-secret config (model, base_url, caps, data paths).
- `.env` — secrets only (`LITELLM_TOKEN`). Never logged; `record.py` redacts it from traces.
- `external/` — cloned data, gitignored. Pinned commits in `resources.txt`.
- `evaluation_ids.txt` — the 10 CTFTiny keys.
- `src/controller/` — loader, verifier, target, budget, runner, record.
- `src/solver/`, `src/tools/` — solver side (built in Phase 1+).
- `make_manifest.py`, `ctf_single_agent_summary.py` — instructor kit scripts; prefer the kit's
  copies over our own.

## Windows notes

- CTFTiny contains `ctftiny/pwn/get_it?/` (illegal on NTFS). It is sparse-excluded and extracted
  byte-identically from the pinned commit to `ctftiny/pwn/get_it_/`; `loader.py` remaps the path
  via `settings.data.path_remap`. `ctftiny.json` is not modified.
- Data repos use `core.autocrlf=false` so shell scripts/Dockerfiles keep LF endings.

## Working rules

- Do not start a large batch to diagnose setup. Setup checks are not benchmark scores.
- Tests: `.venv\Scripts\python.exe -m pytest` (or `-m unittest discover -s tests`).
- Readiness: `.venv\Scripts\python.exe check_ready.py`.
