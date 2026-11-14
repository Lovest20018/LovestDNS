"""Assemble the integrated UI in a build directory; never edit frozen web/."""
from pathlib import Path
import shutil
import sys


def prepare(root, overlay):
    root, overlay = Path(root), Path(overlay)
    if not all((overlay / path).is_file() for path in ('src/App.tsx', 'src/api.ts')):
        raise ValueError('Integrated overlay requires its own App.tsx and API adapter')
    for path in overlay.rglob("*"):
        if path.is_file():
            target = root / path.relative_to(overlay)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    path = root / "index.html"
    text = path.read_text(encoding="utf-8").replace(
        '<title>PaoPaoDNS Web UI</title>', '<title>LovestDNS</title>')
    path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    prepare(*sys.argv[1:])
