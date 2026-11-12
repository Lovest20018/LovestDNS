"""Optional, explicit CPU placement for disposable Linux benchmark roles."""
import os

ROLES = ('controller', 'fixture', 'redis', 'unbound', 'capture', 'generator')


def parse_layout(value, available):
    if not value:
        return {}
    layout = {}
    for item in value.split(','):
        role, separator, number = item.partition('=')
        parts = number.split('+')
        if (not separator or role not in ROLES or role in layout or not all(p.isdecimal() for p in parts)
                or len(parts) != (2 if role == 'generator' and '+' in number else 1)):
            raise ValueError('CPU layout requires each known role exactly once with an integer CPU')
        layout[role] = [int(p) for p in parts] if len(parts) > 1 else int(number)
    flattened = [cpu for value in layout.values() for cpu in cpu_list(value)]
    if set(layout) != set(ROLES) or len(set(flattened)) != len(flattened):
        raise ValueError('All benchmark roles require distinct CPUs')
    if not set(flattened) <= set(available):
        raise ValueError('CPU layout exceeds the allowed CPU affinity')
    return layout


def pin(cpu):
    if cpu is not None:
        os.sched_setaffinity(0, {cpu})
        if os.sched_getaffinity(0) != {cpu}:
            raise RuntimeError('Requested benchmark CPU affinity was not applied')


def placed(command, cpu):
    return (['taskset', '-c', ','.join(map(str, cpu_list(cpu)))] if cpu is not None else []) + list(command)


def cpu_list(cpu):
    return sorted(cpu) if isinstance(cpu, list) else [cpu]


def parse_generator_threads(value, layout):
    if not value:
        return {}
    roles = {}
    for item in value.split(','):
        role, separator, number = item.partition('=')
        if not separator or role not in ('receiver', 'sender') or role in roles or not number.isdecimal():
            raise ValueError('Generator threads require receiver=N,sender=N')
        roles[role] = int(number)
    if (set(roles) != {'receiver', 'sender'} or len(set(roles.values())) != 2 or
            set(roles.values()) != set(cpu_list(layout.get('generator')))):
        raise ValueError('Sender and receiver must use the two configured generator CPUs exactly once')
    return roles


def valid_placement(evidence, layout, thread_cpus=None):
    expected = {('answer_capture' if role == 'capture' else role): cpu_list(cpu) for role, cpu in layout.items()}
    valid = (isinstance(evidence, dict) and evidence.get('complete') is True
            and evidence.get('expected') == layout and evidence.get('samples', 0) > 0
            and evidence.get('mismatches') == [] and evidence.get('last') == expected
            and (not isinstance(layout.get('generator'), list) or
                 (evidence.get('generator_threads_checked', 0) >= 2 and evidence.get('thread_mismatches') == [])))
    if not valid or not thread_cpus:
        return valid
    audit = evidence.get('thread_preflight') or {}
    observed = evidence.get('thread_observations') or {}
    return (audit.get('method') == 'dnsperf-thread-affinity-before-start-v1' and audit.get('complete') is True
            and audit.get('requested') == thread_cpus and set(observed) == set(thread_cpus)
            and len({row.get('tid') for row in observed.values()}) == 2
            and all(observed[role].get('cpus') == [cpu] and observed[role].get('samples', 0) > 0
                    and observed[role].get('last_cpu') == cpu for role, cpu in thread_cpus.items()))
