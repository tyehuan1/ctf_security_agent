"""Phase 0 readiness check (controller side). Starts no targets and makes no model calls.

    .venv\\Scripts\\python.exe check_ready.py

Checks: Python/nyuctf version, Docker server + compose, data repos at the commits in resources.txt,
all evaluation_ids load (name/category/files only, never the flag), contestant export leaks no flag,
sanitized compose renders for service challenges, .gitignore coverage, LITELLM_TOKEN presence.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from common import config  # noqa: E402
from controller import loader  # noqa: E402
from controller.target import Target  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    results.append((ok, msg))
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def main() -> int:
    settings = config.load_settings()
    check(sys.version_info[:2] == (3, 11), f"python {sys.version.split()[0]} (venv: {sys.prefix})")
    check(version("nyuctf") == "1.1.1", f"nyuctf=={version('nyuctf')}")

    srv = sh("docker", "version", "--format", "{{.Server.Version}}")
    check(srv.returncode == 0 and bool(srv.stdout.strip()), f"docker server responds: {srv.stdout.strip() or srv.stderr.strip()[:120]}")
    comp = sh("docker", "compose", "version", "--short")
    check(comp.returncode == 0, f"docker compose {comp.stdout.strip()}")

    pinned = (ROOT / "resources.txt").read_text(encoding="utf-8")
    for repo in ("NYU_CTF_Bench", "CTFTiny"):
        head = sh("git", "-C", str(ROOT / "external" / repo), "rev-parse", "HEAD").stdout.strip()
        check(bool(head) and head in pinned, f"external/{repo} HEAD {head} recorded in resources.txt")

    ds = loader.load_dataset(settings)
    ids = loader.load_evaluation_ids(settings)
    check(len(ids) == 10 and len(set(ids)) == 10, f"evaluation_ids.txt has {len(ids)} unique keys")
    local_host = settings["target"]["local_host"]
    for key in ids:
        try:
            chal = loader.load_challenge(key, ds, settings)
            with tempfile.TemporaryDirectory() as tmp:
                ctx, inv = loader.export_contestant_context(chal, key, Path(tmp) / "ws", local_host)
                t = Target(chal, f"readiness-{key}", settings, Path(tmp) / "run")
                svc = "file-only"
                if t.has_service:
                    t.render()
                    svc = f"service {local_host}:{chal.port} (stripped {len(t.removed)} settings)"
            embedded = [i["path"] for i in inv if i["flag_plaintext_present"]]
            note = f"; flag bytes inside contestant file(s) {embedded}" if embedded else ""
            check(True, f"{key:20} name={chal.name!r} category={chal.category} files={chal.files} [{svc}]{note}")
        except Exception as err:
            check(False, f"{key}: {type(err).__name__}: {err}")

    ignored = sh("git", "-C", str(ROOT), "check-ignore", ".env", "external/", "runs/").stdout.split()
    check({".env", "external/", "runs/"} <= set(ignored), f".gitignore covers {ignored}")
    check(bool(config.secret("LITELLM_TOKEN")), "LITELLM_TOKEN set in .env (value not shown)")

    failed = [m for ok, m in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
