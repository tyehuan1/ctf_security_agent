"""PHASE STUB — use the instructor kit's make_manifest.py (Appendix A5) when available.

    python make_manifest.py \
      --dataset external/CTFTiny/ctftiny.json \
      --ids-file evaluation_ids.txt --split evaluation \
      --source-commit "$(git -C external/CTFTiny rev-parse HEAD)" \
      --out evaluation_manifest.json
"""

import sys

if __name__ == "__main__":
    sys.exit("make_manifest.py: drop in the instructor kit's version (Appendix A5); not implemented here.")
