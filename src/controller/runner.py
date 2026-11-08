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

The controller owns resource accounting and verification. The solver (Phase 1) is any object with
reset(workspace, context), request_next_action(observations) -> (Action, usage), and
execute(action, timeout_s) -> dict; nothing it returns reaches Budget or the target scope.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol

from common import config
from common.schemas import SUCCESS_STATUS, make_result
from controller import loader
from controller.budget import AttemptBudget, Budget
from controller.loader import ScopeViolation
from controller.record import Tracer, append_result
from controller.target import Target, TargetError
from controller.verifier import verify_privately


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


# ------------------------------------------------------------------ appendix steps

def reset_solver_and_target(target: Target, run_dir: Path, tracer: Tracer) -> tuple[str, Optional[dict], Path]:
    workspace = run_dir / "workspace"
    if workspace.exists():
        raise ScopeViolation(f"workspace already exists for {run_dir.name}; attempts must start fresh")
    workspace.mkdir(parents=True)
    endpoint = target.start()
    tracer("target_started", reset_id=target.reset_id, endpoint=endpoint, has_service=target.has_service,
           compose_file=str(target.compose_file) if target.has_service else None, removed_settings=target.removed)
    if target.has_service:
        iso = target.inspect_isolation()
        tracer("isolation_check", **iso)
        if not iso["ok"]:
            raise ScopeViolation("target isolation check failed: " + "; ".join(iso["violations"]))
    return target.reset_id, endpoint, workspace


def start_attempt_timer(budget: AttemptBudget) -> None:
    budget.start()


def initial_contestant_context(chal, challenge_id: str, workspace: Path, settings: dict, tracer: Tracer) -> list:
    context, inventory = loader.export_contestant_context(chal, challenge_id, workspace,
                                                          settings["target"]["local_host"])
    tracer("model_request", role="initial_context", context=context, export_inventory=inventory)
    return [context]


def attempt_budget_remains(budget: AttemptBudget) -> bool:
    return budget.exhausted_reason() is None


def request_next_action(solver: Solver, observations: list, tracer: Tracer) -> tuple[Action, dict]:
    started = time.monotonic()
    action, usage = solver.request_next_action(observations)
    tracer("model_response", kind=action.kind, tool=action.tool, arguments=action.arguments,
           usage=usage, latency_ms=int((time.monotonic() - started) * 1000))
    return action, usage


def record_model_usage(budget: AttemptBudget, usage: dict, tracer: Tracer) -> None:
    budget.record_usage(usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cost_usd"))


def record_submission(tracer: Tracer, flag: str, observation: dict) -> None:
    tracer("submission", candidate=tracer.capture(flag, "submission"))
    tracer("verification", verified=observation["verified"])


def execute_scoped_tool_with_timeout(solver: Solver, action: Action, budget: AttemptBudget, tracer: Tracer) -> dict:
    tracer("tool_proposal", tool=action.tool, arguments=action.arguments)
    started = time.monotonic()
    observation = solver.execute(action, budget.tool_timeout())
    observation = dict(observation)
    observation["latency_ms"] = int((time.monotonic() - started) * 1000)
    for key in ("output", "error"):
        if isinstance(observation.get(key), str):
            observation[key] = tracer.capture(observation[key], f"tool_result.{key}")
    return observation


def log_tool_result_and_usage(tracer: Tracer, action: Action, observation: dict, budget: AttemptBudget) -> None:
    tracer("tool_result", tool=action.tool, **observation, turns_used=budget.turns,
           elapsed_s=round(budget.elapsed_s, 3))


def stop_solver_and_record_terminal_status(target: Target, tracer: Tracer, status: str, reason: str) -> str:
    try:
        tracer("target_stopped", **target.stop())
    except TargetError as err:
        tracer("error", where="teardown", error=str(err))
        status, reason = "infrastructure_error", f"teardown failed: {err}"
    tracer("attempt_ended", status=status, stopping_reason=reason)
    return status


# ------------------------------------------------------------------ attempt

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

    run_id = f"agent-{challenge_id}-t{trial}-{datetime.now():%Y%m%dT%H%M%S}"
    trace_path = traces_dir / f"{run_id}.jsonl"
    run_dir = runs_dir / run_id
    budget_cfg = Budget.from_settings(settings)
    budget = AttemptBudget(budget_cfg)
    tracer = Tracer(trace_path, run_id, budget_cfg.max_chars)
    tracer("attempt_started", config_id="agent", challenge_id=challenge_id, trial=trial, sample_type=sample_type,
           model=settings["model"]["name"], budget=budget_cfg.as_dict(), settings=config.redacted(settings))

    chal = loader.load_challenge(challenge_id, ds, settings)
    target = Target(chal, run_id, settings, run_dir)
    status, reason, wrong = None, None, 0
    try:
        reset_id, endpoint, workspace = reset_solver_and_target(target, run_dir, tracer)
        start_attempt_timer(budget)
        observations = initial_contestant_context(chal, challenge_id, workspace, settings, tracer)
        solver.reset(workspace, observations[0])
        while attempt_budget_remains(budget):
            action, usage = request_next_action(solver, observations, tracer)
            record_model_usage(budget, usage, tracer)
            if action.is_flag_submission:
                observation = verify_privately(action.flag or "", chal)
                record_submission(tracer, action.flag or "", observation)
                if observation["verified"]:
                    status, reason = SUCCESS_STATUS, "verified flag"
                    break
                wrong += 1
            elif action.kind == "give_up":
                status, reason = "no_flag", "solver gave up"
                break
            else:
                observation = execute_scoped_tool_with_timeout(solver, action, budget, tracer)
                log_tool_result_and_usage(tracer, action, observation, budget)
            observations.append(observation)
        if status is None:
            hit = budget.exhausted_reason()
            tracer("budget_stop", cap=hit, turns=budget.turns, elapsed_s=round(budget.elapsed_s, 3),
                   tokens=budget.input_tokens + budget.output_tokens)
            status = "timeout" if hit == "timeout" else ("wrong_flag" if wrong else "budget_exhausted")
            reason = f"budget: {hit}"
    except ScopeViolation as err:
        status, reason = "scope_violation", str(err)
    except ToolError as err:
        status, reason = "tool_error", str(err)
    except Exception as err:  # target/model/infrastructure failures are retained, never retried silently
        status, reason = "infrastructure_error", f"{type(err).__name__}: {err}"
    finally:
        if reason and status in ("scope_violation", "tool_error", "infrastructure_error"):
            tracer("error", where="attempt", status=status, error=reason)
        status = stop_solver_and_record_terminal_status(target, tracer, status or "infrastructure_error",
                                                        reason or "unknown")

    try:
        trace_file = trace_path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        trace_file = trace_path.as_posix()
    rec = make_result(run_id=run_id, challenge_id=challenge_id, trial=trial, status=status,
                      elapsed_s=time.monotonic() - launched, **budget.totals(), trace_file=trace_file,
                      reset_id=target.reset_id, sample_type=sample_type)
    return append_result(results_path, rec)
