import importlib.util
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('public_privacy', ROOT / 'scripts/check-public-privacy.py')
privacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy)


def test_history_check_finds_deleted_private_report_and_personal_author(tmp_path, monkeypatch):
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_COMMITTER_NAME': 'Fixture',
           'GIT_AUTHOR_EMAIL': 'fixture@users.noreply.github.com',
           'GIT_COMMITTER_EMAIL': 'fixture@users.noreply.github.com'}
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, env=env, stderr=subprocess.DEVNULL)
    git('init')
    report = tmp_path / 'docs' / 'validation' / 'private.md'
    report.parent.mkdir(parents=True)
    report.write_text('Private deployment record')
    git('add', '.')
    git('commit', '-m', 'Fixture record')
    report.unlink()
    git('add', '-u')
    git('commit', '-m', 'Remove fixture')
    monkeypatch.setattr(privacy, 'ROOT', tmp_path)
    assert privacy.scan(['HEAD'])[0] == []
    assert any(reason == 'private file' for _, reason in privacy.scan(['HEAD'], history=True)[0])
    (tmp_path / 'README.md').write_text('Public fixture')
    git('add', '.')
    env['GIT_AUTHOR_EMAIL'] = 'fixture@' + 'gmail.com'
    git('commit', '-m', 'Fixture identity')
    assert ('commit metadata', 'personal email') in privacy.scan(['HEAD'], history=True)[0]


def test_file_scan_rejects_private_paths_and_unknown_addresses_but_accepts_reserved_examples():
    assert privacy.inspect('docs/setup.md', b'DNS: 192.0.2.53') == []
    address = '.'.join(['192', '168', '77', '23']).encode()
    assert privacy.inspect('docs/setup.md', b'DNS: ' + address) == [('docs/setup.md', 'non-example private address')]
    assert privacy.inspect('.env', b'') == [('.env', 'private file')]
