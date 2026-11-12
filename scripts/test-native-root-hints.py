"""Exercise the packaged updater's actual file_update function without network."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--updater', type=Path, required=True)
    args = parser.parse_args()
    text = args.updater.read_text()
    function = re.search(r'(?ms)^file_update\(\) \{\n.*?^\}', text).group()
    settings = re.search(r'(?ms)^update_file="/tmp/named.cache"\n.*?(?=^file_update_try$)', text).group()
    before, after = b'; existing root hints\n', b'; verified new root hints\n'
    for mode in ('success', 'bad_download', 'network_error'):
        with tempfile.TemporaryDirectory(prefix='lovestdns-root-hints-') as directory:
            root = Path(directory)
            target = root / 'named.cache'
            target.write_bytes(before)
            source = (function + '\n' + r'''
wait_apply() { :; }
sleep() { :; }
mosdns() {
    if [ "$2" = "$newsum_url" ]; then
        if [ "$MODE" = network_error ]; then return 1; fi
        printf '%s\n' "$EXPECTED_MD5"
    else
        if [ "$MODE" = bad_download ]; then
            printf 'invalid download' >"${@: -1}"
            return 1
        fi
        printf '%s' "$NEXT_HINTS" >"${@: -1}"
    fi
}
''' + '\n' + settings + '\nfile_update\n').replace('/tmp/', root.as_posix() + '/')
            result = subprocess.run(['bash', '-c', source], capture_output=True, text=True,
                                    env={**os.environ, 'MODE': mode, 'NEXT_HINTS': after.decode(),
                                         'EXPECTED_MD5': hashlib.md5(after).hexdigest()}, timeout=10)
            expected_status = 0 if mode == 'success' else 1
            assert result.returncode == expected_status, (mode, result.stdout, result.stderr)
            assert target.is_file(), f'{mode}: updater removed active root hints'
            assert target.read_bytes() == (after if mode == 'success' else before), f'{mode}: active hints corrupted'
            assert not (root / 'named.cache.download').exists(), f'{mode}: staging file not cleaned'
            print(mode + ': passed')


if __name__ == '__main__':
    main()
