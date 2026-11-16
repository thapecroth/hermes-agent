"""Install a pinned ARM64 Lightpanda with a private libc; never upgrade the host."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.0.4"
BROWSER = (
    "https://github.com/lightpanda-io/browser/releases/download/1.0.4/lightpanda-aarch64-linux",
    "f021d49a0b66d07bf2633f8e00870b9075293516f872695b0ca4aeb5848b6ea8",
)
# SHA256 from Debian trixie's official arm64 Packages index, November 15, 2026.
LIBC = (
    "https://deb.debian.org/debian/pool/main/g/glibc/libc6_2.41-12+deb13u4_arm64.deb",
    "8784eda966b189c777a384dac5ce009e8fc9b52d006926c5a013e7fa8aa688cc",
)


def download(url, digest, destination):
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        return
    with urlopen(url, timeout=90) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError(f"SHA256 mismatch for {destination.name}")
    temporary = destination.with_suffix(".partial")
    temporary.write_bytes(payload)
    temporary.replace(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        parser.error("this pinned installer targets Linux ARM64")
    if not shutil.which("dpkg-deb"):
        parser.error("dpkg-deb is needed to extract the private runtime")
    prefix = args.prefix.resolve()
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "libexec").mkdir(exist_ok=True)
    native = prefix / "libexec/lightpanda"
    package = prefix / "libc6.deb"
    download(*BROWSER, native)
    download(*LIBC, package)
    native.chmod(0o755)
    runtime = prefix / "glibc"
    subprocess.run(["dpkg-deb", "--extract", str(package), str(runtime)],
                   check=True, stdin=subprocess.DEVNULL, timeout=30)
    loaders = [p for p in runtime.rglob("ld-linux-aarch64.so.1") if p.is_file()]
    if not loaders:
        raise RuntimeError("private libc has no ARM64 dynamic loader")
    libraries = loaders[0].resolve().parent
    # Hermes's orphan reaper requires 'lightpanda' in /proc/<pid>/comm.
    loader = prefix / "libexec/lightpanda-ld"
    shutil.copy2(loaders[0].resolve(), loader)
    loader.chmod(0o755)
    wrapper = prefix / "bin/lightpanda"
    wrapper.write_text("#!/bin/sh\nexport LIGHTPANDA_DISABLE_TELEMETRY=true\nexec " + shlex.quote(str(loader)) +
                       " --library-path " + shlex.quote(str(libraries)) + " " +
                       shlex.quote(str(native)) + ' "$@"\n')
    wrapper.chmod(0o755)
    proc = subprocess.run([str(wrapper), "help"], check=True, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=15)
    if "serve" not in proc.stdout + proc.stderr:
        raise RuntimeError("installed Lightpanda does not advertise its CDP server")
    receipt = {"lightpanda_version": VERSION, "binary_sha256": BROWSER[1],
               "private_libc_version": "2.41-12+deb13u4", "libc_sha256": LIBC[1],
               "prefix": str(prefix), "wrapper": str(wrapper),
               "host_libraries_changed": False, "help_verified": True}
    (prefix / "installation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
