"""Resource-triggered restart and durable continuation in a disposable systemd unit."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

FIXTURE = r'''import asyncio,json,os,sys,threading
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
root=Path(sys.argv[1]);sys.path.insert(0,sys.argv[2])
from manager import Manager
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'{"status":"ok"}'
  self.send_response(200);self.end_headers();self.wfile.write(body)
 def log_message(self,*_):pass
server=HTTPServer(("127.0.0.1",0),H)
state={"pid":os.getpid(),"platforms":{"api_server":{"state":"connected","listener_base":f"http://127.0.0.1:{server.server_port}"},"telegram":{"state":"connected"}}}
temp=root/"state.tmp";temp.write_text(json.dumps(state));temp.replace(root/"gateway_state.json")
threading.Thread(target=server.serve_forever,daemon=True).start()
class Runtime:
 async def execute(self,job):
  if job['attempt']==0:
   with (root/'effects.jsonl').open('a') as f:f.write(json.dumps({'job':job['id'],'effect':'created-once'})+'\n')
   (root/'started').write_text(str(os.getpid()))
   if (root/'exit-as-resource').exists():os._exit(1)
   await asyncio.Event().wait()
  assert 'previous request exceeded resource' in job['recovery_context']
  assert len((root/'effects.jsonl').read_text().splitlines())==1
  (root/'resumed.json').write_text(json.dumps({'job':job['id'],'attempt':job['attempt'],'warning_received':True}))
  return 'completed','Resumed from saved progress without repeating the side effect.'
 async def classify(self,*a):return {'action':'new'}
 async def send(self,route,text,identity):
  with (root/'sends.jsonl').open('a') as f:f.write(json.dumps({'id':identity,'text':text})+'\n')
  return True
 def cancel(self,*a):return True
 def steer(self,*a):return True
async def main():
 m=Manager(root/'runtime/resource-harness',Runtime(),recover=False)
 if not m.store.jobs('test'):
  await m.admit('test','m1',{'session_key':'test'},'Do the durable fixture work')
 await m.serve()
asyncio.run(main())
'''


def main():
    source = Path(__file__).resolve().parent
    sys.path.insert(0, str(source))
    from watchdog import restart_and_confirm
    unit = "hermes-resource-e2e-" + uuid.uuid4().hex[:10] + ".service"
    artifact = Path(sys.argv[1])
    pause = threading.Event()
    def control(*args):
        return subprocess.run(["systemctl", "--user", *args], text=True, capture_output=True, timeout=10)
    def pid():
        result = control("show", unit, "-p", "MainPID", "--value")
        result.check_returncode()
        return int(result.stdout.strip())
    def until(check):
        deadline = time.monotonic() + 40
        while not check():
            assert time.monotonic() < deadline, "systemd fixture did not reach expected state"
            pause.wait(0.1)
    with tempfile.TemporaryDirectory(prefix="rh-watchdog-e2e-") as temp:
        home = Path(temp)
        fixture = home / "fixture.py"
        fixture.write_text(FIXTURE)
        exit_recorder = home / "record-exit.py"
        exit_recorder.write_text("import os,runpy,sys\n"
            f"sys.path.insert(0,{str(source)!r})\n"
            "if os.environ.get('SERVICE_RESULT')=='exit-code':os.environ['SERVICE_RESULT']='resources'\n"
            f"sys.argv={[str(source / 'watchdog.py'), '--home', str(home), '--unit', unit, '--record-exit']!r}\n"
            f"runpy.run_path({str(source / 'watchdog.py')!r},run_name='__main__')\n")
        unit_file = home / unit
        unit_file.write_text("[Service]\nType=simple\nMemoryMax=128M\nRestart=on-failure\nRestartSec=1\n"
            f"ExecStart=/usr/bin/python3 {fixture} {home} {source}\n"
            f"ExecStopPost=/usr/bin/python3 {exit_recorder}\n")
        unit_link = Path.home() / ".config/systemd/user" / unit
        assert not unit_link.exists(), "fixture unit path already exists"
        command = [sys.executable, str(source / "watchdog.py"), "--home", str(home),
                   "--unit", unit, "--startup-grace-seconds", "0"]
        try:
            control("link", str(unit_file)).check_returncode()
            control("start", unit).check_returncode()
            until(lambda: home.joinpath("started").exists())
            original = pid()
            subprocess.run(command, check=True, timeout=15)
            assert pid() == original, "healthy fixture restarted at default threshold"
            # Deliberately low test threshold exercises real cgroup measurement without
            # exhausting memory or changing the actual gateway's resource limits.
            pressure = [*command, "--resource-threshold", "0.01"]
            subprocess.run(pressure, check=True, timeout=15)
            assert pid() == original, "restarted before the second pressure observation"
            subprocess.run(pressure, check=True, timeout=15)
            until(lambda: pid() not in (0, original))
            replacement = pid()
            replacement_invocation = control("show", unit, "-p", "InvocationID", "--value")
            replacement_invocation.check_returncode()
            until(lambda: home.joinpath("resumed.json").exists())
            resumed = json.loads(home.joinpath("resumed.json").read_text())
            until(lambda: any('completed' in json.loads(line)['text'] for line in home.joinpath('sends.jsonl').read_text().splitlines()))
            notices = [json.loads(line)['text'] for line in home.joinpath('sends.jsonl').read_text().splitlines()]
            assert any('resource pressure' in text and 'Resuming automatically' in text for text in notices)
            assert resumed['attempt'] == 1 and resumed['warning_received']
            assert len(home.joinpath('effects.jsonl').read_text().splitlines()) == 1
            from manager import Store
            tickets = Store(home / "runtime/resource-harness").rows("SELECT * FROM recoveries WHERE state='consumed'")
            assert len(tickets) == 1 and tickets[0]['target_pid'] == replacement
            assert tickets[0]['target_invocation'] == replacement_invocation.stdout.strip()
            control("stop", unit).check_returncode()
            subprocess.run(pressure, check=True, timeout=15)
            assert pid() == 0, "watchdog resurrected a manually stopped service"
            try:
                restart_and_confirm(unit, {'MainPID': str(original), 'InvocationID': 'prior-observation'}, timeout=2)
            except RuntimeError as exc:
                assert 'service was stopped' in str(exc), exc
            else:
                raise AssertionError("successful no-op try-restart authorized continuation")
            # Inject the systemd resource-result label, then exercise a real
            # Restart=on-failure transition. No OOM or host memory pressure is induced.
            for name in ('manager.sqlite3', 'manager.sqlite3-wal', 'manager.sqlite3-shm'):
                home.joinpath('runtime/resource-harness', name).unlink(missing_ok=True)
            for name in ('started', 'resumed.json', 'effects.jsonl', 'sends.jsonl'):
                home.joinpath(name).unlink(missing_ok=True)
            home.joinpath('exit-as-resource').touch()
            control('start', unit).check_returncode()
            until(lambda: home.joinpath('resumed.json').exists())
            auto = json.loads(home.joinpath('resumed.json').read_text())
            auto_unit = control('show', unit, '-p', 'NRestarts', '-p', 'MainPID')
            auto_unit.check_returncode()
            auto_state = dict(line.split('=', 1) for line in auto_unit.stdout.splitlines() if '=' in line)
            assert int(auto_state['NRestarts']) == 1, auto_state
            assert auto['attempt'] == 1 and auto['warning_received']
            assert len(home.joinpath('effects.jsonl').read_text().splitlines()) == 1
            # A successful operator stop invalidates a still-outstanding resource
            # ticket. A later manual start must not replay its snapshotted attempt.
            store = Store(home / 'runtime/resource-harness')
            with store.db() as db:
                db.execute("UPDATE jobs SET state='running'")
            pending = store.prepare_recovery('systemd resource failure: resources', int(auto_state['MainPID']),
                                              source_unit=unit, source_restarts=0)
            store.recovery_state(pending, 'issued')
            control('stop', unit).check_returncode()
            assert store.rows('SELECT state FROM recoveries WHERE id=?', (pending,))[0]['state'] == 'failed'
            control('start', unit).check_returncode()
            until(lambda: store.jobs('test')[0]['state'] == 'interrupted')
            assert len(home.joinpath('effects.jsonl').read_text().splitlines()) == 1
            receipt = {"passed": True, "unit": unit, "original_pid": original,
                       "replacement_pid": replacement, "manual_stop_preserved": True,
                       "trigger": "real cgroup memory with deliberately low test threshold",
                       "automatic_resume_attempt": resumed['attempt'], "worker_warning_received": True,
                       "user_warning_delivered": True, "side_effect_count": 1,
                       "restart_noop_rejected": True, "recovery_bound_to_replacement_invocation": True,
                       "systemd_resource_exit_resume": True, "resource_result_injected": True,
                       "manual_restart_did_not_resume_resource_ticket": True}
            artifact.write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt))
        finally:
            result = control("stop", unit)
            if result.returncode and "not loaded" not in result.stderr:
                raise RuntimeError(result.stderr)
            check = control("show", unit, "-p", "MainPID", "--value")
            assert check.returncode == 0 and check.stdout.strip() == "0", "fixture process survived cleanup"
            if unit_link.is_symlink() and unit_link.resolve() == unit_file:
                unit_link.unlink()
            control('daemon-reload').check_returncode()
            assert not unit_link.exists(), 'fixture unit link survived cleanup'


if __name__ == "__main__":
    main()
