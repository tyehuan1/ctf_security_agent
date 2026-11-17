"""One attempt, following the Appendix A4 illustrative control flow:

    reset_solver_and_target()
    start_attempt_timer()
    observations = initial_contestant_context()
    while attempt_budget_remains():
        action, usage = request_next_action(observations)
        record_model_usage(usage)
        if action.is_flag_submission:
            observation = verify_privately(action.flag)
            record_submission(observation)
            if observation.verified:
                break
        else:
            observation = execute_scoped_tool_with_timeout(action)
            log_tool_result_and_usage(observation)
        observations.append(observation)
    stop_solver_and_record_terminal_status()

The controller owns resource accounting and verification. A Solver is any object with
reset(workspace, context), request_next_action(observations) -> (Action, usage),
execute(action, timeout_s) -> dict, and optionally kill(). Nothing it returns reaches Budget
or the target scope.

Hard wall-clock limit: every solver call (model request or tool) runs under call_with_deadline
with the time left in the attempt (and the per-tool timeout for tools). When it fires, the
controller calls solver.kill(), tears the target down, and records status "timeout".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol

from common import config
from common.schemas import SUCCESS_STATUS, make_result
from common.usage import NO_USAGE
from controller import loader
from controller.budget import AttemptBudget, Budget, DeadlineExceeded, call_with_deadline
from controller.flag_gateway import HTB_CANDIDATE, build_gateway
from controller.loader import ScopeViolation
from controller.record import Tracer, append_htb_result, append_result
from controller.target import Target, TargetError
from controller.verifier import bind_verifier


@dataclass
class Action:
    kind: str                          # "tool" | "submit_flag" | "give_up"
    tool: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    flag: Optional[str] = None

    @property
    def is_flag_submission(self) -> bool:
        return self.kind == "submit_flag"


class Solver(Protocol):
    def reset(self, workspace: Path, context: dict) -> None: ...
    def request_next_action(self, observations: list) -> tuple[Action, dict]: ...
    def execute(self, action: Action, timeout_s: float) -> dict: ...


class ToolError(RuntimeError):
    """A solver tool failed in a way the attempt cannot continue from (status tool_error)."""


def _kill(solver, tracer: Tracer, why: str) -> None:
    kill = getattr(solver, "kill", None)
    if callable(kill):
        try:
            kill()
            tracer("budget_stop", cap="deadline", action="solver killed", reason=why)
        except Exception as err:
            tracer("error", where="solver.kill", error=f"{type(err).__name__}: {err}")


class Attempt:
    """Per-attempt state; method names are the appendix's step names."""

    def __init__(self, challenge_id: str, trial: int, solver_or_factory, settings: dict, sample_type: str,
                 trace_path: Path, run_dir: Path, ds=None, htb_dir: Optional[Path] = None,
                 chal=None, target=None, verify=None, context_provider=None):
        self.challenge_id, self.trial = challenge_id, trial
        # Accept either a pre-constructed Solver (tests) or a factory called once the attempt
        # network exists: factory(settings, network, run_id, trace_path) -> Solver.
        if callable(solver_or_factory) and not hasattr(solver_or_factory, "reset"):
            self._solver_factory = solver_or_factory
            self.solver = None
        else:
            self._solver_factory = None
            self.solver = solver_or_factory
        self.settings, self.sample_type, self.run_dir = settings, sample_type, run_dir
        self.trace_path = trace_path
        self.run_id = run_dir.name
        budget_cfg = Budget.from_settings(settings)
        self.budget = AttemptBudget(budget_cfg)
        self.tracer = Tracer(trace_path, self.run_id, budget_cfg.max_chars)
        self.tracer("attempt_started", config_id="agent", challenge_id=challenge_id, trial=trial,
                    sample_type=sample_type, model=settings["model"]["name"], budget=budget_cfg.as_dict(),
                    agent_version=settings.get("agent_version"), code=config.code_version(),
                    settings=config.redacted(settings))
        # The challenge, target and context source are injected for an operator/HTB run (control
        # plane) and default to the frozen dataset+compose path for CTFTiny.
        self.chal = chal if chal is not None else loader.load_challenge(challenge_id, ds, settings)
        self.verify_privately = verify if verify is not None else bind_verifier(self.chal)
        self._context_provider = context_provider
        # How a candidate flag is handled: local answer-key verify (CTFTiny, frozen) or an HTB
        # gateway (manual capture / MCP auto-submit). The solver is unaffected either way.
        self.flag_mode = settings.get("flag", {}).get("mode", "local")
        self.flag_gateway = build_gateway(
            self.flag_mode, verify=self.verify_privately, htb_dir=Path(htb_dir or run_dir.parent),
            challenge_id=challenge_id, run_id=self.run_id, challenge_name=self.chal.name,
            workspace=Path(run_dir) / "workspace")   # host dir backing /workspace; its _verify/ is surfaced
        self.target = target if target is not None else Target(self.chal, self.run_id, settings, run_dir)
        self.workspace: Optional[Path] = None
        self.wrong_submissions = 0
        self.last_candidate: Optional[str] = None     # HTB record: the last candidate handled
        self.last_outcome = None                      # HTB record: its GatewayOutcome

    # -------------------------------------------------------------- appendix steps

    def reset_solver_and_target(self) -> None:
        self.workspace = self.run_dir / "workspace"
        if self.workspace.exists():
            raise ScopeViolation(f"workspace already exists for {self.run_id}; attempts must start fresh")
        self.workspace.mkdir(parents=True)
        endpoint = self.target.start()                     # creates the per-attempt internal network too
        self.tracer("target_started", reset_id=self.target.reset_id, endpoint=endpoint,
                    has_service=self.target.has_service, removed_settings=self.target.removed,
                    compose_file=str(self.target.compose_file) if self.target.has_service else None)
        if self.target.has_service:
            iso = self.target.inspect_isolation()
            self.tracer("isolation_check", **iso)
            if not iso["ok"]:
                raise ScopeViolation("target isolation check failed: " + "; ".join(iso["violations"]))
        if self.solver is None:
            transcript = self.trace_path.with_suffix(".transcript.jsonl")
            self.solver = self._solver_factory(self.settings, self.target.network, self.run_id, transcript)

    def start_attempt_timer(self) -> None:
        self.budget.start()

    def initial_contestant_context(self) -> list:
        if self._context_provider is not None:       # control-plane run: operator/MCP-supplied files
            context, inventory = self._context_provider(self.workspace)
        else:                                         # frozen CTFTiny path
            context, inventory = loader.export_contestant_context(
                self.chal, self.challenge_id, self.workspace, self.settings["target"]["local_host"])
        self.tracer("model_request", role="initial_context", context=context, export_inventory=inventory)
        self.solver.reset(self.workspace, context)   # fresh conversation and memory
        return [context]

    def verify_egress_isolation(self) -> None:
        """For a forwarder-backed target: prove in the live solver sandbox that only `target` is
        reachable and general outbound fails. Raises ScopeViolation if the sandbox has egress."""
        sb = getattr(self.solver, "sandbox", None)
        if sb is None:
            return
        host = self.settings["target"]["local_host"]
        port = int(getattr(self.target, "listen_port", 0)) or int(self.target.endpoint["port"])
        ok = sb.exec(["python3", "-c",
                      f"import socket;socket.create_connection(('{host}',{port}),timeout=8).close();print('OK')"], 12)
        blk = sb.exec(["python3", "-c",
                       "import socket\ntry:\n socket.create_connection(('1.1.1.1',53),timeout=5);print('OPEN')\n"
                       "except Exception:\n print('BLOCKED')"], 12)
        target_reachable = ok.status == "ok" and "OK" in (ok.stdout or "")
        general_blocked = "BLOCKED" in (blk.stdout or "")
        self.tracer("egress_check", target_reachable=target_reachable, general_egress_blocked=general_blocked,
                    target=f"{host}:{port}")
        if not general_blocked:
            raise ScopeViolation("solver sandbox has general outbound egress; only target must be reachable")
        if not target_reachable:
            raise ScopeViolation("solver sandbox cannot reach target through the forwarder")

    def attempt_budget_remains(self) -> bool:
        return self.budget.exhausted_reason() is None

    def request_next_action(self, observations: list) -> tuple[Action, dict]:
        limit = min(float(self.settings["model"]["timeout_s"]), self.budget.remaining_s)
        started = time.monotonic()
        try:
            action, usage = call_with_deadline(self.solver.request_next_action, limit, observations)
        except DeadlineExceeded as err:
            # The cut-off call's tokens were never reported; token totals for this attempt are
            # therefore a known subtotal. Recorded so the writeup can say so.
            self.tracer("error", where="model_call", error=str(err), usage="unknown (call cut off by deadline)",
                        latency_ms=int((time.monotonic() - started) * 1000))
            raise
        except Exception as err:                       # e.g. LLMError after 429 retries ran out
            self._trace_rate_limit_waits(getattr(err, "waits", None))
            raise
        usage = {**NO_USAGE, **(usage or {})}
        self._trace_rate_limit_waits(usage.pop("rate_limit_waits", None))
        extra = {}
        if action.is_flag_submission and getattr(action, "raw_flag", None) not in (None, action.flag):
            extra["raw_flag"] = self.tracer.capture(str(action.raw_flag), "model_response.raw_flag")
        self.tracer("model_response", kind=action.kind, tool=action.tool, arguments=action.arguments,
                    content=self.tracer.capture(getattr(action, "content", "") or "", "model_response.content"),
                    **extra, usage=usage, latency_ms=int((time.monotonic() - started) * 1000))
        return action, usage

    def _trace_rate_limit_waits(self, waits) -> None:
        for w in waits or []:
            self.tracer("rate_limit_wait", **w, elapsed_s=round(self.budget.elapsed_s, 3))

    def record_model_usage(self, usage: dict) -> None:
        self.budget.record_usage(usage["input_tokens"], usage["output_tokens"], usage["cost_usd"])

    def record_submission(self, flag: str, outcome) -> None:
        self.tracer("submission", candidate=self.tracer.capture(flag, "submission"),
                    source=getattr(outcome, "source", None), submitted=getattr(outcome, "submitted", False))
        self.tracer("verification", verified=outcome.verified, detail=getattr(outcome, "detail", "") or None)

    def execute_scoped_tool_with_timeout(self, action: Action) -> dict:
        self.tracer("tool_proposal", tool=action.tool, arguments=action.arguments)
        timeout = self.budget.tool_timeout()
        started = time.monotonic()
        try:
            # +5s grace: the solver enforces timeout_s itself; this is the controller's backstop.
            observation = dict(call_with_deadline(self.solver.execute, timeout + 5, action, timeout))
        except DeadlineExceeded:
            if self.budget.remaining_s <= 0:
                raise
            _kill(self.solver, self.tracer, f"tool {action.tool} exceeded {timeout:.0f}s")
            raise ToolError(f"tool {action.tool} did not return within {timeout:.0f}s; solver killed")
        observation["latency_ms"] = int((time.monotonic() - started) * 1000)
        if isinstance(observation.get("truncated"), dict):   # capped by the ToolRunner before the model saw it
            self.tracer("truncation", where="tool_result.output", applied_by="tool_runner", **observation["truncated"])
        # Backstop for any solver that does not cap; a no-op on already-capped text.
        for key in ("output", "error"):
            if isinstance(observation.get(key), str):
                observation[key] = self.tracer.capture(observation[key], f"tool_result.{key}")
        return observation

    def log_tool_result_and_usage(self, action: Action, observation: dict) -> None:
        self.tracer("tool_result", tool=action.tool, **observation, turns_used=self.budget.turns,
                    elapsed_s=round(self.budget.elapsed_s, 3))

    def stop_solver_and_record_terminal_status(self, status: str, reason: str) -> str:
        if status == "timeout":   # a timed-out call may still be running; stop its environment
            _kill(self.solver, self.tracer, "attempt deadline reached")
        else:                     # every attempt ends by stopping the solver sandbox, then the target
            stop = getattr(self.solver, "kill", None)
            if callable(stop):
                try:
                    self.tracer("solver_stopped", **(stop() or {}))
                except Exception as err:
                    self.tracer("error", where="solver.kill", error=f"{type(err).__name__}: {err}")
        try:
            self.tracer("target_stopped", **self.target.stop())
        except TargetError as err:
            self.tracer("error", where="teardown", error=str(err))
            status, reason = "infrastructure_error", f"teardown failed: {err}"
        self.tracer("attempt_ended", status=status, stopping_reason=reason)
        return status

    # -------------------------------------------------------------- the loop

    def run(self) -> tuple[str, str]:
        status, reason = None, None
        try:
            self.reset_solver_and_target()
            self.start_attempt_timer()
            observations = self.initial_contestant_context()
            if getattr(self.target, "egress_network", None):   # forwarder-backed target: prove isolation
                self.verify_egress_isolation()
            while self.attempt_budget_remains():
                action, usage = self.request_next_action(observations)
                self.record_model_usage(usage)
                if action.is_flag_submission:
                    justification = (action.arguments or {}).get("reason") or getattr(action, "content", "") or ""
                    outcome = self.flag_gateway.handle(action.flag or "", reason=justification)
                    self.last_candidate, self.last_outcome = action.flag or "", outcome
                    self.record_submission(action.flag or "", outcome)
                    if outcome.verified is True:
                        status, reason = SUCCESS_STATUS, outcome.reason
                        break
                    if outcome.pending:          # HTB: candidate captured; stop for (manual) submission
                        status, reason = HTB_CANDIDATE, outcome.reason
                        break
                    self.wrong_submissions += 1
                    observation = {"verified": outcome.verified}   # solver sees only the boolean
                elif action.kind == "give_up":
                    status, reason = "no_flag", "solver gave up"
                    break
                else:
                    observation = self.execute_scoped_tool_with_timeout(action)
                    self.log_tool_result_and_usage(action, observation)
                observations.append(observation)
            if status is None:
                hit = self.budget.exhausted_reason()
                self.tracer("budget_stop", cap=hit, turns=self.budget.turns,
                            elapsed_s=round(self.budget.elapsed_s, 3),
                            tokens=self.budget.input_tokens + self.budget.output_tokens)
                status = "timeout" if hit == "timeout" else ("wrong_flag" if self.wrong_submissions else "budget_exhausted")
                reason = f"budget: {hit}"
        except DeadlineExceeded as err:
            status, reason = "timeout", f"hard deadline: {err}"
            self.tracer("budget_stop", cap="attempt_deadline_s", reason=str(err),
                        elapsed_s=round(self.budget.elapsed_s, 3))
        except ScopeViolation as err:
            status, reason = "scope_violation", str(err)
        except ToolError as err:
            status, reason = "tool_error", str(err)
        except Exception as err:  # target/model/infrastructure failures are retained, never retried silently
            status, reason = "infrastructure_error", f"{type(err).__name__}: {err}"
        finally:
            if reason and status in ("scope_violation", "tool_error", "infrastructure_error"):
                self.tracer("error", where="attempt", status=status, error=reason)
            status = self.stop_solver_and_record_terminal_status(status or "infrastructure_error",
                                                                 reason or "unknown")
        return status, reason


def new_run_id(challenge_id: str, trial: int, label: str | None = None) -> str:
    ts = f"{datetime.now():%Y%m%dT%H%M%S}"
    if label:
        safe = re.sub(r"[^0-9A-Za-z]+", "-", label).strip("-")
        return f"agent-{safe}-{challenge_id}-t{trial}-{ts}"
    return f"agent-{challenge_id}-t{trial}-{ts}"


def run_attempt(challenge_id: str, trial: int, solver: Solver, *, settings: Optional[dict] = None,
                sample_type: str = "live", results_path: Optional[Path] = None,
                traces_dir: Optional[Path] = None, runs_dir: Optional[Path] = None, ds=None,
                label: Optional[str] = None, provision=None) -> dict:
    """Run one fresh attempt and record its result. For the frozen CTFTiny path (flag mode 'local')
    this appends an A4 result to results.jsonl; for the HTB modes it appends a separate HTB record
    (candidate + HTB verdict) and never touches the frozen A4 file. Never raises for in-attempt
    failures."""
    launched = time.monotonic()        # A6: wall-clock is launch -> termination, including setup/teardown
    settings = settings or config.load_settings()
    paths = settings["paths"]
    results_path = Path(results_path or config.path(paths["results"]))
    traces_dir = Path(traces_dir or config.path(paths["traces_dir"]))
    runs_dir = Path(runs_dir or config.path(paths["runs_dir"]))
    htb_dir = results_path.parent

    run_id = new_run_id(challenge_id, trial, label)
    trace_path = traces_dir / f"{run_id}.jsonl"
    # A control plane (operator/HTB run) provisions the challenge, forwarder target, and file staging
    # once the run_id is known; without one this is the frozen CTFTiny dataset+compose path.
    chal = target = context_provider = None
    if provision is not None:
        chal, target, context_provider = provision(run_id, settings)
    attempt = Attempt(challenge_id, trial, solver, settings, sample_type, trace_path,
                      runs_dir / run_id, ds, htb_dir=htb_dir, chal=chal, target=target,
                      context_provider=context_provider)
    status, _ = attempt.run()

    try:
        trace_file = trace_path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        trace_file = trace_path.as_posix()

    common = dict(run_id=run_id, challenge_id=challenge_id, trial=trial, status=status,
                  elapsed_s=time.monotonic() - launched, trace_file=trace_file,
                  reset_id=attempt.target.reset_id, sample_type=sample_type)
    if attempt.flag_mode in config.HTB_FLAG_MODES:
        outcome = attempt.last_outcome
        rec = {"config_id": "agent", **common, "flag_mode": attempt.flag_mode,
               "candidate": attempt.last_candidate,
               "verified": getattr(outcome, "verified", None),
               "submitted": getattr(outcome, "submitted", False),
               "detail": getattr(outcome, "detail", "") or "",
               **attempt.budget.totals()}
        return append_htb_result(htb_dir / "htb_results.jsonl", rec)
    rec = make_result(**common, **attempt.budget.totals())
    return append_result(results_path, rec)
