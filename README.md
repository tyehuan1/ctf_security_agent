# ctf_security_agent

CTF benchmarking agent for CYBERSEC 511 Assignment 4 (Appendix A Implementation Guide).
One agent configuration (`config_id = "agent"`), three fresh attempts per challenge, 10 CTFTiny
challenges (`evaluation_ids.txt`). See `CLAUDE.md` for the invariants every phase must keep.

## Status: Phase 0 (setup + trusted controller)

Built: repo layout, config, data loading, private verifier, target lifecycle with sanitized compose,
controller budgets, A4 result records + JSONL traces, readiness check, offline tests.
Not built yet: solver loop, solver tools, model client (`src/solver`, `src/tools` are stubs).

## Setup (Windows, trusted controller)

```powershell
python -m venv .venv                       # Python 3.11
.venv\Scripts\python.exe -m pip install -r requirements.txt   # includes nyuctf==1.1.1
docker version; docker compose version     # server must respond, not just the client
```

Data (pinned commits in `resources.txt`):

```bash
git clone https://github.com/NYU-LLM-CTF/NYU_CTF_Bench.git external/NYU_CTF_Bench
git clone https://github.com/NYU-LLM-CTF/CTFTiny.git external/CTFTiny
git -C external/NYU_CTF_Bench rev-parse HEAD
git -C external/CTFTiny rev-parse HEAD
```

On Windows both checkouts fail on paths with `?` / trailing dots. `resources.txt` records the
repo-local git settings and sparse-checkout used; `pwn/get_it?` is extracted byte-identically to
`pwn/get_it_` and remapped in `config/settings.json` → `data.path_remap`.

Secrets: put `LITELLM_TOKEN=...` in `.env` (gitignored). Non-secret settings live in
`config/settings.json`. Confirm the model id with the kit's `list_gateway_models.py` before Phase 1.

## Commands

```powershell
.venv\Scripts\python.exe check_ready.py                       # readiness (no targets started, no model calls)
.venv\Scripts\python.exe src\controller\loader.py pwn-roppity # name, category, files (never the flag)
.venv\Scripts\python.exe -m pytest -v                         # offline tests
```

## Layout

| Path | Role |
|---|---|
| `src/controller/loader.py` | `CTFDataset(dataset_json=...)`, `CTFChallenge(ds.get(key), ds.basedir)`, contestant export + hash inventory |
| `src/controller/verifier.py` | `verify_privately(submitted, chal) -> {"verified": bool}` |
| `src/controller/target.py` | per-attempt sanitized compose, start/stop/teardown verification, isolation inspection |
| `src/controller/budget.py` | frozen caps: max_turns, max_chars, tool_timeout_s, attempt_deadline_s, attempt_token_cap |
| `src/controller/record.py` | `results.jsonl` (A4 schema) and `traces/<run_id>.jsonl` |
| `src/controller/runner.py` | the A4 control flow for one attempt |
| `src/common/schemas.py` | A4 result fields, status vocabulary, trace events |
| `make_manifest.py`, `ctf_single_agent_summary.py` | placeholders for the instructor kit scripts |

## Reused code

Config loading, LLM client shape, tool-runner dispatch, and the JSONL tracer pattern are adapted
from our Assignment 2 triage agent. Built with AI assistance (Claude Code); disclose per course policy.
