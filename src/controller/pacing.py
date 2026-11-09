"""Between-attempt pacing for the gateway's per-key TPM/RPM limits (controller side).

Attempts run strictly one at a time. Before each attempt, wait_for_rate_window() sends one
1-token probe and reads X-Ratelimit-Remaining-Tokens / -Requests, polling until the window has
cleared, so usage from the previous attempt cannot push the next one into HTTP 429 (the
2026-11-08 pilot lost an attempt that way). If the gateway does not expose those headers, it
falls back to a fixed sleep sized so the previous attempt's tokens drain out of a one-minute
window at the TPM limit.

Probe calls are batch overhead: they are printed, not counted toward any attempt's usage.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import requests

from common import config


def _probe(settings: dict) -> requests.Response:
    m = settings["model"]
    return requests.post(m["base_url"].rstrip("/") + "/v1/chat/completions",
                         headers={"Authorization": f"Bearer {config.secret('LITELLM_TOKEN')}"},
                         json={"model": m["name"], "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1},
                         timeout=30)


def _int(headers, name) -> Optional[int]:
    try:
        return int(float(headers.get(name)))
    except (TypeError, ValueError):
        return None


def fallback_sleep_s(last_attempt_tokens: Optional[int], settings: dict) -> float:
    """Fixed sleep when no remaining-* headers: let the last attempt's tokens drain at the TPM rate."""
    rl = settings["rate_limit"]
    if not last_attempt_tokens:
        return float(rl.get("default_wait_s", 65))
    return min(float(rl.get("window_max_wait_s", 300)), 60.0 * last_attempt_tokens / float(rl["tpm"]) + 5)


def wait_for_rate_window(settings: dict, last_attempt_tokens: Optional[int] = None,
                         log: Callable[[str], None] = print,
                         probe: Callable[[dict], requests.Response] = _probe,
                         sleep: Callable[[float], None] = time.sleep) -> dict:
    """Block until the key's TPM and RPM windows are (nearly) full again. Returns a short report."""
    rl = settings["rate_limit"]
    if not rl.get("window_check", True):
        return {"waited_s": 0.0, "mode": "disabled"}
    tpm, rpm = int(rl["tpm"]), int(rl["rpm"])
    poll, max_wait = float(rl.get("window_poll_s", 15)), float(rl.get("window_max_wait_s", 300))
    waited = 0.0
    while True:
        r = probe(settings)
        if r.status_code == 429:
            mode = "429"
        else:
            tok = _int(r.headers, "x-ratelimit-remaining-tokens")
            req = _int(r.headers, "x-ratelimit-remaining-requests")
            if tok is None or req is None:                     # headers not exposed: fixed sleep
                s = fallback_sleep_s(last_attempt_tokens, settings)
                log(f"rate window: no remaining-* headers; fixed sleep {s:.0f}s")
                sleep(s)
                return {"waited_s": waited + s, "mode": "fixed"}
            if tok >= 0.95 * tpm and req >= rpm - 2:           # the probe itself uses ~10 tokens, 1 request
                if waited:
                    log(f"rate window clear after {waited:.0f}s (tokens {tok}/{tpm}, requests {req}/{rpm})")
                return {"waited_s": waited, "mode": "headers", "remaining_tokens": tok, "remaining_requests": req}
            mode = f"tokens {tok}/{tpm}, requests {req}/{rpm}"
        if waited >= max_wait:
            log(f"rate window: still limited after {waited:.0f}s ({mode}); starting anyway, 429 handling will wait")
            return {"waited_s": waited, "mode": "gave_up"}
        log(f"rate window: waiting {poll:.0f}s ({mode})")
        sleep(poll)
        waited += poll
