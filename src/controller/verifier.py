"""Private flag verifier (controller only).

The expected flag stays controller-side. The solver receives only the boolean result.
Never import this module from src/solver or src/tools, and never mount it into the solver.

The attempt loop calls the one-argument form from the appendix, `verify_privately(action.flag)`,
obtained from bind_verifier(chal), which closes over the current challenge.
"""


class Verification(dict):
    """{"verified": bool}; also readable as observation.verified, as the appendix writes it."""

    @property
    def verified(self) -> bool:
        return self["verified"]


def normalize_candidate(submitted) -> str:
    """Strip whitespace and one layer of wrapping quotes/backticks the model may add."""
    s = str(submitted or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1].strip()
    return s


def verify_privately(submitted: str, chal) -> dict:
    ok = submitted.strip() == chal.flag.strip()
    return {"verified": ok}   # return ONLY the boolean result to the solver


def bind_verifier(chal):
    """Return verify_privately(flag) for this attempt's challenge; chal stays in the closure."""
    def verify_privately_bound(submitted: str) -> Verification:
        return Verification(verify_privately(normalize_candidate(submitted), chal))
    verify_privately_bound.__name__ = "verify_privately"
    return verify_privately_bound
