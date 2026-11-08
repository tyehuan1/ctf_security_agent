"""Private flag verifier (controller only).

The expected flag stays controller-side. The solver receives only the boolean result.
Never import this module from src/solver or src/tools, and never mount it into the solver.
"""


def verify_privately(submitted: str, chal) -> dict:
    ok = submitted.strip() == chal.flag.strip()
    return {"verified": ok}   # return ONLY the boolean result to the solver
