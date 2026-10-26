"""Keep the original checkpoint portable without accepting content changes."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path):
    lock = json.loads((ROOT / "packaging/standalone.freeze.json").read_text(encoding="utf-8"))
    archive = tmp_path / lock["archive"]
    archive.parent.mkdir()
    shutil.copyfile(ROOT / lock["archive"], archive)
    (tmp_path / "packaging").mkdir()
    shutil.copyfile(ROOT / "packaging/standalone.freeze.json", tmp_path / "packaging/standalone.freeze.json")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/freeze-standalone.py", tmp_path / "scripts/freeze-standalone.py")
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read(name).replace(b"\r\n", b"\n"))
    return tmp_path


def verify(checkout):
    return subprocess.run([sys.executable, str(checkout / "scripts/freeze-standalone.py")],
                          capture_output=True, text=True, timeout=15)


def test_lf_checkout_accepts_only_newline_changes(checkout):
    result = verify(checkout)
    assert result.returncode == 0 and "39 files" in result.stdout


def test_lf_checkout_still_rejects_content_changes(checkout):
    target = checkout / "web/backend/requirements.txt"
    target.write_bytes(target.read_bytes() + b"changed\n")
    result = verify(checkout)
    assert result.returncode != 0 and "web/backend/requirements.txt" in result.stderr


def test_archive_bytes_remain_frozen(checkout):
    archive = checkout / "releases/standalone-20261024-source.zip"
    archive.write_bytes(archive.read_bytes() + b"changed")
    result = verify(checkout)
    assert result.returncode != 0 and "archive checksum changed" in result.stderr
