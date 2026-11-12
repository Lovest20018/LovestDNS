"""Bound native caches by the service budget and the actual cgroup ceiling."""
import os

from runtime.resources import memory_budget


def main():
    budget = int(os.environ.get('CACHE_BUDGET_MB', '4096'))
    if not 32 <= budget <= 65536:
        raise ValueError('Invalid cache budget')
    print(min(budget, memory_budget()))


if __name__ == '__main__':
    main()
