"""Controller-side flag handling: how a solver's candidate flag is validated and recorded.

This is the one place the controller decides what to do with a candidate flag. The solver never
participates: it reports a candidate through ``submit_flag`` exactly as before, and never sees the
answer key, the MCP client, or any token. Three modes (``settings["flag"]["mode"]``):

  - ``local``  : CTFTiny scored runs. Exact-match against the answer key via the bound
                 ``verify_privately``. Unchanged, frozen behavior.
  - ``manual`` : HTB competition. No local answer key; capture the candidate, surface it in an
                 easy-to-read form for a human to submit on HTB, and leave verification pending.
  - ``auto``   : HTB competition. Submit the candidate to HTB through the controller's MCP client;
                 HTB's immediate scoring is the verification result.

``manual`` is the default HTB behavior (the auto-submit path is built but off); ``auto`` is the
opt-in. The MCP submission lives entirely in the trusted controller (see ``htb_mcp``); the
air-gapped solver sandbox is untouched and still reaches only the forwarded target endpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

# HTB-only terminal status for an attempt that produced a candidate we did not auto-verify. It is
# NOT an A4 result status and must never reach make_result()/results.jsonl; the HTB record path in
# runner.run_attempt handles it. See common.schemas.STATUSES for the frozen A4 vocabulary.
HTB_CANDIDATE = "candidate"


@dataclass
class SubmitResult:
    """Outcome of handing a candidate to an external scorer (HTB via MCP)."""
    verified: Optional[bool]     # True accepted, False rejected, None undetermined / not submitted
    submitted: bool              # whether a submission actually reached the scorer
    source: str                  # "local" | "htb-mcp" | "htb-manual"
    detail: str = ""             # human-readable note (HTB message, error, ...)


@dataclass
class GatewayOutcome:
    """What the attempt loop consumes after a candidate is handled."""
    verified: Optional[bool]     # True -> solved; False -> wrong; None -> undetermined
    pending: bool                # True -> candidate captured; stop the attempt for human submission
    reason: str                  # terminal reason string for the trace
    source: str                  # provenance, mirrors SubmitResult.source
    submitted: bool = False
    detail: str = ""


@runtime_checkable
class FlagSubmitter(Protocol):
    """A scorer the controller can submit a candidate to (implemented by htb_mcp.HTBMcpSubmitter)."""

    def ready(self) -> bool:
        ...

    def submit(self, flag: str) -> SubmitResult:
        ...


class CandidateLog:
    """Surfaces and records every HTB candidate: a console block, htb_candidates.txt, and
    htb_candidates.jsonl. Never used in local mode (that path verifies privately instead)."""

    def __init__(self, htb_dir: Path, challenge_id: str, run_id: str, flag_mode: str,
                 *, echo: Optional[Callable[[str], None]] = print,
                 challenge_name: Optional[str] = None):
        self.dir = Path(htb_dir)
        self.challenge_id = challenge_id
        self.challenge_name = challenge_name or challenge_id
        self.run_id = run_id
        self.flag_mode = flag_mode
        self._echo = echo
        self.txt_path = self.dir / "htb_candidates.txt"
        self.jsonl_path = self.dir / "htb_candidates.jsonl"

    def _status_line(self, submitted: bool, verified: Optional[bool], detail: str) -> str:
        if not submitted:
            return "NOT submitted (manual mode; auto-submit OFF)"
        if verified is True:
            return "submitted to HTB -> ACCEPTED" + (f" ({detail})" if detail else "")
        if verified is False:
            return "submitted to HTB -> REJECTED" + (f" ({detail})" if detail else "")
        return "submitted to HTB -> verdict undetermined" + (f" ({detail})" if detail else "")

    def _block(self, candidate: str, reason: str, reported: str,
               submitted: bool, verified: Optional[bool], detail: str) -> str:
        bar = "=" * 59
        return "\n".join([
            bar,
            "  CANDIDATE FLAG (HTB %s)" % self.flag_mode,
            "  run       : %s" % self.run_id,
            "  challenge : %s" % self.challenge_id,
            "  candidate : %s" % candidate,
            "  reported  : %s" % reported,
            "  reason    : %s" % (reason or "(none given)"),
            "  status    : %s" % self._status_line(submitted, verified, detail),
            bar,
        ])

    def record(self, candidate: str, *, reason: str = "", submitted: bool = False,
               verified: Optional[bool] = None, detail: str = "") -> dict:
        reported = datetime.now().isoformat(timespec="seconds")
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "reported": reported,
            "run_id": self.run_id,
            "challenge_id": self.challenge_id,
            "challenge_name": self.challenge_name,
            "flag_mode": self.flag_mode,
            "candidate": candidate,
            "reason": reason,
            "submitted": submitted,
            "verified": verified,
            "detail": detail,
        }
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        block = self._block(candidate, reason, reported, submitted, verified, detail)
        with self.txt_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(block + "\n\n")
        if self._echo:
            self._echo("\n" + block)
        return rec


class FlagGateway:
    source = "base"

    def handle(self, candidate: str, *, reason: str = "") -> GatewayOutcome:
        raise NotImplementedError


class LocalGateway(FlagGateway):
    """CTFTiny scored path: exact-match against the answer key. Behavior is identical to calling
    the bound verify_privately directly, so frozen runs are unchanged."""
    source = "local"

    def __init__(self, verify: Callable[[str], object]):
        self._verify = verify

    def handle(self, candidate: str, *, reason: str = "") -> GatewayOutcome:
        ok = bool(self._verify(candidate).verified)
        return GatewayOutcome(verified=ok, pending=False, source="local",
                              reason="verified flag" if ok else "wrong flag")


class ManualHTBGateway(FlagGateway):
    """HTB default: capture the candidate and surface it for a human to submit. Never auto-submits."""
    source = "htb-manual"

    def __init__(self, log: CandidateLog):
        self._log = log

    def handle(self, candidate: str, *, reason: str = "") -> GatewayOutcome:
        self._log.record(candidate, reason=reason, submitted=False, verified=None,
                         detail="auto-submit OFF")
        return GatewayOutcome(verified=None, pending=True, source="htb-manual", submitted=False,
                              reason="candidate captured for manual submission")


class AutoHTBGateway(FlagGateway):
    """HTB opt-in: submit the candidate via the controller's MCP client and use HTB's scoring."""
    source = "htb-mcp"

    def __init__(self, submitter: FlagSubmitter, log: CandidateLog):
        self._submitter = submitter
        self._log = log

    def handle(self, candidate: str, *, reason: str = "") -> GatewayOutcome:
        res = self._submitter.submit(candidate)
        self._log.record(candidate, reason=reason, submitted=res.submitted,
                         verified=res.verified, detail=res.detail)
        if res.verified is None:
            # Undetermined (e.g. a submission error): don't count it wrong; capture for a human.
            return GatewayOutcome(verified=None, pending=True, source="htb-mcp",
                                  submitted=res.submitted, detail=res.detail,
                                  reason="HTB submission undetermined; captured for manual check")
        return GatewayOutcome(verified=res.verified, pending=False, source="htb-mcp",
                              submitted=res.submitted, detail=res.detail,
                              reason="HTB accepted the flag" if res.verified else "HTB rejected the flag")


def build_gateway(mode: str, *, verify: Callable[[str], object], htb_dir: Path,
                  challenge_id: str, run_id: str, challenge_name: Optional[str] = None,
                  submitter: Optional[FlagSubmitter] = None,
                  echo: Optional[Callable[[str], None]] = print) -> FlagGateway:
    """Pick the gateway for the active flag mode. ``submitter`` is injected in tests; in production
    the auto path builds the HTB MCP submitter lazily (see htb_mcp)."""
    if mode == "local":
        return LocalGateway(verify)
    log = CandidateLog(htb_dir, challenge_id, run_id, mode, echo=echo, challenge_name=challenge_name)
    if mode == "manual":
        return ManualHTBGateway(log)
    if mode == "auto":
        if submitter is None:
            from controller.htb_mcp import build_default_submitter
            submitter = build_default_submitter(challenge_id)
        if not submitter.ready():
            raise RuntimeError(
                "flag mode 'auto' selected but the HTB MCP submitter is not wired yet. Provide the "
                "HTB MCP server endpoint, transport, and submit tool name to enable auto-submit, or "
                "run with flag mode 'manual' to capture candidates for manual submission.")
        return AutoHTBGateway(submitter, log)
    raise ValueError(f"unknown flag mode {mode!r}")
