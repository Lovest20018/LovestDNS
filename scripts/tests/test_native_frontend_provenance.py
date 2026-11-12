"""Native delivery must reject the development assets that caused a mismatch."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('native_build_frontend', ROOT / 'scripts/build-native-package.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture
def frontend(tmp_path):
    directory = tmp_path / 'dist'
    directory.mkdir()
    (directory / 'index.html').write_bytes(b'<html>locked build</html>')
    source = {'sha256': 'source-fixture', 'files': {'app.py': 'fixture'}}
    manifest = {'schema_version': 1, 'source_commit': 'a' * 40, 'dependency_mode': 'npm-ci',
                'working_tree_modified': False, 'source_snapshot': source,
                'lock_sha256': hashlib.sha256((ROOT / 'integrated/frontend/package-lock.json').read_bytes()).hexdigest(),
                'artifacts': {'index.html': hashlib.sha256((directory / 'index.html').read_bytes()).hexdigest()}}
    return directory, source, manifest


def write_manifest(directory, manifest):
    (directory / 'build-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


def test_native_accepts_complete_locked_frontend(frontend):
    directory, source, manifest = frontend
    write_manifest(directory, manifest)
    assert builder.verify_frontend(directory, source) == manifest


@pytest.mark.parametrize('field,value', [
    ('dependency_mode', 'development-reuse'), ('working_tree_modified', True),
    ('source_commit', ''), ('schema_version', 2),
    ('source_snapshot', {'sha256': 'another-source'}), ('lock_sha256', 'another-lock'),
    ('artifacts', {'../outside': 'invalid'}),
])
def test_native_rejects_unreviewed_frontend_provenance(frontend, field, value):
    directory, source, manifest = frontend
    manifest[field] = value
    write_manifest(directory, manifest)
    with pytest.raises(ValueError, match='Frontend'):
        builder.verify_frontend(directory, source)


def test_native_rejects_modified_or_unlisted_assets(frontend):
    directory, source, manifest = frontend
    write_manifest(directory, manifest)
    (directory / 'index.html').write_bytes(b'changed after build')
    with pytest.raises(ValueError, match='checksum'):
        builder.verify_frontend(directory, source)
    (directory / 'extra.js').write_bytes(b'undeclared asset')
    with pytest.raises(ValueError, match='artifact list'):
        builder.verify_frontend(directory, source)


def test_native_rejects_missing_manifest(frontend):
    directory, source, _ = frontend
    with pytest.raises(ValueError, match='manifest'):
        builder.verify_frontend(directory, source)
