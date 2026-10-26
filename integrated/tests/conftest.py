import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "integrated/backend"), str(ROOT / "integrated"),
               str(ROOT / "web/backend"), str(ROOT / "packaging/integrated")]
