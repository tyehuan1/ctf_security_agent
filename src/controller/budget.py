"""Per-attempt resource caps, enforced in the controller.

Budget is frozen and built only from config/settings.json. Nothing the model returns is passed
to it, so model output cannot change limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Budget:
    max_turns: int
    max_chars: int
    tool_timeout_s: float
    attempt_deadline_s: float
    attempt_token_cap: int

    @classmethod
    def from_settings(cls, settings: dict) -> "Budget":
        b = settings["budget"]
        return cls(int(b["max_turns"]), int(b["max_chars"]), float(b["tool_timeout_s"]),
                   float(b["attempt_deadline_s"]), int(b["attempt_token_cap"]))

    def as_dict(self) -> dict:
        return {"max_turns": self.max_turns, "max_chars": self.max_chars, "tool_timeout_s": self.tool_timeout_s,
                "attempt_deadline_s": self.attempt_deadline_s, "attempt_token_cap": self.attempt_token_cap}


class AttemptBudget:
    """Running counters for one attempt. Missing usage stays null (unknown), never 0."""

    def __init__(self, budget: Budget, clock: Callable[[], float] = time.monotonic):
        self.budget = budget
        self._clock = clock
        self.started: Optional[float] = None
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.input_known = self.output_known = self.cost_known = True

    def start(self) -> None:
        self.started = self._clock()

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self.started is None else self._clock() - self.started

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.budget.attempt_deadline_s - self.elapsed_s)

    def record_usage(self, input_tokens: Optional[int], output_tokens: Optional[int],
                     cost_usd: Optional[float] = None) -> None:
        """Count one model turn and its usage. A None counter marks that total unknown."""
        self.turns += 1
        if input_tokens is None:
            self.input_known = False
        else:
            self.input_tokens += int(input_tokens)
        if output_tokens is None:
            self.output_known = False
        else:
            self.output_tokens += int(output_tokens)
        if cost_usd is None:
            self.cost_known = False
        else:
            self.cost_usd += float(cost_usd)

    def tool_timeout(self) -> float:
        """Per-tool timeout, never past the attempt deadline."""
        return min(self.budget.tool_timeout_s, self.remaining_s)

    def exhausted_reason(self) -> Optional[str]:
        """None while budget remains; otherwise the A5 status for the cap that was hit."""
        if self.started is not None and self.elapsed_s >= self.budget.attempt_deadline_s:
            return "timeout"
        if self.turns >= self.budget.max_turns:
            return "budget_exhausted"
        # Known subtotal counts against the cap even when another counter is unknown.
        if self.input_tokens + self.output_tokens >= self.budget.attempt_token_cap:
            return "budget_exhausted"
        return None

    def totals(self) -> dict:
        """A4 counters: null when any contributing value was unavailable."""
        return {
            "input_tokens": self.input_tokens if self.input_known else None,
            "output_tokens": self.output_tokens if self.output_known else None,
            "cost_usd": round(self.cost_usd, 6) if self.cost_known and self.turns else None,
        }


class DeadlineExceeded(RuntimeError):
    """A model call or tool ran past the time the controller allowed it."""


def call_with_deadline(fn: Callable, timeout_s: float, *args, **kwargs):
    """Run fn in a worker thread; raise DeadlineExceeded if it has not returned within timeout_s.

    This bounds the controller's wait, not the work itself: the caller must then kill the
    execution environment (solver container / HTTP session) so nothing keeps running.
    """
    import threading

    box: dict = {}

    def work():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as err:  # re-raised in the controller thread
            box["error"] = err

    t = threading.Thread(target=work, daemon=True, name="deadline-call")
    t.start()
    t.join(max(0.0, timeout_s))
    if t.is_alive():
        raise DeadlineExceeded(f"call exceeded {timeout_s:.1f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def truncate(text: str, max_chars: int) -> tuple[str, Optional[dict]]:
    """Cap captured output, keeping head and tail. Returns (text, truncation info or None)."""
    if text is None or len(text) <= max_chars:
        return text, None
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - head - tail
    marker = f"\n...[truncated {omitted} chars]...\n"
    return text[:head] + marker + text[-tail:], {"original_chars": len(text), "kept_head": head,
                                                 "kept_tail": tail, "omitted": omitted}
