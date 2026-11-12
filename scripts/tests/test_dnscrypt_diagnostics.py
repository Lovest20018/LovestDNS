"""Diagnostic artifacts must never be confused with release components."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
builder = runpy.run_path(str(SCRIPTS / 'build-dnscrypt-diagnostic.py'))
profiler = runpy.run_path(str(SCRIPTS / 'profile-dnscrypt.py'))


def artifact(tmp_path, **flags):
    binary = tmp_path / 'dnscrypt-diagnostic'
    binary.write_bytes(b'disposable test artifact')
    manifest = {'diagnostic_only': True, 'deployable': False,
                'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), **flags}
    (tmp_path / 'diagnostic-manifest.json').write_text(json.dumps(manifest))
    return binary


@pytest.mark.parametrize('flags', [{'diagnostic_only': False}, {'deployable': True}, {'diagnostic_only': 1}])
def test_profiler_rejects_release_or_ambiguous_artifacts(tmp_path, flags):
    artifact(tmp_path, **flags)
    with pytest.raises(ValueError, match='diagnostic build'):
        profiler['diagnostic_binary'](tmp_path)


def test_profiler_detects_binary_replacement(tmp_path):
    artifact(tmp_path).write_bytes(b'replaced after manifest creation')
    with pytest.raises(ValueError, match='digest mismatch'):
        profiler['diagnostic_binary'](tmp_path)


def test_profiler_accepts_the_bound_diagnostic_binary(tmp_path):
    binary = artifact(tmp_path)
    selected, _ = profiler['diagnostic_binary'](tmp_path)
    assert selected == binary.resolve()


def test_builder_rejects_unreviewed_source_before_creating_work(tmp_path):
    archive = tmp_path / 'unreviewed.tar.gz'
    archive.write_bytes(b'not the reviewed upstream tag')
    work = tmp_path / 'build'
    with pytest.raises(ValueError, match='reviewed upstream tag'):
        builder['build'](archive, tmp_path / 'nonexistent-go', work, '2.1.20')
    assert not work.exists()


@pytest.mark.parametrize('options', [{'buffer_patch': True}, {'packet_patch': True}])
def test_component_patch_cannot_silently_apply_to_another_version(tmp_path, options):
    work = tmp_path / 'build'
    with pytest.raises(ValueError, match='only for 2.1.20'):
        builder['build'](tmp_path / 'archive', tmp_path / 'go', work, '2.1.5', **options)
    assert not work.exists()
