"""Keep configuration variables from replacing the native service environment."""
from fnmatch import fnmatchcase
import sys

from runtime.component import read_overrides

RESERVED = ('PATH', 'PYTHON*', 'LOVESTDNS_*', 'LD_*', 'BASH_ENV', 'ENV',
            'SUITE_STATE_DIR', 'SUITE_RUN_DIR', 'SUITE_DIST_DIR', 'SUITE_BUILD_INFO',
            'SUITE_DNS_PORT', 'SUITE_READ_TOKEN', 'WEB_*', 'DNSPORT', 'DNS_TEST_*',
            'DNS_BIND_ADDRESS', 'FORWARD_BIND_ADDRESS')


def validate(values):
    if any(fnmatchcase(key, pattern) for key in values for pattern in RESERVED):
        raise ValueError('Reserved runtime environment key in custom_env.ini; use the systemd instance configuration')


if __name__ == '__main__':
    validate(read_overrides(sys.argv[1]))
