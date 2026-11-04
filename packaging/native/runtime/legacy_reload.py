"""Reuse the shared mutation lock with the packaged watcher, never host paths."""
import os
from pathlib import Path
import subprocess
import sys

from runtime.locking import maintenance_lock


def main():
    action = sys.argv[1]
    if action not in {'force', 'check'}:
        raise SystemExit('Unsupported reload action')
    with maintenance_lock(Path(os.environ['SUITE_STATE_DIR']), timeout=90):
        result = subprocess.run(['/bin/bash', str(Path(os.environ['LOVESTDNS_HOME']) / 'core/watch_list.sh'),
                                 'suite-reload-' + action],
                                env={**os.environ, 'SUITE_RELOAD_LOCKED': '1'}, timeout=45, check=False)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
