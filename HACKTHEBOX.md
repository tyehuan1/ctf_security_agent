# Running a Hack The Box challenge (manual control plane)

Game-day runbook for the agent against a live HTB instance **with the MCP server disabled** — the
fully offline-from-the-platform path. The controller stages your downloaded files into the solver
workspace, starts a forwarder so the remote is reachable as `target`, runs the air-gapped solver,
and surfaces the candidate flag for you to submit on HTB. Nothing in the sandbox changes vs. a
CTFTiny run; only *where the endpoint and files come from* changes.

## Add the next challenge yourself (3 steps)

1. **Download the challenge files** into their own folder under `external/hackthebox/`:

   ```
   external/hackthebox/<challenge_name>/   <- put all provided files here (nested dirs are fine)
   ```

2. **Spawn the HTB instance** and note its `IP:PORT` (e.g. `154.57.164.83:31763`).

3. **Run it (Haiku first — cheap):**

   ```
   python run_htb.py --platform manual --flag-mode manual \
       --files-dir "external/hackthebox/<challenge_name>" \
       --category <pwn|rev|crypto|web|stego|hardware|misc> \
       --target <IP>:<PORT>
   ```

   Arm **2.2.0** (Claude Haiku 5.5 + full tooling) is the default. `--category` sets the per-attempt
   allowance (pwn/rev → 40 turns, others → 30; see below). The candidate flag prints as a block and
   is appended to `hackthebox_runs/htb_candidates.txt` (and `.jsonl`). **You** submit it on HTB.

   The run prints a header line confirming what it's using, e.g.
   `arm=2.2.0 model=claude-haiku-5-5 category='pwn' (long (pwn/rev) budget: 40 turns, 1200s, 600,000 tokens)`.

## Model routing — escalate a stuck challenge to Sonnet

Run every challenge on **Haiku 5.5 first** (the default, ~$0.02/attempt). A failed Haiku pass is
nearly free, so escalate to **Claude Sonnet 5.5** only on the ones Haiku can't crack — same sandbox,
same prompt, same five tools, stronger model (~$0.35–0.9/attempt):

```
python run_htb.py --platform manual --flag-mode manual \
    --files-dir "external/hackthebox/<challenge_name>" \
    --category <cat> --target <IP>:<PORT> \
    --prompt 2.2.0-sonnet
```

That's the whole routing workflow: **default = Haiku, backup arm = `2.2.0-sonnet`.** No image
rebuild — the Sonnet arm reuses `ctf-solver-sandbox:v2.2`; only the model id changes, controller-side.
Cost accounting prices the run at Sonnet rates automatically (the `cost_usd` in `htb_results.jsonl`
is correct for whichever model ran).

## Selective turn / timeout bump (per category)

`--category` picks the per-attempt budget (HTB runs only — the scored CTFTiny path is untouched):

| Category | Turns | Deadline | Token cap |
|---|---|---|---|
| `pwn`, `rev`/`reversing` | **40** | 20 min | 600K |
| everything else (`crypto`, `web`, `stego`, `hardware`, `misc`, …) | **30** | 15 min | 450K |

pwn and reversing exhaust their budget far more often (measured on Eval Set 3), so they get the
longer allowance; leaner categories save budget. `--max-turns`, `--deadline-s`, and `--token-cap`
each override the matching field when you pass them explicitly.

### File-only challenge (no IP:PORT)

Many challenges (crypto, rev, forensics, a logic/hardware capture) ship only files with no live
service. **Just omit `--target`** — no forwarder is started, and the solver works from the staged
files on an air-gapped network:

```
python run_htb.py --platform manual --flag-mode manual \
    --files-dir "external/hackthebox/<challenge_name>"
```

(With no service there is no `egress_check`; there is nothing to connect to.)

## Re-pointing when the instance respawns

`--target` and `--files-dir` are plain parameters — when HTB respawns the instance on a new
`IP:PORT`, just re-run with the new value. Nothing else changes.

## The toggles

| Flag | Values | Meaning |
|---|---|---|
| `--platform` | `manual` (default) · `mcp` | Where the endpoint/files come from. `manual` = your `--files-dir` + `--target`, zero MCP calls. `mcp` = HTB MCP client (wired; needs the token joined to the event). |
| `--flag-mode` | `manual` (default) · `auto` | `manual` = surface the candidate for you to submit. `auto` = submit via the controller MCP client. |
| `--prompt` | `2.2.0` (default, Haiku 5.5) · `2.2.0-sonnet` (backup, Sonnet 5.5) · `2.1.0` · `1.1.0` · `1.0.0` · `baseline` | Solver arm. **Default `2.2.0`** = Haiku 5.5 + full tooling (image `ctf-solver-sandbox:v2.2`). **`2.2.0-sonnet`** = the escalation arm (same sandbox/prompt, Sonnet 5.5). |
| `--category` | `pwn` · `rev` · `crypto` · `web` · `stego` · `hardware` · `misc` · … | Sets the per-attempt turn/timeout/token allowance (pwn/rev get the long tier). Default `hackthebox` → the lean tier. |
| `--max-turns` / `--deadline-s` / `--token-cap` | int | Per-run budget override; wins over the per-category default. Frozen scored-path defaults untouched. |

- **Fully offline run** (MCP down): `--platform manual --flag-mode manual` (the defaults).
- **Fully automatic**: `--platform mcp --flag-mode auto --challenge-id <HTB id>` = start / endpoint /
  download / submit all through MCP, no human step (the candidate is still written to the files).
- **Mixed**: `--platform mcp --flag-mode manual --challenge-id <HTB id>` = MCP starts + gets the
  endpoint and files, you still submit by hand.

## MCP mode (wired)

The HTB MCP client is wired against `https://mcp.hackthebox.ai/v1/ctf/mcp/` (streamable HTTP, auth
`Bearer $MCP_TOKEN` from `.env`). The token stays controller-side (redacted from traces, never given
to the solver). Confirmed tools: `retrieve_ctf`, `start_container`, `stop_container`,
`container_status`, `submit_flag`, `get_download_link`, `retrieve_ctf_scores`.

```
python run_htb.py --platform mcp --flag-mode auto --challenge-id <HTB challenge id>
```

In MCP mode the controller starts the container, reads host/port from `container_status`, downloads
the files via `get_download_link` (controller has egress; the solver does not) and stages them, then
the forwarder makes it reachable as `target` — same solver path as manual mode. On teardown it stops
the HTB container.

> **One-time prerequisite:** your MCP token must be *joined* to the event. Until then
> `retrieve_ctf` / `start_container` return `403 Forbidden`. A connection check (`tools/list`) works
> regardless. Join the event (e.g. "MCP TryOut") on the HTB site or via `join_ctf_event`, then MCP
> mode is live. Response field names are parsed defensively — the first real `start_container` /
> `submit_flag` responses confirm host/port and verdict fields.

## Competition setup & budget ($25 key)

Before game day:

1. **Point the controller at the competition endpoint.** The model API base URL lives in
   `config/settings.json` (`model.base_url`); the token lives in `.env` as `LITELLM_TOKEN` (secret,
   redacted from traces, never given to the solver). Set both to the key/endpoint you were issued.
2. **Confirm both models are served** by that endpoint: `claude-haiku-5-5` (default) and
   `claude-sonnet-5-5` (backup arm). If the gateway uses different ids, set `LITELLM_MODEL` to
   override, or tell me and I'll repoint the `2.2.0-sonnet` arm.
3. **Budget math (measured on Eval Set 3).** Haiku ≈ **$0.02/attempt**; Sonnet ≈ **$0.34/attempt**
   at 30 turns, **~$0.45–0.52** at 40 turns (pwn/rev). A sane $25 plan: run all 10 on Haiku first
   (~$0.20 total), then escalate the ~3–5 hardest to Sonnet with a couple of retries each
   (~$7–15). That leaves comfortable headroom. The per-attempt `attempt_token_cap` is the hard
   guardrail so no single run can drain the key.
4. **Cost tracking.** Every run records `cost_usd` in `hackthebox_runs/htb_results.jsonl`, priced
   from the gateway's cost header when present (else the first-party Anthropic fallback rates, which
   are model-correct). Sum that column to watch spend:
   `python -c "import json;print(sum(json.loads(l)['cost_usd'] or 0 for l in open('hackthebox_runs/htb_results.jsonl')))"`.

## Isolation guarantees (checked every run)

- `isolation_check` — the forwarder isn't privileged, publishes no ports, mounts nothing, no docker.sock.
- `egress_check` — proven **inside the live solver sandbox**: `target` is reachable, **general
  outbound (e.g. 1.1.1.1:53) fails**. If the sandbox ever has general egress the run aborts with a
  `scope_violation`. The solver reaches the remote only through `target`; it never calls HTB directly.

## Where things land

- `hackthebox_runs/htb_candidates.txt` + `.jsonl` — the candidate flags (human + machine).
- `hackthebox_runs/htb_results.jsonl` — per-attempt record (status, candidate, tokens, cost).
- `hackthebox_runs/traces/` — full traces. All of `hackthebox_runs/` is gitignored (it holds live flags).

## Verified example (Try Out instance)

`hardware_its_oops_pm` against `154.57.164.83:31763`, `--prompt 1.1.0`: the solver read `backdoor.vhdl`,
found the trigger pattern, connected via `pwn.remote('target', 31763)` through the forwarder, and the
service returned `HTB{4_7yp1c41_53cu23_TPM_ch1p}` — surfaced as a candidate (not auto-submitted).
egress_check: target reachable, general egress blocked.
