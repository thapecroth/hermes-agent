"""Opt-in direct agent-browser + Lightpanda smoke; run in a private trial cgroup."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-browser', type=Path, required=True)
    parser.add_argument('--lightpanda', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    import psutil
    work = Path(tempfile.mkdtemp(prefix='hermes-agent-browser-lp-'))
    (work / 'tmp').mkdir()
    sockets = work / 'sockets'
    sockets.mkdir()
    name = 'lp-check-' + uuid.uuid4().hex[:12]
    env = {'HOME': str(work), 'TMPDIR': str(work / 'tmp'), 'PATH': os.environ['PATH'],
           'AGENT_BROWSER_SOCKET_DIR': str(sockets), 'LIGHTPANDA_DISABLE_TELEMETRY': 'true'}
    prefix = [str(args.agent_browser), '--engine', 'lightpanda', '--executable-path',
              str(args.lightpanda), '--session', name, '--json']
    def call(*arguments):
        proc = subprocess.run(prefix + list(arguments), env=env, cwd=work, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=25)
        if proc.returncode:
            raise RuntimeError(proc.stdout + proc.stderr)
        value = json.loads(proc.stdout)
        assert value.get('success') is True, value
        return value

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<html><head><title>Direct Lightpanda proof</title></head><body><input id="field"></body></html>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    processes = []
    closed = False
    failure = None
    receipt = {'passed': False, 'engine': 'lightpanda', 'isolated_workdir': str(work),
               'local_only': True, 'cleanup_verified': False}
    try:
        version = subprocess.run([str(args.agent_browser), '--version'], check=True, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10).stdout.strip()
        call('open', f'http://127.0.0.1:{server.server_port}')
        title = call('eval', 'document.title')
        assert 'Direct Lightpanda proof' in json.dumps(title), title
        filled = call('eval', 'document.querySelector("#field").value="typed-value";document.querySelector("#field").value')
        assert 'typed-value' in json.dumps(filled), filled
        pid = int((sockets / (name + '.pid')).read_text().strip())
        daemon = psutil.Process(pid)
        assert daemon.environ().get('AGENT_BROWSER_SOCKET_DIR') == str(sockets), 'daemon ownership mismatch'
        processes = [daemon, *daemon.children(recursive=True)]
        for process in processes:
            process.create_time()
        engines = [process for process in processes if 'lightpanda' in process.name().lower()]
        assert len(engines) == 1, [(p.pid, p.name()) for p in processes]
        call('close')
        closed = True
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from browser_cleanup import finish_cleanup
        finish_cleanup(processes)
        receipt = {'passed': True, 'agent_browser_version': version, 'engine': 'lightpanda',
                   'local_only': True, 'title_and_form_verified': True,
                   'engine_instances': len(engines), 'cleanup_verified': True}
        shutil.rmtree(work)
    except Exception as exc:
        failure = exc
        receipt['error'] = str(exc)[:1500]
    finally:
        try:
            if not closed:
                try:
                    call('close')
                except Exception as exc:
                    receipt['cleanup_error'] = str(exc)[:1500]
        finally:
            server.shutdown()
            server.server_close()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)
    if failure:
        raise RuntimeError('Direct agent-browser/Lightpanda qualification failed; see ' + str(args.out)) from failure


if __name__ == '__main__':
    main()
