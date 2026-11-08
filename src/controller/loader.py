"""Challenge loading and contestant export (controller only).

Loads challenges exactly as Appendix A2 shows, passing dataset_json explicitly:

    ds = CTFDataset(dataset_json="external/CTFTiny/ctftiny.json")
    chal = CTFChallenge(ds.get("pwn-puffin"), ds.basedir)
    print(chal.name, chal.category, chal.files)
    # Never send chal.flag or chal.challenge to the solver.

CTFChallenge objects stay inside the controller because they expose the expected flag.
export_contestant_context() is the only path from challenge data to the solver (Appendix A3):
listed contestant files, a sanitized description, and the local target endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/controller/loader.py KEY`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nyuctf.challenge import CTFChallenge
from nyuctf.dataset import CTFDataset
from nyuctf.utils import SafeDict

from common import config

CSAW_HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)*csaw\.io\b", re.I)


class ScopeViolation(RuntimeError):
    """Exported material would leak controller-only data or escape the challenge directory."""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_dataset(settings: dict | None = None) -> CTFDataset:
    settings = settings or config.load_settings()
    return CTFDataset(dataset_json=str(config.path(settings["data"]["dataset_json"])))


def load_evaluation_ids(settings: dict | None = None) -> list[str]:
    settings = settings or config.load_settings()
    text = config.path(settings["data"]["evaluation_ids"]).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def load_challenge(key: str, ds: CTFDataset | None = None, settings: dict | None = None) -> CTFChallenge:
    """CTFChallenge(ds.get(key), ds.basedir), with the Windows path remap from settings.data.path_remap."""
    settings = settings or config.load_settings()
    ds = ds or load_dataset(settings)
    info = dict(ds.get(key))
    remap = {k: v for k, v in settings["data"].get("path_remap", {}).items() if not k.startswith("$")}
    info["path"] = remap.get(info["path"], info["path"])
    return CTFChallenge(info, ds.basedir)


def sanitize_text(text: str | None, local_host: str) -> str | None:
    """Rewrite original CSAW hostnames to the local target alias."""
    return CSAW_HOST.sub(local_host, text) if text else text


def target_endpoint(chal: CTFChallenge, local_host: str) -> dict | None:
    """Local endpoint for service challenges; None for file-only challenges."""
    if not (chal.container and chal.server_name and chal.port):
        return None
    return {"host": local_host, "port": int(chal.port), "type": chal.server_type}


def export_contestant_context(chal: CTFChallenge, challenge_id: str, workspace: Path,
                              local_host: str) -> tuple[dict, list[dict]]:
    """Copy the listed contestant files into workspace and build the solver's initial context.

    Returns (context, inventory). inventory is the SHA-256 hash inventory of exported files; it is
    controller-side evidence and is not itself placed in the workspace.
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    endpoint = target_endpoint(chal, local_host)
    box = local_host if endpoint else None

    root = chal.challenge_dir.resolve()
    inventory = []
    flag_bytes = chal.flag.strip().encode()
    for name in chal.files:
        src = (root / name).resolve()
        dst = (workspace / name).resolve()
        if root not in src.parents or workspace.resolve() not in dst.parents:
            raise ScopeViolation(f"contestant file escapes its directory: {name!r}")
        if src.name == "challenge.json" or not src.is_file():
            raise ScopeViolation(f"not an exportable contestant file: {name!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        inventory.append({
            "path": Path(name).as_posix(),
            "sha256": sha256_file(dst),
            "size": dst.stat().st_size,
            # Some rev/pwn files legitimately embed the flag; recorded, not blocked.
            "flag_plaintext_present": flag_bytes in dst.read_bytes(),
        })

    description = chal.challenge["description"].format_map(SafeDict(box=box, port=chal.port))
    context = {
        "challenge_id": challenge_id,
        "name": chal.name,
        "category": chal.category,
        "description": sanitize_text(description, local_host),
        "server_description": sanitize_text(chal.server_description, local_host),
        "flag_format": chal.flag_format,
        "files": [i["path"] for i in inventory],
        "target": endpoint,
    }
    if chal.flag.strip() in json.dumps(context, ensure_ascii=False):
        raise ScopeViolation("expected flag appears in the exported context")
    return context, inventory


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "pwn-roppity"
    settings = config.load_settings()
    ds = CTFDataset(dataset_json=str(config.path(settings["data"]["dataset_json"])))
    chal = load_challenge(key, ds, settings)
    print(chal.name, chal.category, chal.files)
    # Never send chal.flag or chal.challenge to the solver.
