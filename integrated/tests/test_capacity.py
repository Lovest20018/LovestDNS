from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from runtime.admission import CapacityBusy, slots
from runtime.manager import Manager
from runtime.tasks import TaskStore
from test_integrated_api import client, AUTH


def test_diagnostic_slots_apply_across_processes_and_release_after_kill(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = 'import sys,time;from runtime.admission import slots\nwith slots(sys.argv[1], 1):\n print("ready",flush=True);time.sleep(60)\n'
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                             env={**os.environ, 'PYTHONPATH': str(root)}, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(CapacityBusy):
            with slots(tmp_path, 1):
                pytest.fail('Cross-process limit was bypassed')
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    with slots(tmp_path, 1):
        pass


def test_busy_diagnostic_releases_web_capacity_and_preserves_v1_envelope(client, monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, 'RUN_DIR', tmp_path)
    with slots(tmp_path / 'diagnostic-slots', 2), slots(tmp_path / 'diagnostic-slots', 2):
        response = client[0].post('/api/v1/dns-test', headers=AUTH, json={'domain': 'example.org'})
        assert response.status_code == 503
        assert response.get_json()['error']['code'] == 'UNAVAILABLE'
        assert response.headers['Retry-After'] == '1'
        assert client[0].get('/api/v1/instance', headers=AUTH).status_code == 200


def test_slow_plan_does_not_queue_other_inspections_or_block_task_reads(tmp_path):
    started, release = threading.Event(), threading.Event()
    class Core:
        state = tmp_path
        def plan(self):
            started.set()
            assert release.wait(5)
            return {'sha256': 'done'}
    manager = Manager(Core(), TaskStore(tmp_path))
    with ThreadPoolExecutor(1) as pool:
        planned = pool.submit(manager.dispatch, {'operation': 'plan'})
        try:
            assert started.wait(2)
            with pytest.raises(RuntimeError, match='配置检查'):
                manager.dispatch({'operation': 'plan'})
            assert manager.dispatch({'operation': 'tasks'}) == {'tasks': []}
        finally:
            release.set()
        assert planned.result() == {'sha256': 'done'}
