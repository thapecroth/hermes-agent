"""Repair Codon 0.20.6's CLI allocator CPU requirement on Cortex-A72.

The official SDK must already be extracted and its published digest verified.
This builds a sibling executable, retaining the supplied compiler/runtime libraries.
The upstream full-build equivalent is -DCODON_USE_MIMALLOC=OFF.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import os
import signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    source = args.out / 'codon-main-0.20.6.cpp'
    url = 'https://raw.githubusercontent.com/exaloop/codon/v0.20.6/codon/app/main.cpp'
    urllib.request.urlretrieve(url, source)
    command = ['g++', '-std=c++20', '-O2', '-march=armv8-a', '-mtune=cortex-a72',
               '-moutline-atomics', '-D_GLIBCXX_USE_CXX11_ABI=0', '-DFMT_HEADER_ONLY',
               '-I' + str(sdk / 'include'), str(source), '-L' + str(sdk / 'lib/codon'),
               '-Wl,-rpath,$ORIGIN/../lib/codon', '-Wl,-rpath-link,' + str(sdk / 'lib/codon'),
               '-lcodonc', '-lcodon_jupyter', '-lcodonrt', '-pthread', '-ldl', '-lrt',
               '-latomic', '-o', str(sdk / 'bin/codon-a72')]
    start = time.monotonic()
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    receipt = {'source': url, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
               'command': command, 'returncode': process.returncode, 'timed_out': timed_out,
               'stdout': stdout, 'stderr': stderr,
               'wall_s': time.monotonic() - start}
    (args.out / 'compiler-rebuild.json').write_text(json.dumps(receipt, indent=2) + '\n')
    if process.returncode:
        raise SystemExit(process.returncode)
    subprocess.run([str(sdk / 'bin/codon-a72'), '--version'], check=True, timeout=15)


if __name__ == '__main__':
    main()
