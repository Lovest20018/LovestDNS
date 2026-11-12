"""Bootstrap cache ceiling from host availability and cgroup remaining capacity."""
from pathlib import Path


def cgroup_limits(root='/sys/fs/cgroup', membership='/proc/self/cgroup'):
    root = Path(root)
    result = [root / 'memory.max', root / 'memory/memory.limit_in_bytes']
    try:
        lines = Path(membership).read_text().splitlines()
    except OSError:
        return result
    for line in lines:
        parts = line.split(':', 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, relative = parts
        if hierarchy == '0' and controllers == '':
            base, name = root, 'memory.max'
        elif 'memory' in controllers.split(','):
            base, name = root / 'memory', 'memory.limit_in_bytes'
        else:
            continue
        group = base / relative.lstrip('/')
        # Native services may inherit a tighter ancestor slice limit.
        if '..' in group.parts:
            continue
        while group == base or base in group.parents:
            result.append(group / name)
            if group == base:
                break
            group = group.parent
    return list(dict.fromkeys(result))


def memory_budget(meminfo="/proc/meminfo", limits=None):
    values = {}
    for line in Path(meminfo).read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemAvailable", "MemFree"}:
            values[key] = int(rest.split()[0]) * 1024
    available = values.get("MemAvailable", values.get("MemFree", 128 * 1024 * 1024))
    limits = cgroup_limits() if limits is None else limits
    for path in limits:
        try:
            raw = Path(path).read_text().strip()
            if raw.isdigit() and 0 < int(raw) < 2**60:
                limit = int(raw)
                usage_name = 'memory.current' if Path(path).name == 'memory.max' else 'memory.usage_in_bytes'
                try:
                    usage = int(Path(path).with_name(usage_name).read_text().strip())
                    if usage < 0:
                        raise ValueError('Invalid cgroup usage')
                except (OSError, ValueError):
                    usage = 0  # Older/unavailable accounting: retain ceiling guard.
                available = min(available, max(0, limit - usage))
        except OSError:
            continue
    # Reserve at least 128 MiB, plus 20% for processes, kernel and transient work.
    budget = (available - max(128 * 1024 * 1024, int(available * 0.2))) // (1024 * 1024)
    if budget < 32:
        raise ValueError('Insufficient memory headroom for caches and maintenance reserve')
    return budget


if __name__ == "__main__":
    print(memory_budget())
