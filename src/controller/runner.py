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
from controller.loader import ScopeViolation
from controller.record import Tracer, append_result
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
                 trace_path: Path, run_dir: Path, ds=None):
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
                    settings=config.redacted(settings))
        self.chal = loader.load_challenge(challenge_id, ds, settings)
        self.verify_privately = bind_verifier(self.chal)   # one-argument form used by the loop
        self.target = Target(self.chal, self.run_id, settings, run_dir)
        self.workspace: Optional[Path] = None
        self.wrong_submissions = 0

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
        context, inventory = loader.export_contestant_context(
            self.chal, self.challenge_id, self.workspace, self.settings["target"]["local_host"])
        self.tracer("model_request", role="initial_context", context=context, export_inventory=inventory)
        self.solver.reset(self.workspace, context)   # fresh conversation and memory
        return [context]

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
        usage = {**NO_USAGE, **(usage or {})}
        self.tracer("model_response", kind=action.kind, tool=action.tool, arguments=action.arguments,
                    usage=usage, latency_ms=int((time.monotonic() - started) * 1000))
        return action, usage

    def record_model_usage(self, usage: dict) -> None:
        self.budget.record_usage(usage["input_tokens"], usage["output_tokens"], usage["cost_usd"])

    def record_submission(self, flag: str, observation) -> None:
        self.tracer("submission", candidate=self.tracer.capture(flag, "submission"))
        self.tracer("verification", verified=observation.verified)

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
            while self.attempt_budget_remains():
                action, usage = self.request_next_action(observations)
                self.record_model_usage(usage)
                if action.is_flag_submission:
                    observation = self.verify_privately(action.flag)
                    self.record_submission(action.flag or "", observation)
                    if observation.verified:
                        status, reason = SUCCESS_STATUS, "verified flag"
                        break
                    self.wrong_submissions += 1
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


def new_run_id(challenge_id: str, trial: int) -> str:
    return f"agent-{challenge_id}-t{trial}-{datetime.now():%Y%m%dT%H%M%S}"


def run_attempt(challenge_id: str, trial: int, solver: Solver, *, settings: Optional[dict] = None,
                sample_type: str = "live", results_path: Optional[Path] = None,
                traces_dir: Optional[Path] = None, runs_dir: Optional[Path] = None, ds=None) -> dict:
    """Run one fresh attempt and append its A4 result. Never raises for in-attempt failures."""
    launched = time.monotonic()        # A6: wall-clock is launch -> termination, including setup/teardown
    settings = settings or config.load_settings()
    paths = settings["paths"]
    results_path = Path(results_path or config.path(paths["results"]))
    traces_dir = Path(traces_dir or config.path(paths["traces_dir"]))
    runs_dir = Path(runs_dir or config.path(paths["runs_dir"]))

    run_id = new_run_id(challenge_id, trial)
    trace_path = traces_dir / f"{run_id}.jsonl"
    attempt = Attempt(challenge_id, trial, solver, settings, sample_type, trace_path, runs_dir / run_id, ds)
    status, _ = attempt.run()

    try:
        trace_file = trace_path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        trace_file = trace_path.as_posix()
    rec = make_result(run_id=run_id, challenge_id=challenge_id, trial=trial, status=status,
                      elapsed_s=time.monotonic() - launched, **attempt.budget.totals(), trace_file=trace_file,
                      reset_id=attempt.target.reset_id, sample_type=sample_type)
    return append_result(results_path, rec)
