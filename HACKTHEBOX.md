# Running a Hack The Box challenge (manual control plane)

Game-day runbook for the agent against a live HTB instance **with the MCP server disabled** — the
fully offline-from-the-platform path. The controller stages your downloaded files into the solver
workspace, starts a forwarder so the remote is reachable as `target`, runs the air-gapped solver,
and surfaces the candidate flag for you to submit on HTB. Nothing in the sandbox changes vs. a
CTFTiny run; only *where the endpoint and files come from* changes.

## Add the next challenge yourself (3 steps)

1. **Download the challenge files** into their own folder under `external/hackthebox/`:

   ```
   external/hackthebox/<challenge_name>/   <- put all provided files here (flat)
   ```

2. **Spawn the HTB instance** and note its `IP:PORT` (e.g. `154.57.164.83:31763`).

3. **Run it:**

   ```
   python run_htb.py --platform manual --flag-mode manual \
       --files-dir "external/hackthebox/<challenge_name>" \
       --target <IP>:<PORT>
   ```

   (Arm **1.1.0** (imaging+OCR) is the default for every HTB run; pass `--prompt <arm>` only to
   override.) The candidate flag prints as a block and is appended to `hackthebox_runs/htb_candidates.txt`
   (and `.jsonl`). **You** submit it on HTB. Done.

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
| `--platform` | `manual` (default) · `mcp` | Where the endpoint/files come from. `manual` = your `--files-dir` + `--target`, zero MCP calls. `mcp` = HTB MCP client (scaffold; not wired yet). |
| `--flag-mode` | `manual` (default) · `auto` | `manual` = surface the candidate for you to submit. `auto` = submit via the controller MCP client (scaffold; needs the HTB MCP submit tool wired). |
| `--prompt` | `1.1.0` (default) · `1.0.0` · `baseline` | Solver arm. **Every HTB run defaults to `1.1.0`** (imaging+OCR); pass this only to override. |
| `--max-turns` / `--token-cap` | int | Per-run budget override (frozen defaults untouched). |

- **Fully offline run** (MCP down): `--platform manual --flag-mode manual` (the defaults).
- **Fully automatic** (once MCP is wired): `--platform mcp --flag-mode auto` = enumerate/start/
  endpoint and submit all through MCP, no human step (the candidate is still written to the files).
- **Mixed** (once MCP is wired): `--platform mcp --flag-mode manual` = MCP gets the endpoint, you
  still submit by hand.

> `mcp` and `auto` both depend on the HTB MCP client, which is a scaffold today: provide the MCP
> server endpoint, transport, and tool names (`enumerate` / `start_instance` / `connection_info` /
> `submit_flag`) to light them up. `manual` + `manual` needs none of that.

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
