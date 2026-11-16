"""Real Hermes dispatch -> Lightpanda -> local oss-invisible Firefox.

Disposable profile and local synthetic auth/form only. Native human-control
fences, restart persistence, two-slot admission and descendant cleanup checked.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    artifact = Path(sys.argv[1])
    site_packages = sys.argv[2]
    source = Path(__file__).resolve().parent
    # Browser Use embeds the profile path in Unix socket names (108-byte cap).
    work = Path(tempfile.mkdtemp(prefix="hs-e2e-", dir="/tmp"))
    result = {"passed": False, "local_only": True, "engines": ["lightpanda", "oss-invisible Firefox"]}
    print("E2E_HOME", work, flush=True)
    plugin = work / "plugins/resource-harness"
    shutil.copytree(source, plugin, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    os.environ.update(HERMES_HOME=str(work), HERMES_RUNTIME_DIR=str(work / "runtime"))
    from pm.paths import store_root
    runtime = work / "runtime"
    runtime.mkdir()
    for asset in store_root().iterdir():
        if asset.is_dir():
            (runtime / asset.name).symlink_to(asset, target_is_directory=True)
    if (store_root() / "facts.json").exists():
        shutil.copy2(store_root() / "facts.json", runtime / "facts.json")
    import yaml
    config = {"plugins": {"enabled": ["resource-harness"], "entries": {"resource-harness": {"settings": {
        "browser_auth": True, "interactive_browser": True, "interactive_site_packages": site_packages,
        "interactive_binary": os.environ["HERMES_INVISIBLE_BINARY"],
        "queue_timeout_seconds": 15, "browser_timeout_seconds": 60, "telegram_manager": False}}}},
        "browser": {"backend": "browser-use", "engine": "lightpanda", "cloud_provider": "local", "allow_private_urls": True},
        "terminal": {"backend": "local", "cwd": str(work)},
        "bot_desktop": {"placement": "gateway", "min_free_memory_mb": 1024},
        "memory": {"memory_enabled": False, "user_profile_enabled": False}}
    (work / "config.yaml").write_text(yaml.safe_dump(config))
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(work)
    from agent.secret_scope import set_secret_scope
    set_secret_scope({}, profile_home=str(work))
    import model_tools
    from hermes_cli.plugins import discover_plugins, invoke_hook
    model_tools.discover_builtin_tools()
    discover_plugins(force=True)
    spec = importlib.util.spec_from_file_location("screen_e2e_harness", plugin / "__init__.py", submodule_search_locations=[str(plugin)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    from screen_e2e_harness.browser_auth import write_private, state_path
    from screen_e2e_harness.pool import occupied, Pool, CapacityTimeout
    from screen_e2e_harness.interactive_browser import request
    from tools.bot_desktop import runtime as desktop, lease
    import psutil

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            title = "Just a moment" if self.path.startswith("/challenge") else "Rendered form fixture"
            body = ("""<!doctype html><html><head><title>%s</title><style>
            body{background:#f3f4f6;color:#17262c;font:20px system-ui;padding:45px}main{max-width:650px;margin:auto;background:white;padding:30px}
            label,input,button{display:block;margin:14px 0}button{background:#006b66;color:white;padding:12px;border:0}</style></head>
            <body><main><h1>Rendered form fixture</h1><div id="auth"></div><form onsubmit="event.preventDefault();document.querySelector('#result').textContent='Saved '+document.querySelector('#name').value">
            <label for="name">Name</label><input id="name" name="name"><button id="submit">Save fixture</button></form><p id="result"></p>
            <a id="other-origin" href="http://localhost:%s/logout">Visit other origin and sign out</a></main>
            <script>if(location.pathname==='/logout'){localStorage.clear();sessionStorage.clear()}
            if(location.pathname==='/refresh' && localStorage.getItem('fixture-token')==='synthetic-only'){
              localStorage.setItem('fixture-token','refreshed-only');location.reload();
            }
            document.querySelector('#auth').textContent=localStorage.getItem('fixture-token')==='synthetic-only'?'authenticated':localStorage.getItem('fixture-token')==='refreshed-only'?'refreshed-auth':'signed-out';</script></body></html>""" % (title, self.server.server_port)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    other = f"http://localhost:{server.server_port}"
    seed = {"version": 1, "saved_at": time.time(), "cookies": [], "origins": [{"origin": base,
        "localStorage": [{"name": "fixture-token", "value": "synthetic-only"}], "sessionStorage": []},
        {"origin": other, "localStorage": [{"name": "fixture-token", "value": "synthetic-only"}], "sessionStorage": []}]}
    write_private(work / "browser-auth/chromium-seed.json", seed)
    owned = []

    def browser(code, sid="auth", turn="t1", success=True):
        output = model_tools.handle_function_call("browser_exec", {"code": code, "timeout_s": 60},
            session_id=sid, task_id=sid, turn_id=turn)
        data = json.loads(output) if isinstance(output, str) else output
        if success:
            assert data.get("success"), str(data)[:1500]
        return data

    def release(sid="auth", turn="t1"):
        invoke_hook("on_session_end", session_id=sid, task_id=sid, turn_id=turn)
        deadline = time.monotonic() + 25
        while any(r["owner"] == f"{sid}:{turn}" for r in occupied(work / "runtime/resource-harness")["browser"]):
            assert time.monotonic() < deadline, "research cleanup timed out"
            time.sleep(.1)

    try:
        output = browser(f"new_tab({base!r}); wait_for_load(); print(js(\"document.querySelector('#auth').textContent\"))")
        assert "authenticated" in output.get("output", ""), str(output)[:1200]
        result["real_browser_use_auth"] = True
        browser("new_tab('about:blank'); print('created fresh tab')")
        output = browser(f"goto_url({base!r}); wait_for_load(); print(js(\"document.querySelector('#auth').textContent\"))")
        assert "authenticated" in output.get("output", ""), str(output)[:1200]
        result["new_tab_auth"] = True
        browser("js('localStorage.clear(); sessionStorage.clear()'); raise RuntimeError('synthetic failure after logout')", success=False)
        release()
        output = browser(f"new_tab({base!r}); wait_for_load(); print(js(\"document.querySelector('#auth').textContent\"))", turn="t2")
        assert "signed-out" in output.get("output", ""), str(output)[:1200]
        release(turn="t2")
        result["logout_and_exception_survive_restart"] = True
        print("E2E: Browser Use auth/new-tab/logout passed", flush=True)

        output = browser(f"new_tab({(base + '/challenge')!r}); wait_for_load(); print(page_info())", sid="fallback")
        fallback = output.get("rendered_fallback", {})
        assert fallback.get("success"), str(output)[:1800]
        assert fallback.get("title") == "Just a moment", str(fallback)[:900]
        result["automatic_fallback"] = True
        assert desktop.status().running
        root = work / "runtime/resource-harness"
        slots = occupied(root)["browser"]
        assert len(slots) == 1 and slots[0]["owner"].startswith("interactive:"), slots
        worker = psutil.Process(slots[0]["pid"])
        owned = worker.children(recursive=True)
        for process in owned:
            process.create_time()
        with Pool(root, "browser", 2).acquire("second-browser-test", 0):
            try:
                Pool(root, "browser", 2).acquire("third-browser-test", 0)
            except CapacityTimeout:
                result["two_browser_cap"] = True
            else:
                raise AssertionError("third browser admitted")
        denied = request(work, "inspect", identity="other-job")
        assert denied.get("error") == "interactive_browser_busy_with_another_job", denied
        result["job_isolation"] = True
        lease.acquire("synthetic-human-viewer", reason="E2E takeover")
        denied = request(work, "fill", identity="fallback", selector="#name", value="must not be typed")
        assert denied.get("error") == "human_has_control", denied
        denied = request(work, "inspect", identity="fallback")
        assert denied.get("error") == "human_has_control", denied
        lease.release(viewer_id="synthetic-human-viewer")
        result["native_human_fence"] = True
        assert request(work, "fill", identity="fallback", selector="#name", value="Synthetic visitor").get("success")
        clicked = request(work, "click", identity="fallback", selector="#submit")
        assert "Saved Synthetic visitor" in clicked.get("text", ""), clicked
        assert "authenticated" in clicked.get("text", ""), clicked
        result["rendered_js_form_and_auth"] = True
        assert request(work, "close", identity="fallback").get("closing")
        # Reopen immediately; request must await the prior supervisor's cleanup.
        reopened = request(work, "open", identity="fallback", url=base)
        assert reopened.get("success") and "authenticated" in reopened.get("text", ""), reopened
        from screen_e2e_harness.browser_cleanup import verify_stopped
        # Desktop stays up until explicitly stopped below; only assert Firefox descendants.
        firefox = [p for p in owned if not p.is_running() or 'firefox' in p.name().lower()]
        verify_stopped(firefox, timeout=0)
        result["immediate_close_reopen_cleanup"] = True
        result["rendered_restart_auth"] = True
        logged_out = request(work, "click", identity="fallback", selector="#other-origin")
        assert "signed-out" in logged_out.get("text", ""), logged_out
        still_out = request(work, "goto", identity="fallback", url=other)
        assert "signed-out" in still_out.get("text", ""), still_out
        result["pending_origin_logout_does_not_restore_stale_auth"] = True
        assert request(work, "goto", identity="fallback", url=base).get("success")
        # Kill the executor, leaving its independent supervisor to reap the
        # entire Firefox tree before releasing the browser admission lease.
        worker_info = json.loads((root / "interactive/owner.json").read_text())
        worker = psutil.Process(worker_info["pid"])
        crashed_children = worker.children(recursive=True)
        for child in crashed_children:
            child.create_time()
        worker.kill()
        deadline = time.monotonic() + 20
        while occupied(root)["browser"]:
            assert time.monotonic() < deadline
            time.sleep(.1)
        verify_stopped(crashed_children, timeout=0)
        result["worker_crash_reaped_before_admission_released"] = True
        recovered = request(work, "open", identity="fallback", url=base)
        assert recovered.get("success") and "authenticated" in recovered.get("text", ""), recovered
        assert request(work, "fill", identity="fallback", selector="#name", value="After recovery").get("success")
        assert "Saved After recovery" in request(work, "click", identity="fallback", selector="#submit").get("text", "")
        request(work, "close", identity="fallback")
        deadline = time.monotonic() + 20
        while occupied(root)["browser"]:
            assert time.monotonic() < deadline
            time.sleep(.1)
        result["worker_crash_recovery"] = True
        output = browser(f"new_tab({base!r}); wait_for_load(); print(page_info())", sid="explicit")
        output = model_tools.handle_function_call("interactive_browser", {"action": "open", "url": base},
            session_id="explicit", task_id="explicit", turn_id="t1")
        output = json.loads(output) if isinstance(output, str) else output
        assert output.get("success") and "authenticated" in output.get("text", ""), output
        slots = occupied(root)["browser"]
        assert len(slots) == 1 and slots[0]["owner"] == "interactive:explicit", slots
        result["explicit_migration_releases_lightpanda"] = True
        assert request(work, "close", identity="explicit").get("closing")
        redirected = request(work, "open", identity="redirect-auth", url=base + "/redirect")
        assert redirected.get("success") and "authenticated" in redirected.get("text", ""), redirected
        assert redirected.get("url", "").startswith(other), redirected
        direct = request(work, "goto", identity="redirect-auth", url=base)
        assert "authenticated" in direct.get("text", ""), direct
        result["redirect_imports_destination_and_preserves_unvisited_origin"] = True
        assert request(work, "close", identity="redirect-auth").get("closing")
        refreshed = request(work, "open", identity="refresh-auth", url=base + "/refresh")
        assert refreshed.get("success") and "refreshed-auth" in refreshed.get("text", ""), refreshed
        assert request(work, "goto", identity="refresh-auth", url=base).get("success")
        refreshed = request(work, "inspect", identity="refresh-auth")
        assert "refreshed-auth" in refreshed.get("text", ""), refreshed
        result["token_refresh_and_immediate_reload"] = True
    finally:
        lease.release()
        for sid, turn in (("auth", "t1"), ("auth", "t2"), ("fallback", "t1"), ("explicit", "t1")):
            release(sid, turn)
        try:
            request(work, "close", identity="dashboard")
            deadline = time.monotonic() + 20
            while occupied(work / "runtime/resource-harness")["browser"]:
                assert time.monotonic() < deadline, "final rendered cleanup timed out"
                time.sleep(.1)
        finally:
            desktop.stop()
            server.shutdown()
            server.server_close()
            thread.join(5)
            result["remaining_browser_leases"] = len(occupied(work / "runtime/resource-harness")["browser"])
            result["passed"] = (sys.exc_info()[0] is None and result["remaining_browser_leases"] == 0)
            artifact.write_text(json.dumps(result, indent=2) + "\n")
    assert result["remaining_browser_leases"] == 0
    shutil.rmtree(work)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
