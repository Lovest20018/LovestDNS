"""Conservative cache budget: respect both host availability and cgroup limits."""
from pathlib import Path


def memory_budget(meminfo="/proc/meminfo", limits=None):
    values = {}
    for line in Path(meminfo).read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemAvailable", "MemFree"}:
            values[key] = int(rest.split()[0]) * 1024
    available = values.get("MemAvailable", values.get("MemFree", 128 * 1024 * 1024))
    limits = limits or ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]
    for path in limits:
        try:
            raw = Path(path).read_text().strip()
            if raw.isdigit() and 0 < int(raw) < 2**60:
                available = min(available, int(raw))
        except OSError:
            continue
    # Reserve at least 128 MiB, plus 20% for processes, kernel and transient work.
    return max(32, (available - max(128 * 1024 * 1024, int(available * 0.2))) // (1024 * 1024))


if __name__ == "__main__":
    print(memory_budget())
