"""Advance real native compilation with target-derived compatibility shims.

Run after qualify.py has made its separate source mirror. No Python fallback or
auto-python is used for these builds. Every failure remains in the receipt.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qualify import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codon', type=Path, required=True)
    parser.add_argument('--view', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    for name in ('codon', 'view', 'source', 'out'):
        setattr(args, name, getattr(args, name).resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    staged = args.out / 'source'
    view_path, staged_path = args.view.resolve(), staged.resolve()
    if view_path == staged_path or view_path in staged_path.parents or staged_path in view_path.parents:
        raise SystemExit('--view and --out/source must be separate directory trees')
    if staged.is_symlink():
        staged.unlink()
    elif staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(args.view, staged)
    args.view = staged
    env = dict(os.environ, CODON_PATH=str(args.view), OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    receipt = {'whole_stack_native': False, 'python_fallback': False, 'builds': {}}
    def save():
        (args.out / 'native-port.json').write_text(json.dumps(receipt, indent=2) + '\n')
    headers = run(['cc', '-dM', '-E', '-include', 'errno.h', '-x', 'c', '/dev/null'], args.source, env, 15)
    receipt['target_headers'] = headers
    if headers.get('returncode') != 0:
        save()
        raise SystemExit('Cannot derive target errno constants')
    macros = dict(re.findall(r'^#define (\w+) (.+)$', headers['stdout'], re.M))
    def resolve(name, seen=()):
        if name in seen:
            raise ValueError('C macro cycle: ' + name)
        value = macros[name].strip()
        return resolve(value, (*seen, name)) if value in macros else int(value, 0)
    values = {name: resolve(name) for name in ('EINTR', 'EAGAIN', 'EWOULDBLOCK', 'EISCONN', 'EALREADY', 'EINPROGRESS',
                                              'EACCES', 'EDEADLK', 'ENOSPC')}
    shim = args.view / 'errno.codon'
    shim.write_text('# Generated from target <errno.h>; no runtime Python dependency.\n' +
                    ''.join(f'{name}: Literal[int] = {value}\n' for name, value in values.items()))
    receipt['errno_values'] = values
    receipt['errno_shim_sha256'] = hashlib.sha256(shim.read_bytes()).hexdigest()
    probe = args.out / 'check_errno.codon'
    probe.write_text('import errno\n' + ''.join(f'assert errno.{name} == {value}\n' for name, value in values.items()) +
                     'assert {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR, 10035} == {0,4,11,114,115,10035}\nprint("NATIVE_ERRNO_OK")\n')
    receipt['linux_fallback_adaptation'] = ('Codon getattr(module, missing_attribute, default) fails statically; '
        'Linux has no WSAEWOULDBLOCK; specialize that known fallback to 10035 in native code')
    candidates = {'errno_shim': probe, 'hermes_cli': args.view / 'hermes_cli/main.codon',
                  'conversation_agent': args.view / 'run_agent.codon',
                  'session_database': args.view / 'hermes_state.codon',
                  'locale_catalog': args.view / 'agent/i18n.codon'}
    for name, source in candidates.items():
        executable = args.out / name
        result = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                  'build': run([str(args.codon), 'build', '-release', '-mcpu=cortex-a72',
                                '-o', str(executable), str(source)], args.source, env, 90)}
        if name == 'errno_shim' and result['build'].get('returncode') == 0:
            result['execution'] = run([str(executable)], args.source, env, 15)
            result['semantics_passed'] = (result['execution'].get('returncode') == 0 and
                                           result['execution']['stdout'].strip() == 'NATIVE_ERRNO_OK')
        receipt['builds'][name] = result
        save()
        print(name, result['build'].get('returncode'), flush=True)
    # Codon's parser rejects Python's multiline class-base header. Normalize
    # only that syntax in the disposable mirror, retaining original line count.
    for name in ('conversation_agent', 'session_database'):
        source = candidates[name]
        text = source.read_text()
        def compact(match):
            bases = ' '.join(line.strip() for line in match[2].splitlines()).rstrip(', ')
            header = f'class {match[1]}({bases}):'
            return header + '\n' * match[0].count('\n')
        adapted, changes = re.subn(r'(?m)^class (\w+)\(\n((?:[ \t]+[^\n]+\n)+)\):', compact, text)
        if changes:
            source.write_text(adapted)
            result = {'adaptation': 'Flatten multiline class-base header; preserve line count',
                      'headers_changed': changes, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                      'build': run([str(args.codon), 'build', '-release', '-mcpu=cortex-a72',
                                    '-o', str(args.out / (name + '_adapted')), str(source)], args.source, env, 90)}
            receipt['builds'][name + '_adapted'] = result
            save()
            print(name + '_adapted', result['build'].get('returncode'), flush=True)
    receipt['errno_addressed_natively'] = receipt['builds']['errno_shim'].get('semantics_passed', False)
    receipt['native_entrypoint_passed'] = receipt['builds']['hermes_cli']['build'].get('returncode') == 0
    save()
    if not receipt['errno_addressed_natively']:
        raise SystemExit(1)
    print('NATIVE_PORT_ATTEMPTS_COMPLETE', flush=True)


if __name__ == '__main__':
    main()
