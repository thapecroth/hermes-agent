"""Disposable loopback-only form for the dashboard/noVNC E2E.

Run with the activated, installed Hermes environment; stop with Ctrl-C after QA.
The only browser commands target the synthetic form's fixed controls.
"""
import importlib.util
import hashlib
import json
import shutil
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

from hermes_constants import get_hermes_home

home = get_hermes_home()
plugin = home / "plugins/resource-harness"
spec = importlib.util.spec_from_file_location("screen_fixture", plugin / "__init__.py", submodule_search_locations=[str(plugin)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from screen_fixture.interactive_browser import request
from screen_fixture.browser_auth import state_path, write_private

submissions = []
visits = []
identity = "dashboard-e2e-" + uuid.uuid4().hex


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/result":
            return self.reply(json.dumps({"submissions": submissions, "visits": visits}), "application/json")
        visits.append(self.path)
        self.reply("""<!doctype html><meta charset="utf-8"><title>Bot Screen form test</title>
        <style>body{background:#edf2f3;color:#18343b;font:20px system-ui;padding:40px}
        main{max-width:640px;margin:30px auto;background:white;padding:36px;border-radius:12px}
        label,input,button{display:block;margin:16px 0}input{font:inherit;padding:10px;width:90%}
        button{font:inherit;background:#006b66;color:white;padding:12px 20px;border:0;border-radius:6px}
        p{line-height:1.5}</style><main><h1>Complete a form together</h1>
        <p>This local fixture uses JavaScript and CSS. No information leaves the Pi.</p>
        <form><label for="name">Visitor name</label><input id="name" autocomplete="off">
        <button id="submit">Save fixture</button></form><p id="result" role="status">Ready for human input.</p>
        <script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();
        const name=document.querySelector('#name').value;
        await fetch('/submit',{method:'POST',body:JSON.stringify({name})});
        document.querySelector('#result').textContent='Saved '+name;};</script></main>""")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 4096:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if self.path == "/prepare":
            # The live dashboard can control this existing worker through its
            # dashboard identity, while QA starts with no personal credentials.
            write_private(state_path(home, identity), {"version": 1, "saved_at": time.time(),
                                                       "cookies": [], "origins": []})
            result = request(home, "open", identity=identity, url="http://127.0.0.1:19120")
        elif self.path == "/submit":
            submissions.append(json.loads(body)["name"])
            result = {"saved": True}
        elif self.path == "/focus":
            result = request(home, "fill", selector="#name", value="")
        elif self.path == "/fence":
            result = request(home, "inspect")
            result = {"error": result.get("error"), "success": result.get("success", False)}
        else:
            self.send_error(404)
            return
        self.reply(json.dumps(result), "application/json")

    def reply(self, text, content_type="text/html; charset=utf-8"):
        payload = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 19120), Fixture) as server:
        print("Synthetic dashboard fixture ready on 127.0.0.1:19120", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            owner = home / "runtime/resource-harness/interactive/owner.json"
            def owned():
                return owner.exists() and json.loads(owner.read_text()).get("identity") == identity
            if owned():
                result = request(home, "close", identity=identity)
                if result.get("error"):
                    raise RuntimeError("QA browser still needs cleanup: " + result["error"])
            deadline = time.monotonic() + 20
            while owned():
                if time.monotonic() >= deadline:
                    raise RuntimeError("QA browser cleanup is still pending")
                time.sleep(.1)
            path = state_path(home, identity)
            path.unlink(missing_ok=True)
            path.with_suffix(".lock").unlink(missing_ok=True)
            profile = home / "browser-auth/rendered" / hashlib.sha256(identity.encode()).hexdigest()
            if profile.exists():
                shutil.rmtree(profile)
            print("Synthetic QA profile removed", flush=True)
