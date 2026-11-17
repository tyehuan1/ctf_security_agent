"""Manual (no-MCP) challenge environment: operator-supplied files + a remote IP:PORT.

For game-day when the MCP server is down or misbehaving. The operator points the controller at a
local directory of downloaded challenge files (under external/hackthebox/<name>) and a remote
IP:PORT. The controller stages the files into the solver workspace and the forwarder makes that
remote reachable as `target`. There is no dataset entry, no answer key, and no MCP call.

The staged context has exactly the shape the solver already consumes from a CTFTiny challenge, so
the solver, its workspace, and its five tools are byte-identical to any other run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from controller.loader import ScopeViolation

DEFAULT_FLAG_FORMAT = "HTB{...}"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


class ManualChallenge:
    """The small subset of a CTFChallenge the attempt loop touches, for an operator-supplied
    challenge with no dataset entry and no answer key (flag is validated via the HTB flag gateway)."""

    def __init__(self, name: str, files_dir: Path, category: str = "hackthebox"):
        self.name = name
        self.category = category
        self.challenge_dir = Path(files_dir)
        self.flag = None                      # no local answer key; HTB (or the operator) scores it
        self.files = sorted(p.name for p in self.challenge_dir.iterdir() if p.is_file())
        self.challenge = {"name": name, "category": category}


def stage_manual_context(chal: ManualChallenge, challenge_id: str, workspace: Path, endpoint: dict,
                         *, description: str, flag_format: str = DEFAULT_FLAG_FORMAT) -> tuple[dict, list]:
    """Copy the operator's files into the workspace and build the solver's initial context.

    Mirrors loader.export_contestant_context (same keys), minus the answer-key leak check (there is
    no key). Files are copied flat from the operator directory; nothing escapes the workspace.
    """
    import shutil

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    root = chal.challenge_dir.resolve()
    inventory = []
    for name in chal.files:
        src = (root / name).resolve()
        dst = (workspace / name).resolve()
        if root not in src.parents or workspace.resolve() not in dst.parents:
            raise ScopeViolation(f"operator file escapes its directory: {name!r}")
        if not src.is_file():
            raise ScopeViolation(f"not a file: {name!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        inventory.append({"path": Path(name).as_posix(), "sha256": _sha256(dst), "size": dst.stat().st_size})

    context = {
        "challenge_id": challenge_id,
        "name": chal.name,
        "category": chal.category,
        "description": description,
        "server_description": None,
        "flag_format": flag_format,
        "files": [i["path"] for i in inventory],
        "target": endpoint,
    }
    return context, inventory


def download_challenge_files(url: str, dest_dir: Path) -> list:
    """Download a challenge archive/file from a (signed) URL into dest_dir and return the staged
    file names. The CONTROLLER does this (it has egress); the solver never sees the URL. Defensive:
    a zip is extracted, anything else is saved under its URL basename.
    """
    import urllib.parse
    import urllib.request
    import zipfile

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ctf-controller"})
    with urllib.request.urlopen(req, timeout=60) as resp:   # noqa: S310 - controller-side, operator URL
        blob = resp.read()
    name = Path(urllib.parse.urlparse(url).path).name or "download.bin"
    tmp = dest_dir / name
    tmp.write_bytes(blob)
    if zipfile.is_zipfile(tmp):
        with zipfile.ZipFile(tmp) as zf:
            safe = [m for m in zf.namelist() if not (m.startswith("/") or ".." in Path(m).parts)]
            zf.extractall(dest_dir, members=safe)
        tmp.unlink(missing_ok=True)
    return sorted(p.name for p in dest_dir.iterdir() if p.is_file())


def default_description(endpoint: Optional[dict], flag_format: str = DEFAULT_FLAG_FORMAT) -> str:
    where = (f"a live service reachable at {endpoint['host']}:{endpoint['port']} (connect with pwntools "
             "remote('target', <port>) or connect_endpoint)") if endpoint else "the provided files only"
    return ("You are solving a Hack The Box challenge in the isolated sandbox at /workspace. The "
            f"provided files are staged there, and the challenge is served from {where}. Recover the "
            f"flag (format {flag_format}) and submit it with submit_flag.")
