"""Real Hermes discovery/dispatch + HTTP + Chromium, with isolated user state.

Run under the installed Hermes Python environment, with its checkout on sys.path.
No provider credentials or Telegram messages are needed for this deterministic test.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import faulthandler
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    source = Path(__file__).resolve().parent
    run = "rh-e2e-" + uuid.uuid4().hex[:12]
    artifact = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / f"{run}.json"
    faulthandler.dump_traceback_later(180, repeat=True)
    from pm.paths import store_root
    installed_tools = store_root()
    with tempfile.TemporaryDirectory(prefix=run) as temp:
        home = Path(temp)
        plugin = home / "plugins" / "resource-harness"
        shutil.copytree(source, plugin, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_RUNTIME_DIR"] = str(home / "runtime")
        # Reuse verified installed binaries; give test-only metadata/locks their own home.
        runtime = home / "runtime"
        runtime.mkdir()
        for asset in installed_tools.iterdir():
            if asset.is_dir():
                runtime.joinpath(asset.name).symlink_to(asset, target_is_directory=True)
        if installed_tools.joinpath("facts.json").exists():
            shutil.copy2(installed_tools / "facts.json", runtime / "facts.json")
        import yaml
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": ["resource-harness"], "entries": {
                "resource-harness": {"settings": {"queue_timeout_seconds": 240}}}},
            "browser": {"backend": "browser-use", "engine": os.environ.get("HERMES_E2E_BROWSER_ENGINE", "auto"), "cloud_provider": "local",
                        "allow_private_urls": True, "auto_local_for_private_urls": True},
            "terminal": {"backend": "local", "cwd": str(home)},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
        }), encoding="utf-8")

        from hermes_cli.plugins import discover_plugins, has_middleware, invoke_hook
        from hermes_cli.middleware import run_llm_execution_middleware
        from hermes_constants import set_hermes_home_override
        from agent.secret_scope import set_secret_scope
        set_hermes_home_override(home)
        set_secret_scope({}, profile_home=str(home))
        import model_tools
        model_tools.discover_builtin_tools()
        discover_plugins(force=True)
        assert has_middleware("tool_execution") and has_middleware("llm_execution"), "plugin did not load"
        from tools.interrupt import set_interrupt
        terminal_called = []
        set_interrupt(True)
        try:
            try:
                run_llm_execution_middleware({}, lambda _: terminal_called.append(True), session_id=run)
            except TimeoutError:
                pass
            else:
                raise AssertionError("cancelled API admission failed open")
            blocked = model_tools.handle_function_call("browser_exec", {"code": "print('bypass')"},
                                                       session_id=run, task_id=run)
            assert "capacity unavailable" in str(blocked).lower(), blocked
            assert not terminal_called, "cancelled middleware executed its downstream call"
        finally:
            set_interrupt(False)
        # Load this plugin's own diagnostics without installing a second plugin instance.
        import importlib.util
        spec = importlib.util.spec_from_file_location("rh_pool_probe", source / "pool.py")
        pools = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pools)
        pool_root = home / "runtime" / "resource-harness"
        release_api = threading.Event()
        api_started = threading.Barrier(11)
        browser_finished = [threading.Event() for _ in range(4)]
        release_browsers = [threading.Event() for _ in range(4)]
        stop_sampler = threading.Event()
        peak = {"api": 0, "browser": 0, "managed_browsers": 0}
        lock = threading.Lock()
        failures = []

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/search"):
                    api_started.wait(timeout=30)
                    if not release_api.wait(300):
                        self.send_error(504)
                        return
                    body = json.dumps({"result": "api-search-fixture", "id": self.path}).encode()
                else:
                    body = b"<html><head><title>Harness browser fixture</title></head><body><h1>Browser proof</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def api_worker(index):
            set_hermes_home_override(home)
            set_secret_scope({}, profile_home=str(home))
            def request(_):
                with urllib.request.urlopen(f"{base}/search?id={index}", timeout=310) as response:
                    return json.load(response)
            return run_llm_execution_middleware({}, request, session_id=f"{run}-api-{index}")

        def browser_worker(index):
            set_hermes_home_override(home)
            set_secret_scope({}, profile_home=str(home))
            sid = f"{run}-browser-{index}"
            try:
                result = model_tools.handle_function_call("browser_exec", {
                    "code": f'new_tab({base!r}); wait_for_load(); print("BROWSER_PROOF", js("document.title"))',
                    "session": f"unbounded-name-{index}", "timeout_s": 90},
                    session_id=sid, task_id=sid, turn_id=f"test-turn-{index}")
                decoded = json.loads(result) if isinstance(result, str) else result
                assert decoded.get("success"), str(decoded)[:800]
                assert "Harness browser fixture" in decoded.get("output", ""), str(decoded)[:800]
                browser_finished[index].set()
                assert release_browsers[index].wait(300), "browser release signal missing"
                return decoded["session"]
            finally:
                invoke_hook("on_session_end", session_id=sid, task_id=sid, turn_id=f"test-turn-{index}")

        def sample():
            from tools import browser_tool
            while not stop_sampler.wait(0.05):
                try:
                    state = pools.occupied(pool_root)
                    with browser_tool._cleanup_lock:
                        count = len(browser_tool._active_sessions)
                    with lock:
                        peak["api"] = max(peak["api"], len(state["api"]))
                        peak["browser"] = max(peak["browser"], len(state["browser"]))
                        peak["managed_browsers"] = max(peak["managed_browsers"], count)
                except Exception as exc:
                    failures.append(str(exc))
                    return

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        print("E2E: plugin loaded through real discovery", flush=True)
        browser_names = []
        try:
            with ThreadPoolExecutor(max_workers=14) as executor:
                api_futures = [executor.submit(api_worker, n) for n in range(10)]
                api_started.wait(timeout=40)
                assert len(pools.occupied(pool_root)["api"]) == 10
                print("E2E: ten HTTP requests overlap through LLM middleware", flush=True)
                browser_futures = [executor.submit(browser_worker, n) for n in range(4)]
                deadline = time.monotonic() + 150
                while sum(event.is_set() for event in browser_finished) < 2:
                    for future in browser_futures:
                        if future.done():
                            future.result()
                    assert time.monotonic() < deadline, "two browser sessions did not start"
                    stop_sampler.wait(0.1)
                assert len(pools.occupied(pool_root)["browser"]) == 2
                assert sum(event.is_set() for event in browser_finished) == 2
                release_api.set()
                for future in api_futures:
                    assert future.result(timeout=10)["result"] == "api-search-fixture"
                assert sum(event.is_set() for event in browser_finished) == 2
                print("E2E: API work completed while two additional browsers remained queued", flush=True)
                for event in release_browsers:
                    event.set()
                browser_names = [future.result(timeout=180) for future in browser_futures]
            deadline = time.monotonic() + 90
            while any(pools.occupied(pool_root).values()):
                assert time.monotonic() < deadline, "resource leases survived session cleanup"
                stop_sampler.wait(0.1)
            from browser_harness.admin import daemon_alive
            from tools import browser_tool
            from tools.browser_use_cli import _sidecar_daemon_name
            assert all(not daemon_alive(name) and not daemon_alive(_sidecar_daemon_name(name))
                       for name in browser_names), "a browser harness daemon survived"
            with browser_tool._cleanup_lock:
                assert not browser_tool._active_sessions, "a managed browser survived"
            # Reproduce two browser-owning coordinators delegating browser children.
            # Middleware must close each parent's browser before dispatch, retaining
            # its agent lease, so children can make progress within the same two slots.
            from agent.delegation_context import delegated_child_context
            from hermes_cli.middleware import run_tool_execution_middleware
            coordinators = [f"{run}-coordinator-{n}" for n in range(2)]
            def open_browser(sid):
                value = model_tools.handle_function_call("browser_exec", {
                    "code": f'new_tab({base!r}); wait_for_load(); print(js("document.title"))', "timeout_s": 90},
                    session_id=sid, task_id=sid, turn_id="nested-turn")
                value = json.loads(value) if isinstance(value, str) else value
                assert value.get("success"), value
                browser_names.append(value["session"])
            for sid in coordinators:
                with delegated_child_context():
                    run_llm_execution_middleware({}, lambda _: "coordinator", session_id=sid, task_id=sid, turn_id="nested-turn")
                open_browser(sid)
            assert len(pools.occupied(pool_root)["browser"]) == 2
            for sid in coordinators:
                def child(_args, parent=sid):
                    state = pools.occupied(pool_root)
                    assert any(item["owner"] == parent + ":nested-turn" for item in state["agents"]), state
                    assert not any(item["owner"] == parent + ":nested-turn" for item in state["browser"]), state
                    child_sid = parent + "-child"
                    try:
                        open_browser(child_sid)
                    finally:
                        invoke_hook("on_session_end", session_id=child_sid, task_id=child_sid, turn_id="nested-turn")
                    return "child completed"
                try:
                    assert run_tool_execution_middleware("delegate_task", {"goal": "nested fixture"}, child,
                        session_id=sid, task_id=sid, turn_id="nested-turn") == "child completed"
                finally:
                    invoke_hook("on_session_end", session_id=sid, task_id=sid, turn_id="nested-turn")
            deadline = time.monotonic() + 90
            while any(pools.occupied(pool_root).values()):
                assert time.monotonic() < deadline, "nested browser leases survived cleanup"
                stop_sampler.wait(.1)
            assert all(not daemon_alive(name) and not daemon_alive(_sidecar_daemon_name(name)) for name in browser_names)
            assert not failures, failures
            assert peak["api"] == 10 and peak["browser"] == 2 and peak["managed_browsers"] <= 2, peak
            receipt = {"run": run, "passed": True, "peak": peak,
                       "api_requests": 10, "real_browser_jobs": 4,
                       "nested_coordinators": 2, "nested_browser_children": 2,
                       "remaining_leases": pools.occupied(pool_root), "remaining_browser_daemons": 0,
                       "uses_real_model_provider": False}
            artifact.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(receipt), flush=True)
            faulthandler.cancel_dump_traceback_later()
        finally:
            release_api.set()
            for event in release_browsers:
                event.set()
            # Cleanup workers use assets under the isolated home: keep it until they settle,
            # including when an assertion fails halfway through the qualification.
            cleanup_deadline = time.monotonic() + 90
            while any(pools.occupied(pool_root).values()) and time.monotonic() < cleanup_deadline:
                stop_sampler.wait(0.1)
            stop_sampler.set()
            sampler.join(5)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
