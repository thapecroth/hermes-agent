"""Start and recover a disposable real systemd service, never the user's gateway."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path


FIXTURE = '''import json,os,sys
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
root=Path(sys.argv[1])
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'{"status":"ok"}'
  self.send_response(200);self.end_headers();self.wfile.write(body)
 def log_message(self,*_):pass
server=HTTPServer(("127.0.0.1",0),H)
state={"pid":os.getpid(),"platforms":{"api_server":{"state":"connected","listener_base":f"http://127.0.0.1:{server.server_port}"},"telegram":{"state":"connected"}}}
temp=root/"state.tmp"
temp.write_text(json.dumps(state));temp.replace(root/"gateway_state.json")
server.serve_forever()
'''


def main():
    source = Path(__file__).resolve().parent
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
        deadline = time.monotonic() + 30
        while not check():
            assert time.monotonic() < deadline, "systemd fixture did not reach expected state"
            pause.wait(0.1)
    with tempfile.TemporaryDirectory(prefix="rh-watchdog-e2e-") as temp:
        home = Path(temp)
        fixture = home / "fixture.py"
        fixture.write_text(FIXTURE)
        command = [sys.executable, str(source / "watchdog.py"), "--home", str(home),
                   "--unit", unit, "--startup-grace-seconds", "0"]
        try:
            subprocess.run(["systemd-run", "--user", "--unit", unit, "--collect",
                            "/usr/bin/python3", str(fixture), str(home)], check=True, timeout=10)
            until(lambda: home.joinpath("gateway_state.json").exists())
            original = pid()
            state = json.loads(home.joinpath("gateway_state.json").read_text())
            state["platforms"]["telegram"] = {"state": "fatal", "error_code": "injected_fixture_fault"}
            home.joinpath("gateway_state.json").write_text(json.dumps(state))
            subprocess.run(command, check=True, timeout=15)
            assert pid() == original, "restarted before the second unhealthy observation"
            subprocess.run(command, check=True, timeout=15)
            until(lambda: pid() not in (0, original))
            replacement = pid()
            until(lambda: json.loads(home.joinpath("gateway_state.json").read_text())["pid"] == replacement)
            subprocess.run(command, check=True, timeout=15)
            control("stop", unit).check_returncode()
            subprocess.run(command, check=True, timeout=15)
            assert pid() == 0, "watchdog resurrected a manually stopped service"
            receipt = {"passed": True, "unit": unit, "original_pid": original,
                       "replacement_pid": replacement, "manual_stop_preserved": True}
            artifact.write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt))
        finally:
            result = control("stop", unit)
            if result.returncode and "not loaded" not in result.stderr:
                raise RuntimeError(result.stderr)
            check = control("show", unit, "-p", "MainPID", "--value")
            assert check.returncode == 0 and check.stdout.strip() == "0", "fixture process survived cleanup"


if __name__ == "__main__":
    main()
