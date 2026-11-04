"""Bound native caches by the service budget and the actual cgroup ceiling."""
import os
from pathlib import Path

from runtime.resources import memory_budget


def main():
    limits = ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        if line.startswith('0::'):
            limits.insert(0, str(Path('/sys/fs/cgroup') / line[3:].lstrip('/') / 'memory.max'))
    budget = int(os.environ.get('CACHE_BUDGET_MB', '4096'))
    if not 32 <= budget <= 65536:
        raise ValueError('Invalid cache budget')
    print(min(budget, memory_budget(limits=limits)))


if __name__ == '__main__':
    main()
