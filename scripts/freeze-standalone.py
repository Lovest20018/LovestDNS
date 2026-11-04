"""Create or verify the standalone source checkpoint without changing Git history."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging/standalone.freeze.json"
EXCLUDED = {"node_modules", "dist", "__pycache__", ".pytest_cache", ".vite"}
ARCHIVED_WORKFLOW = "packaging/legacy/web-docker-ghcr.yml.disabled"


def checkpoint_path(name):
    path = ROOT / name
    if name == ".github/workflows/web-docker-ghcr.yml" and not path.exists():
        return ROOT / ARCHIVED_WORKFLOW
    return path


def source_files():
    paths = [p for p in (ROOT / "web").rglob("*") if p.is_file()
             and not EXCLUDED.intersection(p.relative_to(ROOT).parts)]
    paths += [checkpoint_path(p) for p in ("docker-compose-web.yaml", "docker-compose.sidecar.yaml",
                                          ".github/workflows/web-docker-ghcr.yml")]
    return sorted(paths)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest(path):
    # Git checkouts may use CRLF on Windows and LF in Linux CI.
    data = path.read_bytes()
    if path.suffix in {".py", ".ts", ".tsx", ".css", ".json", ".html", ".md", ".yaml", ".yml", ".svg"}:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    current = {(p.relative_to(ROOT).as_posix() if p != ROOT / ARCHIVED_WORKFLOW else
                ".github/workflows/web-docker-ghcr.yml"): source_digest(p) for p in source_files()}
    if args.create:
        if LOCK.exists():
            raise SystemExit("Checkpoint already exists; refusing to replace it")
        archive = ROOT / "releases/standalone-20261024-source.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as output:
            for name in current:
                output.write(checkpoint_path(name), name)
        LOCK.parent.mkdir(exist_ok=True)
        LOCK.write_text(json.dumps({"checkpoint": "standalone-20261024", "release": False,
                                    "source_hash_mode": "text-lf",
                                    "archive": archive.relative_to(ROOT).as_posix(),
                                    "archive_sha256": digest(archive), "files": current},
                                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        changed = sorted(name for name in current.keys() | lock["files"].keys()
                         if current.get(name) != lock["files"].get(name))
        archive = ROOT / lock["archive"]
        if digest(archive) != lock["archive_sha256"]:
            raise SystemExit("Standalone checkpoint archive checksum changed")
        # The original text-lf manifest left four text files (.gitignore,
        # Dockerfile, requirements.txt, eslint.config.js) hashed as raw CRLF.
        # Preserve that manifest and the original ZIP byte-for-byte. For a Git
        # checkout with LF, accept only newline-equivalent content from the
        # checksum-verified archive; never regenerate the frozen checkpoint.
        with zipfile.ZipFile(archive) as source:
            if set(source.namelist()) != set(lock["files"]):
                raise SystemExit("Standalone checkpoint archive file list changed")
            changed = [name for name in changed if name not in current or name not in lock["files"]
                       or checkpoint_path(name).read_bytes().replace(b"\r\n", b"\n")
                       != source.read(name).replace(b"\r\n", b"\n")]
        if changed:
            raise SystemExit("Standalone checkpoint changed: " + ", ".join(changed))
    print(f"Standalone checkpoint verified: {len(current)} files")


if __name__ == "__main__":
    main()
