import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:  # lets `python -m unittest discover -s tests -t .` import src packages
    sys.path.insert(0, SRC)
