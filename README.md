# ctf_security_agent

CTF benchmarking agent for CYBERSEC 590 Assignment 4 (Appendix A Implementation Guide).
One agent configuration (`config_id = "agent"`), three fresh attempts per challenge, 10 CTFTiny
challenges (`evaluation_ids.txt` → `evaluation_manifest.json`), 30 planned attempts.
`CLAUDE.md` lists the invariants every change must keep.

## Status

| Part | State |
|---|---|
| Controller: loader, private verifier (bound one-argument `verify_privately`), sanitized targets, budgets with hard deadline, A4 records + JSONL traces | done, offline-tested |
| `make_manifest.py`, `run_batch.py`, `ctf_single_agent_summary.py` | done, offline-tested |
| Solver: tools, system prompt, gateway client (`src/solver`, `src/tools`) | done; verified on development pilots (`run_pilot.py`) |

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
| Agent version | `agent_version` in `config/settings.json` (0.2.0); see the change log |
| Agent commit | `git rev-parse HEAD` at launch (record in the writeup) |
| Model ID | `GPT 4.1 Mini` (exact id from `GET /v1/models`, 2026-11-08) |
| Route | Duke AI Gateway (LiteLLM, OpenAI-compatible) `https://litellm.oit.duke.edu/v1/chat/completions`, native tool calling, Duke VPN |
| Settings | temperature 0.6, per-turn model time budget 200 s incl. retries and 429 waits; each HTTP request cut off at 60 s (`request_timeout_s`) so a hung request leaves time for a retry (`config/settings.json`) |
| Retry rules | ordinary failures: at most 2 tries, 2 s apart, no retry on 400/401/403/404/422. HTTP 429 (gateway per-key limit, 40K TPM / 20 RPM): wait `Retry-After` or 65 s, up to 3 times, only if the wait fits the turn's time; each wait traced as `rate_limit_wait`. A 429 that survives is `infrastructure_error`. An attempt is never re-run — a recorded trial slot is final |
| Pacing | attempts strictly sequential; before each, a 1-token probe waits until the key's TPM/RPM window has cleared (fixed sleep from the last attempt's tokens if the headers are missing). Probes are batch overhead, not attempt usage |
| Limits (controller-enforced) | max_turns 12 (one turn = one action; a no-tool nudge is an extra model call in the same turn), max_chars 3000 per tool output (non-printable bytes shown as `.`, then head+tail cap, applied before the model sees it), tool_timeout_s 45, attempt_deadline_s 600 (hard: runaway calls are cut off and the solver killed), attempt_token_cap 200000 |
| Context | only the last 6 tool results are sent in full each turn; older ones are replaced by a short stub (the full history stays in the transcript) |
| Usage | input/output tokens from each response's `usage`. `cost_usd` from the gateway's `x-litellm-response-cost` header; when missing, computed from tokens at the published rates in `model.pricing` ($0.40 / $0.10 cached / $1.60 per 1M). The source of each call's cost (`header`, `rate`, or `rate_estimate` when cached tokens are unreported) is in the trace only, not in `results.jsonl`; rate-computed costs are estimates |
| Prompts | `src/solver/prompts.py`, `PROMPT_VERSION = ctf-solver-0.2.0` (matches `agent_version`). Every tool the model sees has a required `hypothesis` argument (one sentence: current hypothesis and why this action tests it); `submit_flag` has a required `reason`. The agent removes it before the tool runs and records it in the trace's `model_response.content` |
| Submissions | the solver submits the flag-format substring of its candidate (`flag{...}`, `FLAG{...}`, `csawctf{...}`, through the last `}`) when one is present, otherwise the candidate unchanged; the trace keeps the unextracted string as `raw_flag` |
| Tools / versions | `src/tools/` (run_shell, inspect_file, python_exec, connect_endpoint, submit_flag); sandbox image `ctf-solver-sandbox:v1` (`docker image inspect` ID at freeze), pwntools 4.15.0, pycryptodome, requests, sympy, Debian binwalk 2.4.3 |
| Isolation | sanitized per-attempt compose (no docker.sock, privileged, cap_add, published ports); internal per-attempt network; target alias `target` |
| Dependencies | `requirements.txt` (pip freeze, Python 3.11.9) |

## Pilot runs (verification only, not scores)

```powershell
.venv\Scripts\python.exe run_pilot.py --tag pilot3 --plan   # list
.venv\Scripts\python.exe run_pilot.py --tag pilot3          # one trial of each, sequential
```

`pilot_ids.txt` lists development challenges plus the CTFTiny `rev-checker` baseline; it refuses
any key in `evaluation_ids.txt`. Output goes to `results_<tag>.jsonl` and `traces_<tag>/`, kept out
of git and never mixed into `results.jsonl`.

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

## Agent improvement log

Significant changes to how the agent solves CTFs, one row per improvement (bug fixes are not
listed). **Version** is the `agent_version` the problem was seen in; the proposed changes ship
in the next version. `PROMPT_VERSION` is `ctf-solver-<agent_version>`.

| Version | Problem | Hypothesis | Proposed Changes | Result |
|---|---|---|---|---|
| 0.0.0 | Practice trials go over the Duke AI gateway’s 40k tokens-per-minute (TPM) rate limit, so runs error out. | The run is set to error out when a rate-limit error comes back, even though the per-minute window could be waited out. | Wait out rate-limit errors instead of failing (allow more time per attempt); before each attempt, send a small request to check the tokens left in the window, and don’t count that wait against the attempt. | Tests pass. The token-reduction changes below meant the rate limit wasn’t hit again in the next pilot. Will keep watching in case it comes back. |
| 0.0.0 | Trial runs use too many tokens, which drives up cost on the Duke AI gateway. | On the binary-reverse challenge, one turn returned a large block of binary characters that ballooned the input tokens for every turn after it. | Lower the max characters a tool can return; clean non-readable binary bytes before capture (and trace how many are replaced); replace tool results older than 6 turns with a short stub in the sent payload; tell the model to inspect binary files with inspect_file instead of cat or head. | Token use per trial dropped from ~70k–100k to ~31k–32k. Worked, but watch for cases where the agent needs an older tool result that’s now a stub. |
| 0.1.0 | The solver sometimes submits a wrong flag. | The model isn’t showing its reasoning each turn, so it tends to submit the first string that looks like a flag instead of stopping to interpret what it found. | Have the agent state a short hypothesis before each tool call, and a short reason before submitting, so it has to interpret the evidence instead of guessing. | Hypothesis and reasoning provide additional context for interpreting traces, but still struggling with reverse binary CTF decoys. |

## Reused code and AI assistance

Config loading, LLM client shape, tool-runner dispatch, and the JSONL tracer pattern are adapted
from our Assignment 2 triage agent. Built with AI assistance (Claude Code); disclose per course policy.
