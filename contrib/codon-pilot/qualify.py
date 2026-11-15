"""Compile and execute real Codon compatibility probes, retaining all diagnostics.

Each probe is independent. The report distinguishes native fixes from CPython
boundaries; a hybrid success never changes whole_stack_native to true.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import signal
import shutil
import hashlib


BASE_ASYNC = '''import asyncio
async def value():
    await asyncio.sleep(0)
    return 7
async def main():
    BODY
asyncio.run(main())
'''
PROBES = {
    'native_hello': 'print("7")\n',
    'dynamic_fields': '''from sys import argv
class Agent:
    def __init__(self): self.retries = 0
agent = Agent()
name = argv[1] if len(argv)>1 else "retries"
setattr(agent, name, 7)
print(getattr(agent, name))
''',
    'declared_fields_fix': '''class Agent:
    def __init__(self): self.retries = 0
agent = Agent()
agent.retries = 7
print(agent.retries)
''',
    'dynamic_method': '''class Context:
    def __init__(self): self.name = "local"
def register(self, value): return self.name + value
setattr(Context, "register_custom", register)
print(Context().register_custom("-ok"))
''',
    'static_method_fix': '''class Context:
    def __init__(self): self.name = "local"
    def register_custom(self, value): return self.name + value
print(Context().register_custom("-ok"))
''',
    'dynamic_import': 'import importlib\nmodule = importlib.import_module("json")\nprint(module.loads(\'{"id": 7}\')["id"])\n',
    'python_import_boundary': 'from python import importlib\nmodule = importlib.import_module("json")\nprint(int(module.loads(\'{"id": 7}\')["id"]))\n',
    'contextvars': 'import contextvars\njob=contextvars.ContextVar("job",default="missing")\njob.set("J1")\nprint(contextvars.copy_context().run(job.get))\n',
}
for name, body in {
    'async_baseline': 'print(await asyncio.create_task(value()))',
    'async_to_thread': 'print(await asyncio.to_thread(lambda: 7))',
    'async_executor': 'print(await asyncio.get_running_loop().run_in_executor(None, lambda: 7))',
    'async_timeout': 'print(await asyncio.wait_for(value(), timeout=1))',
    'async_gather_exceptions': 'print(await asyncio.gather(value(), return_exceptions=True))',
}.items():
    PROBES[name] = BASE_ASYNC.replace('BODY', body)
PROBES['python_async_boundary'] = '''@python
def probe() -> int:
    import asyncio
    import contextvars
    async def main():
        job = contextvars.ContextVar("job", default="missing")
        job.set("J1")
        assert await asyncio.to_thread(job.get) == "J1"
        cleaned = []
        async def worker():
            try: await asyncio.sleep(10)
            finally: cleaned.append(True)
        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        task.cancel()
        outcomes = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert cleaned == [True]
        await asyncio.wait_for(asyncio.sleep(0), timeout=1)
        return 7
    return asyncio.run(main())
print(probe())
'''
EXPECTED = {name: '7' for name in PROBES}
EXPECTED.update(dynamic_method='local-ok', static_method_fix='local-ok', contextvars='J1')
EXPECTED['async_gather_exceptions'] = '[7]'


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*.codon')):
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def probe_fingerprint(source, toolchain):
    return hashlib.sha256(json.dumps({'source': source, 'toolchain': toolchain},
                                    sort_keys=True).encode()).hexdigest()


def probe_reusable(old, source, toolchain, executable):
    if not old or old.get('fingerprint') != probe_fingerprint(source, toolchain):
        return False
    build = old['build']
    if build.get('returncode') is None or build.get('timed_out'):
        return False
    if build['returncode'] != 0:
        return True
    return (executable.is_file() and old.get('executable_sha256') == file_hash(executable)
            and old.get('execution', {}).get('returncode') is not None)


def run(command, cwd, env, timeout):
    start = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        stdout, stderr = process.communicate(timeout=timeout)
        return dict(command=command, returncode=process.returncode, stdout=stdout,
                    stderr=stderr, wall_s=time.monotonic() - start)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return dict(command=command, timed_out=True, stdout=stdout, stderr=stderr,
                    wall_s=time.monotonic() - start)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codon', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--python-library', type=Path, required=True)
    parser.add_argument('--python-home', type=Path, required=True)
    parser.add_argument('--cpu', default='cortex-a72')
    parser.add_argument('--resume', action='store_true', help='Reuse unchanged probe sources and captured executions')
    args = parser.parse_args()
    for name in ('codon', 'source', 'out', 'python_library', 'python_home'):
        setattr(args, name, getattr(args, name).resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    # Codon's search roots look for .codon modules, while Hermes ships .py.
    # A separate source copy fixes resolution without changing installed source.
    # Symlinks are unsuitable because the importer canonicalizes their targets.
    view = args.out / 'native-source'
    if args.source == view or args.source in view.parents or view in args.source.parents:
        raise SystemExit('The generated native-source tree must be separate from installed source')
    # Removed/renamed installed modules must not survive in a resumed mirror.
    if view.is_symlink():
        view.unlink()
    elif view.exists():
        shutil.rmtree(view)
    packages = ('agent', 'gateway', 'hermes_cli', 'tools', 'pm', 'providers', 'cron',
                'acp_adapter', 'plugins', 'environments')
    sources = list(args.source.glob('*.py'))
    for package in packages:
        directory = args.source / package
        if directory.is_dir():
            sources.extend(directory.rglob('*.py'))
    for source in sources:
        path = (view / source.relative_to(args.source)).with_suffix('.codon')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        # Deferred annotation imports have no native module. Codon resolves its
        # annotations statically; keep line numbers stable for diagnostics.
        text = source.read_text()
        text = text.replace('from __future__ import annotations', '# Native static annotations')
        path.write_text(text)
    env = dict(os.environ, CODON_PYTHON=str(args.python_library), PYTHONHOME=str(args.python_home),
               HERMES_CODON_SOURCE=str(args.source), HERMES_DISABLE_LAZY_INSTALLS='1',
               CODON_PATH=str(view), OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    sdk_libraries = args.codon.resolve().parent.parent / 'lib/codon'
    toolchain = {'compiler': file_hash(args.codon), 'python_library': file_hash(args.python_library),
                 'compiler_path': str(args.codon), 'python_library_path': str(args.python_library),
                 'cpu': args.cpu, 'flags': ['build', '-release'],
                 'sdk_stdlib': source_tree_hash(sdk_libraries / 'stdlib'),
                 'native_source': source_tree_hash(view),
                 'sdk': {p.name: file_hash(p) for p in sorted(sdk_libraries.glob('*.so'))}}
    # Build native CLI directly before testing feature-specific blockers.
    previous = {}
    receipt_path = args.out / 'qualification.json'
    if args.resume and receipt_path.exists():
        previous = json.loads(receipt_path.read_text())
    report = dict(whole_stack_native=False, toolchain=toolchain, probes={}, native_entrypoint=run(
        [str(args.codon), 'build', '-release', '-mcpu=' + args.cpu,
         '-o', str(args.out / 'native-hermes'), str(view / 'hermes_cli/main.codon')],
        args.source, env, 90))
    (args.out / 'qualification.json').write_text(json.dumps(report, indent=2) + '\n')
    for name, source in PROBES.items():
        path = args.out / ('probe_' + name + '.codon')
        path.write_text(source)
        executable = args.out / name
        old = previous.get('probes', {}).get(name)
        fingerprint = probe_fingerprint(source, toolchain)
        legacy = args.out / (name + '.codon')
        if name == 'contextvars' and legacy.exists() and legacy.read_text() == source:
            legacy.unlink()
        reusable = probe_reusable(old, source, toolchain, executable)
        result = dict(old, reused_unchanged_probe=True) if reusable else dict(build=run(
            [str(args.codon), 'build', '-release', '-mcpu=' + args.cpu,
             '-o', str(executable), str(path)], args.source, env, 90))
        # Reuse compilation, but always recheck executable behavior: embedded
        # Python's stdlib can change independently of the compiler binary.
        if result['build'].get('returncode') == 0:
            result['execution'] = run([str(executable)], args.source, env, 30)
        if 'execution' in result:
            result['semantics_passed'] = (result['execution'].get('returncode') == 0 and
                                          result['execution'].get('stdout', '').strip() == EXPECTED[name])
        result['fingerprint'] = fingerprint
        if result['build'].get('returncode') == 0 and executable.is_file():
            result['executable_sha256'] = file_hash(executable)
        report['probes'][name] = result
        (args.out / 'qualification.json').write_text(json.dumps(report, indent=2) + '\n')
        print(name, result['build'].get('returncode'), result.get('execution', {}).get('returncode'), flush=True)
    launcher = Path(__file__).with_name('hermes.codon')
    shutil.copy2(launcher.with_name('runtime_compat.py'), args.out / 'runtime_compat.py')
    report['hybrid_build'] = run([str(args.codon), 'build', '-release', '-mcpu=' + args.cpu,
                                  '-o', str(args.out / 'hermes-codon'), str(launcher)], args.source, env, 90)
    if report['hybrid_build'].get('returncode') == 0:
        report['hybrid_version'] = run([str(args.out / 'hermes-codon'), '--version'], args.source, env, 45)
        report['hybrid_cli_error'] = run([str(args.out / 'hermes-codon'), '--codon-invalid-option'], args.source, env, 45)
    positives = ('native_hello', 'declared_fields_fix', 'static_method_fix',
                 'python_import_boundary', 'async_baseline', 'python_async_boundary')
    report['compatibility_passed'] = (all(report['probes'][name].get('semantics_passed') for name in positives)
        and report.get('hybrid_version', {}).get('returncode') == 0
        and report.get('hybrid_cli_error', {}).get('returncode') == 2)
    report['compatibility_scope'] = 'Positive native controls and hybrid CPython interoperability; not native Hermes'
    (args.out / 'qualification.json').write_text(json.dumps(report, indent=2) + '\n')
    print('CODON_QUALIFICATION_COMPLETE', flush=True)
    if not report['compatibility_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
