"""Build a private DNS-OARC dnsperf 2.14.0 binary with a benchmark recorder."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


RELEASE = 'dnsperf-2.14.0'
SHA256 = '134ba69744705bdb65ea57e25713300a6771f7cecbd5d6a74dd9472c18ac2696'
INCLUDE = Path(__file__).with_name('dnsperf-trace.inc')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def instrument(original):
    logging = '''                perf_log_printf(
                    "> %s %s %u.%06u",
                    perf_dns_rcode_strings[recvd[i].rcode],
                    recvd[i].desc,
                    (unsigned int)(latency / MILLION),
                    (unsigned int)(latency % MILLION));'''
    replacements = (
        ('} received_query_t;\n', '} received_query_t;\n\n#include "benchmark-trace.inc"\n'),
        ('    setup(argc, argv, &config);\n', '    setup(argc, argv, &config);\n    benchmark_trace_init(&config);\n'),
        ('    perf_os_thread_setname(tinfo->sender, name);\n',
         '    perf_os_thread_setname(tinfo->sender, name);\n    benchmark_trace_place_threads(tinfo);\n'),
        ('    times.start_time = perf_get_time();\n',
         '    times.start_time = perf_get_time();\n    benchmark_start = times.start_time;\n'),
        ('            latency = recvd[i].when - recvd[i].sent;\n',
         '            latency = recvd[i].when - recvd[i].sent;\n'
         '            if (benchmark_trace)\n                benchmark_trace_add(&recvd[i]);\n'),
        (logging, '                if (!benchmark_trace_deferred)\n' +
         '\n'.join('    ' + line for line in logging.splitlines())),
        ('    perf_net_stats_print(config.mode);\n', '    perf_net_stats_print(config.mode);\n    benchmark_trace_write();\n'),
    )
    for before, after in replacements:
        if original.count(before) != 1:
            raise ValueError('Unexpected dnsperf release source layout')
        original = original.replace(before, after)
    return original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('work', type=Path)
    parser.add_argument('--sysroot', type=Path, help='Optional isolated Debian development and runtime libraries')
    args = parser.parse_args()
    if sha(args.archive) != SHA256:
        parser.error('Source archive differs from the DNS-OARC release checksum')
    args.work.mkdir(mode=0o700)
    with tarfile.open(args.archive) as source:
        source.extractall(args.work, filter='data')
    src = args.work / RELEASE
    upstream = src / 'src/dnsperf.c'
    upstream.write_text(instrument(upstream.read_text()))
    shutil.copyfile(INCLUDE, src / 'src/benchmark-trace.inc')
    env = os.environ.copy()
    if args.sysroot:
        root = args.sysroot.resolve()
        triplet = subprocess.check_output(['cc', '-print-multiarch'], text=True).strip()
        lib = root / 'usr/lib' / triplet
        inc = root / 'usr/include'
        env.update(PKG_CONFIG=str(root / 'usr/bin/pkgconf'), PKG_CONFIG_SYSROOT_DIR=str(root),
                   PKG_CONFIG_LIBDIR=f'{lib}/pkgconfig:{root}/usr/share/pkgconfig', PKG_CONFIG_PATH='',
                   LD_LIBRARY_PATH=str(lib), CPPFLAGS=f'-I{inc} -I{inc / triplet}',
                   LDFLAGS=f'-L{lib} -Wl,--disable-new-dtags,-rpath,{lib}')
    with (args.work / 'build.log').open('xb') as log:
        for argv in (['./configure', 'CFLAGS=-O2 -g'], ['make', '-C', 'src', '-j2', 'dnsperf']):
            subprocess.run(argv, check=True, cwd=src, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
    binary = src / 'src/dnsperf'
    with (args.work / 'build.json').open('x') as output:
        json.dump({'source_sha256': SHA256, 'include_sha256': sha(INCLUDE), 'builder_sha256': sha(__file__),
                   'instrumented_source_sha256': sha(upstream), 'binary_sha256': sha(binary),
                   'libraries': subprocess.check_output(['ldd', str(binary)], text=True, timeout=10)}, output, indent=2)
        output.write('\n')
    print(binary)


if __name__ == '__main__':
    main()
