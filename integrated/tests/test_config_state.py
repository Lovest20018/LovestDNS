from pathlib import Path
from types import SimpleNamespace

from runtime.config_state import inputs, record, status


def test_receipt_survives_client_restart_but_not_process_change(tmp_path):
    data, state = tmp_path / 'data', tmp_path / 'state'
    data.mkdir()
    (data / 'custom_env.ini').write_text('CNAUTO="no"\n')
    process = {'unbound-raw': {'statename': 'RUNNING', 'pid': 1, 'start': 100}}
    core = SimpleNamespace(data=data, state=state, environment=lambda: {'CNAUTO': 'no'}, states=lambda: process)
    record(core, 'restart_dns_core', inputs(data))
    assert status(core)['files'][0]['state'] == 'recorded'
    (data / 'custom_env.ini').write_text('CNAUTO="yes"\n')
    assert status(core)['files'][0]['state'] == 'changed'
    assert status(core)['verified'] is False
    process['unbound-raw']['pid'] = 2
    assert status(core)['files'][0]['state'] == 'unknown'


def test_external_edits_cannot_be_recorded_as_applied(tmp_path):
    import pytest
    data = tmp_path / 'data'
    data.mkdir()
    before = inputs(data)
    (data / 'custom_mod.yaml').write_text('Hosts: []\n')
    core = SimpleNamespace(data=data, state=tmp_path / 'state')
    with pytest.raises(ValueError, match='文件已改变'):
        record(core, 'apply_custom_mod', before)
    assert not core.state.exists()


def test_incomplete_process_identity_and_corrupt_receipt_are_unknown(tmp_path):
    import pytest
    data, state = tmp_path / 'data', tmp_path / 'state'
    data.mkdir()
    state.mkdir()
    process = {'mosdns': {'statename': 'RUNNING', 'pid': 10}}
    core = SimpleNamespace(data=data, state=state, environment=lambda: {'CNAUTO': 'yes'}, states=lambda: process)
    with pytest.raises(ValueError, match='进程身份'):
        record(core, 'apply_custom_mod', inputs(data))
    (state / 'apply_custom_mod-inputs.json').write_text('{"action":"apply_custom_mod","files":{},"completed_at":null}')
    assert all(row['state'] == 'unknown' for row in status(core)['files'])
