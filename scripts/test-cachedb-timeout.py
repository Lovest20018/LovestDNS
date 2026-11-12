"""Measure Redis stalls and recovery inside disposable local component processes."""
import argparse
import json
import os
from pathlib import Path
import signal
import statistics
import runpy
import time

from dns_lab import Fixture, candidate_command, query, running_lab


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-home', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.work.exists():
        parser.error('Use a new disposable work directory')
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / 'packaging/components.lock.json').read_text())
    runpy.run_path(str(root / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    args.work.mkdir(parents=True)
    fixture = Fixture(ttl=600)
    candidate = {name: candidate_command(args.bundle, 'unbound' if name == 'unbound' else 'redis', name)
                 for name in ('redis-server', 'redis-cli', 'unbound')}
    results = []
    try:
        for label, upgraded, timeout in [('baseline', False, None), ('candidate-default', True, None), ('candidate-20ms', True, 20)]:
            commands = candidate if upgraded else {name: [str((args.baseline_home / 'core' / name).resolve())] for name in candidate}
            with running_lab(args.work / label, commands['redis-server'], commands['redis-cli'], commands['unbound'],
                             [fixture], command_timeout=timeout) as lab:
                os.kill(lab.redis.process.pid, signal.SIGSTOP)
                samples = []
                try:
                    for i in range(args.repeats):
                        started = time.monotonic()
                        result = query(lab.ports[0], f'stalled-{label}-{i}.cache.test', timeout=3)
                        elapsed = (time.monotonic() - started) * 1000
                        assert result['rcode'] == 0 and result['answers'][0]['value'] == fixture.address, result
                        samples.append(elapsed)
                finally:
                    os.kill(lab.redis.process.pid, signal.SIGCONT)
                assert lab.cli('PING') == 'PONG'
                assert query(lab.ports[0], 'recovered.cache.test')['rcode'] == 0
                entry = {'case': label, 'samples_ms': samples, 'median_ms': statistics.median(samples), 'recovered': True}
                results.append(entry)
                print(json.dumps(entry), flush=True)
        (args.work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    finally:
        fixture.close()


if __name__ == '__main__':
    main()
