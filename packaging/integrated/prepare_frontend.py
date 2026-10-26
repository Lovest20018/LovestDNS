"""Assemble the integrated UI in a build directory; never edit frozen web/."""
from pathlib import Path
import shutil
import sys


def prepare(root, overlay):
    root, overlay = Path(root), Path(overlay)
    for path in overlay.rglob("*"):
        if path.is_file():
            target = root / path.relative_to(overlay)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    # An integrated App.tsx may replace the standalone shell entirely. Keep
    # the historical text substitution as a fallback for older overlays.
    path = root / "src/App.tsx"
    if not (overlay / "App.tsx").is_file():
        text = path.read_text(encoding="utf-8")
        text = text.replace("import OverviewPage from './pages/OverviewPage';",
                            "import OverviewPage from './pages/SuiteOverview';")
        text = text.replace('PaoPaoDNS Web UI', 'PaoPaoDNS 集成版')
        path.write_text(text, encoding="utf-8", newline="\n")
    # Existing editors keep their serializers and UI, but share v1 authentication,
    # error handling and backend validation with the integrated pages.
    path = root / "src/api.ts"
    text = path.read_text(encoding="utf-8")
    start = text.index("async function apiFetch<T>")
    end = text.index("export interface SystemStatus", start)
    text = text[:start] + "async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {\n  return suiteFetch<T>(path, options);\n}\n\n" + text[end:]
    text = "import { suiteFetch } from './suiteApi';\n" + text.replace("const API_BASE = '';\n", "")
    path.write_text(text, encoding="utf-8", newline="\n")
    path = root / "src/pages/EnvConfigPage.tsx"
    text = path.read_text(encoding="utf-8").replace(
        '修改后会自动热重载，无需重启。',
        '保存后请按后端返回的生效条件处理；初始化变量需要重启集成容器。')
    path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    prepare(*sys.argv[1:])
