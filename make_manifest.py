"""Build the evaluation manifest from the trusted CTFTiny index (Appendix A5).

    python make_manifest.py \
      --dataset external/CTFTiny/ctftiny.json \
      --ids-file evaluation_ids.txt --split evaluation \
      --source-commit "$(git -C external/CTFTiny rev-parse HEAD)" \
      --out evaluation_manifest.json

PowerShell: $c = git -C external/CTFTiny rev-parse HEAD; then pass --source-commit $c.

Controller-side preparation only. Reads ctftiny.json (index metadata) and the published difficulty
table in the CTFTiny README; never opens challenge.json, so no flag or answer metadata is included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SHORT = {"crypto": "cry", "cry": "cry", "forensics": "for", "for": "for", "misc": "msc", "msc": "msc",
         "pwn": "pwn", "rev": "rev", "web": "web"}
ROW = re.compile(r"^\|\s*(\w+)\s*\|\s*(\d{4}[qf])\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_difficulty(readme: Path) -> dict:
    """{(cat, '2023q', normalized name): label} from the CTFTiny README table."""
    table = {}
    if readme.exists():
        for line in readme.read_text(encoding="utf-8").splitlines():
            m = ROW.match(line)
            if m and m.group(1).lower() in SHORT:
                table[(SHORT[m.group(1).lower()], m.group(2).lower(), norm(m.group(3)))] = m.group(4).strip()
    return table


def read_ids(path: Path) -> list[str]:
    ids = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"duplicate ids in {path}: {dupes}")
    return ids


def build(dataset: Path, ids: list[str], split: str, source_commit: str) -> dict:
    index = json.loads(dataset.read_text(encoding="utf-8"))
    missing = [i for i in ids if i not in index]
    if missing:
        raise SystemExit(f"ids not in {dataset}: {missing}")
    difficulty = load_difficulty(dataset.parent / "README.md")
    challenges = []
    for cid in ids:
        e = index[cid]
        cat = SHORT.get(e["category"].lower(), e["category"])
        code = f"{e['year']}{'f' if 'final' in e['event'].lower() else 'q'}"   # README form, e.g. 2023q
        challenges.append({
            "challenge_id": cid,
            "category": cat,
            "year": str(e["year"]),
            "event": e["event"],
            "challenge": e["challenge"],
            "path": e["path"],
            "difficulty": difficulty.get((cat, code, norm(e["challenge"])), "Unrated"),
        })
    return {
        "split": split,
        "dataset": dataset.as_posix(),
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "source_commit": source_commit,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trials_per_challenge": 3,
        "config_id": "agent",
        "n_challenges": len(challenges),
        "challenges": challenges,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, type=Path)
    ap.add_argument("--ids-file", required=True, type=Path)
    ap.add_argument("--split", required=True)
    ap.add_argument("--source-commit", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", a.source_commit.strip()):
        raise SystemExit(f"--source-commit must be a 40-hex git commit, got {a.source_commit!r}")
    manifest = build(a.dataset, read_ids(a.ids_file), a.split, a.source_commit.strip())
    a.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {a.out}: {manifest['n_challenges']} challenges, split={a.split}, "
          f"source_commit={manifest['source_commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
