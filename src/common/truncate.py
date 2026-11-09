"""Head+tail output cap shared by the solver's tool runner and the controller's tracer.

Lives in common/ so solver code can apply the cap without importing controller code.
The returned text, marker included, is never longer than max_chars, so capping an already
capped string is a no-op (no double markers when both sides apply it).
"""

from __future__ import annotations

from typing import Optional


def truncate(text: Optional[str], max_chars: int) -> tuple[Optional[str], Optional[dict]]:
    """Cap text to max_chars keeping head and tail. Returns (text, truncation info or None)."""
    if text is None or len(text) <= max_chars:
        return text, None
    # The marker's own width depends on the omitted count; size it for the worst case first.
    marker = f"\n...[truncated {len(text)} chars]...\n"
    keep = max(0, max_chars - len(marker))
    head = keep // 2
    tail = keep - head
    omitted = len(text) - head - tail
    marker = f"\n...[truncated {omitted} chars]...\n"
    out = text[:head] + marker + (text[-tail:] if tail else "")
    return out, {"original_chars": len(text), "kept_head": head, "kept_tail": tail, "omitted": omitted}
