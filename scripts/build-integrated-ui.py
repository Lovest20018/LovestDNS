"""Build the integrated frontend in isolation from the frozen standalone tree."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def assemble(target):
    target = Path(target)
    # Validate the resolved cleanup boundary before touching an incremental tree.
    if target.is_symlink() or target.resolve().parent != (ROOT / '.build').resolve():
        raise ValueError('UI build target must be a direct directory under .build')
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.name == 'node_modules':
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif getattr(child, 'is_junction', lambda: False)():
            child.rmdir()
        else:
            shutil.rmtree(child)
    for path in (ROOT / "web").rglob("*"):
        if path.is_file() and not {"node_modules", "dist", "__pycache__", ".vite"}.intersection(path.parts):
            destination = target / path.relative_to(ROOT / "web")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    runpy.run_path(str(ROOT / "packaging/integrated/prepare_frontend.py"))["prepare"](
        target, ROOT / "integrated/frontend")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-dependencies", action="store_true", help="Use existing web/node_modules for local development")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "scripts/freeze-standalone.py")], check=True)
    target = ROOT / ".build/integrated-web"
    assemble(target)
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
        subprocess.run([npm, "audit", "--audit-level=low"], cwd=target, check=True)
    subprocess.run([npm, "run", "build"], cwd=target, check=True)
    subprocess.run([npm, "run", "lint"], cwd=target, check=True)
    manifest = {
        'schema_version': 1,
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'working_tree_modified': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
        'dependency_mode': 'development-reuse' if args.reuse_dependencies else 'npm-ci',
        'lock_sha256': hashlib.sha256((target / 'package-lock.json').read_bytes()).hexdigest(),
        'node_version': subprocess.check_output([shutil.which('node'), '--version'], text=True).strip(),
        'source_snapshot': runpy.run_path(str(ROOT / 'scripts/export-api-contract.py'))['source_summary'](),
        'artifacts': {str(path.relative_to(target / 'dist')).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted((target / 'dist').rglob('*')) if path.is_file()},
    }
    (target / 'dist/build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    subprocess.run([sys.executable, str(ROOT / "scripts/freeze-standalone.py")], check=True)
    print(f"Integrated UI: {target / 'dist'}")


if __name__ == "__main__":
    main()
