"""Build the integrated frontend in isolation from the frozen standalone tree."""
import argparse
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-dependencies", action="store_true", help="Use existing web/node_modules for local development")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "scripts/freeze-standalone.py")], check=True)
    target = ROOT / ".build/integrated-web"
    target.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "web").rglob("*"):
        if path.is_file() and not {"node_modules", "dist", "__pycache__", ".vite"}.intersection(path.parts):
            destination = target / path.relative_to(ROOT / "web")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    runpy.run_path(str(ROOT / "packaging/integrated/prepare_frontend.py"))["prepare"](
        target, ROOT / "integrated/frontend")
    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    if not npm:
        raise SystemExit("Node.js/npm is required")
    modules = target / "node_modules"
    if args.reuse_dependencies and not modules.exists():
        source = ROOT / "web/node_modules"
        if not source.exists():
            raise SystemExit("web/node_modules is missing; omit --reuse-dependencies to install in the isolated build directory")
        if sys.platform == "win32":
            # A junction avoids copying dependency files and needs no elevated privileges.
            subprocess.run(["powershell", "-NoProfile", "-Command",
                "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                str(modules), str(source)], check=True)
        else:
            modules.symlink_to(source, target_is_directory=True)
    if not args.reuse_dependencies:
        if modules.is_symlink() or (sys.platform == "win32" and modules.is_junction()):
            raise SystemExit("Remove the dependency link before using npm ci; it must not modify the frozen tree")
        subprocess.run([npm, "ci"], cwd=target, check=True)
    subprocess.run([npm, "run", "build"], cwd=target, check=True)
    subprocess.run([npm, "run", "lint"], cwd=target, check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/freeze-standalone.py")], check=True)
    print(f"Integrated UI: {target / 'dist'}")


if __name__ == "__main__":
    main()
