# ctf_security_agent

CTF benchmarking agent for CYBERSEC 511 Assignment 4 (Appendix A Implementation Guide).
One agent configuration (`config_id = "agent"`), three fresh attempts per challenge, 10 CTFTiny
challenges (`evaluation_ids.txt` → `evaluation_manifest.json`), 30 planned attempts.
`CLAUDE.md` lists the invariants every change must keep.

## Status

| Part | State |
|---|---|
| Controller: loader, private verifier (bound one-argument `verify_privately`), sanitized targets, budgets with hard deadline, A4 records + JSONL traces | done, offline-tested |
| `make_manifest.py`, `run_batch.py`, `ctf_single_agent_summary.py` | done, offline-tested |
| Solver: tools, system prompt, gateway client (`src/solver`, `src/tools`) | **not built** — stubs |

`run_batch.py` and `run_attempt.py` cannot produce live results until the solver exists.

## Clean setup (Windows, trusted controller)

```powershell
python -m venv .venv                                            # Python 3.11
.venv\Scripts\python.exe -m pip install -r requirements.txt     # pinned; includes nyuctf==1.1.1
docker version; docker compose version                          # the server must respond
```

Data (pinned commits recorded in `resources.txt`; Windows checkout notes there too):

```bash
git clone https://github.com/NYU-LLM-CTF/NYU_CTF_Bench.git external/NYU_CTF_Bench
git clone https://github.com/NYU-LLM-CTF/CTFTiny.git external/CTFTiny
git -C external/NYU_CTF_Bench rev-parse HEAD   # 4c5744f55a31a58e03d6118c9dbb1c18e5bbdfa9
git -C external/CTFTiny rev-parse HEAD         # f1c9531672c45b24b7fb5f3aa44a7ac33d3602f8
```

Secrets: `LITELLM_TOKEN=...` in `.env` (gitignored, never logged; traces redact it).
The Duke AI Gateway is reachable only from the Duke network — connect the **Duke VPN** first.

Readiness and offline tests:

```powershell
.venv\Scripts\python.exe check_ready.py
.venv\Scripts\python.exe -m unittest -v test_helpers test_single_agent_summary
.venv\Scripts\python.exe -m pytest -q
```

## Frozen configuration: `config_id = "agent"`

Record these with the results; do not change them between trials or challenges.

| Item | Value |
|---|---|
| Agent commit | `git rev-parse HEAD` at launch (record in the writeup) |
| Model ID | `GPT 4.1 Mini` (exact id from `GET /v1/models`, 2026-11-08) |
| Route | Duke AI Gateway (LiteLLM, OpenAI-compatible) `https://litellm.oit.duke.edu/v1/chat/completions`, native tool calling, Duke VPN |
| Settings | temperature 0.6, per-turn model time budget 200 s incl. retries and 429 waits (`config/settings.json`) |
| Retry rules | ordinary failures: at most 2 tries, 2 s apart, no retry on 400/401/403/404/422. HTTP 429 (gateway per-key limit, 40K TPM / 20 RPM): wait `Retry-After` or 65 s, up to 3 times, only if the wait fits the turn's time; each wait traced as `rate_limit_wait`. A 429 that survives is `infrastructure_error`. An attempt is never re-run — a recorded trial slot is final |
| Pacing | attempts strictly sequential; before each, a 1-token probe waits until the key's TPM/RPM window has cleared (fixed sleep from the last attempt's tokens if the headers are missing). Probes are batch overhead, not attempt usage |
| Limits (controller-enforced) | max_turns 12 (one turn = one action; a no-tool nudge is an extra model call in the same turn), max_chars 3000 per tool output (non-printable bytes shown as `.`, then head+tail cap, applied before the model sees it), tool_timeout_s 45, attempt_deadline_s 600 (hard: runaway calls are cut off and the solver killed), attempt_token_cap 200000 |
| Context | only the last 6 tool results are sent in full each turn; older ones are replaced by a short stub (the full history stays in the transcript) |
| Usage | input/output tokens from each response's `usage`. `cost_usd` from the gateway's `x-litellm-response-cost` header; when missing, computed from tokens at the published rates in `model.pricing` ($0.40 / $0.10 cached / $1.60 per 1M). The source of each call's cost (`header`, `rate`, or `rate_estimate` when cached tokens are unreported) is in the trace only, not in `results.jsonl`; rate-computed costs are estimates |
| Prompts | `src/solver/prompts.py`, `PROMPT_VERSION = ctf-solver-v2` |
| Tools / versions | `src/tools/` (run_shell, inspect_file, python_exec, connect_endpoint, submit_flag); sandbox image `ctf-solver-sandbox:v1` (`docker image inspect` ID at freeze), pwntools 4.15.0, pycryptodome, requests, sympy, Debian binwalk 2.4.3 |
| Isolation | sanitized per-attempt compose (no docker.sock, privileged, cap_add, published ports); internal per-attempt network; target alias `target` |
| Dependencies | `requirements.txt` (pip freeze, Python 3.11.9) |

## Evaluation commands

```powershell
# 1. manifest (PowerShell: compute the commit first)
$c = git -C external/CTFTiny rev-parse HEAD
.venv\Scripts\python.exe make_manifest.py --dataset external/CTFTiny/ctftiny.json --ids-file evaluation_ids.txt --split evaluation --source-commit $c --out evaluation_manifest.json

# 2. the selected configuration: 3 x N attempts, resumable (once the solver is built)
.venv\Scripts\python.exe run_batch.py --manifest evaluation_manifest.json --plan
.venv\Scripts\python.exe run_batch.py --manifest evaluation_manifest.json

# 3. A6 metrics
.venv\Scripts\python.exe ctf_single_agent_summary.py --manifest evaluation_manifest.json --results results.jsonl --config agent --out summary.json --csv per_challenge.csv
```

`run_batch.py` skips trial slots already in `results.jsonl`, closes out a crashed attempt's trace
as `infrastructure_error` instead of retrying it, and holds a lock so two batches cannot run at once.
`--allow-synthetic` on the summarizer is for offline examples only; synthetic fixtures are never
measured results and must not be submitted.

## Layout

| Path | Role |
|---|---|
| `src/controller/loader.py` | `CTFDataset(dataset_json=...)`, `CTFChallenge(ds.get(key), ds.basedir)`, contestant export + hash inventory |
| `src/controller/verifier.py` | `verify_privately`; `bind_verifier(chal)` gives the loop's one-argument form |
| `src/controller/target.py` | sanitized per-attempt compose, start/stop/teardown checks, isolation inspection |
| `src/controller/budget.py` | frozen caps, `call_with_deadline` hard limit, head+tail truncation |
| `src/controller/record.py` | `results.jsonl` (A4 schema) and `traces/<run_id>.jsonl` |
| `src/controller/runner.py` | the A4 control flow for one attempt |
| `src/common/usage.py` | tokens + cost from gateway responses |
| `make_manifest.py`, `run_batch.py`, `ctf_single_agent_summary.py` | A5/A6 scripts |
| `test_helpers.py`, `test_single_agent_summary.py`, `tests/` | offline tests (synthetic data only) |

## Reused code and AI assistance

Config loading, LLM client shape, tool-runner dispatch, and the JSONL tracer pattern are adapted
from our Assignment 2 triage agent. Built with AI assistance (Claude Code); disclose per course policy.
