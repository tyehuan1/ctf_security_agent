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

> **Baseline vs. improvement arms.** The table below is the **frozen baseline, `v1.0.0`** (git tag
> `v1.0.0`, sandbox image `ctf-solver-sandbox:v1`): the configuration that produced the scored
> `results.jsonl`. `git checkout v1.0.0` reproduces this column exactly. The current working tree
> carries a post-freeze **improvement arm, `v1.1.0`** (`agent_version 1.1.0` in
> `config/settings.json`, image `ctf-solver-sandbox:v1.1`), described under
> [Improvement arms](#improvement-arms-post-freeze); the baseline image and its scored results are
> left untouched. Each arm is one fixed `config_id = "agent"` configuration; only one is active at a
> time (whatever the working tree / checked-out tag points to).

| Item | Value |
|---|---|
| Agent version | `1.0.0` (`agent_version` in `config/settings.json`); see the change log |
| Agent commit | `b57394d3a0c57808b918b660478d4e3df0fd986e` (tag `v1.0.0`) |
| Model ID | `claude-haiku-5-5` (exact id from `GET /v1/models`); chosen at freeze after pilot 8 (haiku solved 4/5 development challenges vs 1/5 for GPT 4.1 Mini under the same prompt) |
| Route | Duke AI Gateway (LiteLLM, OpenAI-compatible) `https://litellm.oit.duke.edu/v1/chat/completions`, native tool calling, Duke VPN |
| Settings | temperature 0.6, per-turn model time budget 200 s incl. retries and 429 waits; each HTTP request cut off at 60 s (`request_timeout_s`) so a hung request leaves time for a retry (`config/settings.json`) |
| Retry rules | ordinary failures: at most 2 tries, 2 s apart, no retry on 400/401/403/404/422. HTTP 429 (gateway per-key limit, 40K TPM / 20 RPM): wait `Retry-After` or 65 s, up to 3 times, only if the wait fits the turn's time; each wait traced as `rate_limit_wait`. A 429 that survives is `infrastructure_error`. An attempt is never re-run — a recorded trial slot is final |
| Pacing | attempts strictly sequential; before each, a 1-token probe waits until the key's TPM/RPM window has cleared (fixed sleep from the last attempt's tokens if the headers are missing). Probes are batch overhead, not attempt usage |
| Limits (controller-enforced) | max_turns 25 (one turn = one action; a no-tool nudge is an extra model call in the same turn), max_chars 3000 per tool output (non-printable bytes shown as `.`, then head+tail cap, applied before the model sees it), tool_timeout_s 45, attempt_deadline_s 600 (hard: runaway calls are cut off and the solver killed), attempt_token_cap 400000 |
| Context | only the last 6 tool results are sent in full each turn; older ones are replaced by a short stub (the full history stays in the transcript) |
| Usage | input/output tokens from each response's `usage`. `cost_usd` from the gateway's `x-litellm-response-cost` header; when missing, computed from tokens at the published rates in `model.pricing` ($0.10 / $0.01 cached / $0.50 per 1M). The source of each call's cost (`header`, `rate`, or `rate_estimate` when cached tokens are unreported) is in the trace only, not in `results.jsonl`; rate-computed costs are estimates |
| Prompts | `src/solver/prompts.py`, `PROMPT_VERSION = ctf-solver-1.0.0` (matches `agent_version`). Every tool the model sees has a required `hypothesis` argument (one sentence: current hypothesis and why this action tests it); `submit_flag` has a required `reason`. The agent removes it before the tool runs and records it in the trace's `model_response.content` |
| Submissions | the solver submits the flag-format substring of its candidate (`flag{...}`, `FLAG{...}`, `csawctf{...}`, through the last `}`) when one is present, otherwise the candidate unchanged; the trace keeps the unextracted string as `raw_flag` |
| Tools / versions | `src/tools/` (run_shell, inspect_file, python_exec, connect_endpoint, submit_flag); sandbox image `ctf-solver-sandbox:v1`, id `sha256:a5fc9d1e674cb3ff7084e0141325fdd9414869823eadc7e8c5f8f20020511d58`, pwntools 4.15.0, pycryptodome, requests, sympy, Debian binwalk 2.4.3, curl 8.14.1 |
| Isolation | sanitized per-attempt compose (no docker.sock, privileged, cap_add, published ports); internal per-attempt network; target alias `target` |
| Dependencies | `requirements.txt` (pip freeze, Python 3.11.9) |

## Improvement arms (post-freeze)

Each arm is a self-contained `config_id = "agent"` configuration built on the frozen baseline. It
gets its own `agent_version`, `PROMPT_VERSION = ctf-solver-<agent_version>`, and sandbox image tag,
so a trace's `attempt_started` (which embeds the full settings and `agent_version`) is enough to
tell which arm produced any result. The baseline (`v1.0.0`, image `ctf-solver-sandbox:v1`,
id `sha256:a5fc9d1e67…`) is never rebuilt in place.

| Arm | Delta from baseline | Sandbox image |
|---|---|---|
| **`v1.1.0` — imaging + OCR** | Adds image-handling and OCR so the solver can decode/manipulate a *revealed* image and read a flag drawn in it as short text, instead of dumping the pixels as ASCII art (which the `max_chars` cap truncates, making the flag unrecoverable). Built from a **separate** Dockerfile [`sandbox/Dockerfile.imaging`](sandbox/Dockerfile.imaging) (baseline `sandbox/Dockerfile` untouched). New prompt paragraph (general to visual/stego): when a flag appears drawn/rendered in an image, use Pillow + numpy to combine/XOR/bit-plane the image and read the result with `pytesseract` OCR (or `pyzbar` for a QR/barcode) rather than printing ASCII art. Model, budgets (`max_turns 25`, caps), tools, and isolation are unchanged. | `ctf-solver-sandbox:v1.1`, id `sha256:beb66bc273a906d6c650819069b6c9a8ec5c8db6efeed624dfb90fd8aa4a0413`. Baseline libs + **Pillow 13.0.0, numpy 2.4.6, pytesseract 0.3.13** (engine **tesseract 5.5.0**, apt `tesseract-ocr`), **pyzbar 0.1.9** (apt `libzbar0`). Build: `docker build -f sandbox/Dockerfile.imaging -t ctf-solver-sandbox:v1.1 sandbox/` |

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

### Selecting a configuration arm

`CTF_ARM` picks the arm for a launch (unset = `config/settings.json`'s `agent_version`, currently
`1.1.0`). It sets the sandbox image and whether the prompt carries the imaging/OCR guidance, and is
recorded in each trace's `attempt_started` (`agent_version`, `code.commit`, and `settings.sandbox`),
so a result self-identifies its arm. Write each arm to its own labeled files so arms never mix:

```powershell
# Baseline v1.0.0 (image ctf-solver-sandbox:v1, no imaging prompt)
$env:CTF_ARM='1.0.0'
.venv\Scripts\python.exe run_attempt.py cry-perfect-secrecy 1 --results results.v1.0.0.jsonl --traces traces.v1.0.0 --label arm-v1.0.0

# Imaging + OCR arm v1.1.0 (image ctf-solver-sandbox:v1.1, imaging prompt)
$env:CTF_ARM='1.1.0'
.venv\Scripts\python.exe run_attempt.py cry-perfect-secrecy 1 --results results.v1.1.0.jsonl --traces traces.v1.1.0 --label arm-v1.1.0

Remove-Item Env:CTF_ARM   # back to the settings.json default
```

`--label` tags the `run_id` and trace filename (`agent-arm-v1-1-0-cry-perfect-secrecy-t1-…`).
Summarize an arm by pointing the summarizer at that arm's results/traces:
`ctf_single_agent_summary.py … --results results.v1.1.0.jsonl --csv per_challenge.v1.1.0.csv`.
(The baseline arm, byte-for-byte, is also reproducible with `git checkout v1.0.0`.)

The first measured arm run — `cry-perfect-secrecy` ×3 under `v1.1.0`
(`results.v1.1.0.jsonl` / `traces.v1.1.0/` / `per_challenge.v1.1.0.csv`) — solved **3/3**, where the
frozen `v1.0.0` baseline was **0/3** (`budget_exhausted`): the imaging+OCR capability let the solver
read the flag drawn in the XOR-recovered image instead of exhausting its turns on ASCII art.

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
| 0.2.0 | On reverse-engineering challenges the solver submits strings it found inside the binary (rev-rock: `flag{11HighTemplar11DarkTemplar}`, reason: "fits the flag format and is plausible"). | The model treats `strings` output as the answer instead of running the program; a string inside a binary is usually an expected input, a message, or a decoy. | Tell the agent, for rev and pwn challenges, to run the program (`run_shell`, or pwntools `pwn.process` for multi-step I/O), test the strings it found as inputs, and submit only a flag the program printed or that it derived from the program's logic. | Pilot 5 (one trial each): the decoy submission stopped — rev-rock ran the binary with several inputs and submitted nothing rather than a found string. rev-checker was solved by reversing its encode function (the project's first verified solve). Still 0/4 on the harder challenges, but four hit the 12-turn cap with real progress (bricks found the IV hint; warmup built the overflow; mfw found the LFI), so the turn cap, not guessing, is now the binding limit. |
| 0.3.0 | On rev-rock the solver runs the binary but only ever pipes the input to standard input, repeating that one mechanism (lengths 30/31/32/33, then `flag{rock}`) until it runs out of turns; rock actually reads its input as a command-line argument. | The model assumes a program reads stdin and never tries the other ways a program can take input, so it gets stuck when stdin is ignored. | Tell the agent that programs take input in different ways — command-line argument, standard input, a file, an environment variable — and that if one mechanism does nothing or keeps printing the same thing it should switch to another rather than repeat it. | Pilot 6: worked — rev-rock now passes the input as a command-line argument (`/workspace/rock AAAA…` at several lengths) instead of only piping to stdin, then moves on to pwntools. The stuck-on-stdin loop is gone. Still no solve in 12 turns (it brute-forces argument lengths rather than reading the length check out of the binary). |
| 0.3.0 | On the crypto challenge (bricks_of_gold) the solver finds the IV hint `THE_SECRET_IV=CASH` early but, on its last turn, retypes the ciphertext hex from an `xxd` dump, gets a non-hex character, and the decryption attempt crashes with no budget left to retry. | The model transcribes bytes by hand from a hex dump instead of reading the file, and a single typo wastes the attempt. | Tell the agent, for cryptography and any raw-byte work, to read the file directly in python (`open(path,'rb').read()`) and never retype hex or bytes from a dump, doing the read and the decode in one `python_exec` call. | Pilot 6: worked — bricks_of_gold now reads the ciphertext with `open(path,'rb').read()` and runs real AES decryption (it decrypted the first blocks); the last-turn hex-transcription crash did not recur. Still ran out of turns on recon plus trial-and-error over the cipher mode, so no solve. |
| 0.3.0 | On the web challenge (MFW) the solver finds the LFI bug, the empty `flag.php`, and the `.git` clue, but never sends a single HTTP request to the live target before the turn cap — it only reads the provided source. | The model treats the provided source as the place the flag lives and does not know to exploit the running service, nor which web tactics to try. | Tell the agent to drive live web services from `python_exec` with the `requests` library; clarify that provided files are for finding the bug but the flag is usually on the running target (while a files-only challenge keeps the flag in the files); and list concrete tactics — `php://filter` for LFI, `/.git/` when Git is mentioned, code injection for `assert()`/`eval()`, and to consider SQLi, SSRF and path traversal. | Pilot 6: worked — MFW now sends real HTTP requests to the live target (`requests.get('http://target:8000/')` returns the page) and tries LFI and path-traversal variants (`?page=../flag`, URL-encoded) against the running server, where pilot 5 made no request at all. Two turns were still wasted calling `curl` (not installed in the sandbox) and it did not reach `php://filter` before the 12-turn cap, so no solve yet. |
| 0.4.0 | On web challenges the solver repeatedly reaches for `curl` (e.g. `curl -s http://target:8000/`), which is not installed in the sandbox, wasting one or more turns on "curl: not found" before falling back to the `requests` library. | `curl` is the reflexive tool for an HTTP request, and the sandbox image did not provide it; a scarce turn is lost each time the model has to discover that and switch. | Add `curl` to the solver sandbox image (`sandbox/Dockerfile`), a new program in the sandbox — it is the natural tool for probing a web service and saves the wasted turns, while `requests` in `python_exec` remains available for scripted exploitation. | Pilot 7 (web only): `curl` is now installed in the image (8.14.1, verified). This trial did not exercise it — the model reached the live service with `connect_endpoint` instead of `curl` or `requests` and made no "curl: not found" error, so no turns were lost to it, but the saved-turns benefit was not demonstrated either (run-to-run variance at temperature 0.6). It spent most turns on recon and ended on a `connect_endpoint` timeout; still no solve. Needs more web trials to confirm. |
| 0.5.0 | With GPT 4.1 Mini the solver now does the right kind of work in every category but still solves only 1/5 development challenges — it runs out of its 12 turns with real progress (built the overflow, found the LFI, found the IV hint) but does not finish. Turns, not approach, are the binding limit. | A stronger model reasoning within the same harness may actually finish these, and most challenges stop as soon as the flag is found, so more turns cost little. | Freeze (v1.0.0): switch the model to `claude-haiku-5-5` (same prompt and tools, chosen by pilot), raise `max_turns` 12 → 25, and raise `attempt_token_cap` 200000 → 400000 to fit the heavier per-attempt token use. | Pilot 8 (one trial each, same prompt): haiku solved **4/5** — rev-checker, rev-rock, warmup and MFW — vs 1/5 for Mini; only bricks_of_gold (crypto) remained unsolved. Haiku uses ~2–3× the tokens per attempt (mean 86.8K in / 6.7K out) at a similar or lower cost (~$0.006/attempt) because its rates are ~4× cheaper. |
| 1.0.0 | On the scored set, `cry-perfect-secrecy` is 0/3 although the agent's reasoning is right: it identifies one-time-pad key reuse and XORs the two PNGs to recover the image, but the sandbox has no image library, so it decodes the PNG by hand and renders the result as ASCII art. That overflows the `max_chars` tool-output cap; working around the truncation burns its remaining turns and the flag (drawn in the revealed image) is never read. | The agent has the right method but no way to *read* a flag that is drawn in an image — it can only dump pixels as text, which the output cap mangles. Giving it image manipulation plus OCR lets it extract the flag as a short string instead. | Post-freeze **imaging + OCR arm (v1.1.0)**, a separate configuration leaving the frozen baseline untouched: add Pillow + numpy (image handling), pytesseract/tesseract (OCR) and pyzbar (QR/barcode) to a new sandbox image `ctf-solver-sandbox:v1.1` (own Dockerfile), and add a general visual/stego prompt line telling the agent to manipulate a revealed image with the image library and read it with OCR rather than printing ASCII art. | Capability verified in-container: tesseract 5.5.0, Pillow 13.0.0 and numpy 2.4.6 import; a round-trip test renders text to an image and OCRs it back, and an image XOR recovers the key. The solve improvement on `cry-perfect-secrecy` itself is **not yet measured** — pending a pilot/re-run of this arm. |

## Reused code and AI assistance

Config loading, LLM client shape, tool-runner dispatch, and the JSONL tracer pattern are adapted
from our Assignment 2 triage agent. Built with AI assistance (Claude Code); disclose per course policy.
