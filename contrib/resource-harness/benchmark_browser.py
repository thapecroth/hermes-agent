"""Controlled, isolated browser_exec trials on the installed Hermes stack.

No model calls or messaging adapters: the same Python helper code, local HTTP
fixture, fresh profile and instrumentation are used for both browser engines.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ENTRY = time.perf_counter()


def cgroup_stats():
    """Counters for this trial's complete transient unit, including daemons."""
    relative = next(row.split(':', 2)[2] for row in
                    Path('/proc/self/cgroup').read_text().splitlines() if row.startswith('0::'))
    root = Path('/sys/fs/cgroup') / relative.lstrip('/')
    return {"path": str(root),
            "cpu_usage_usec": int(dict(row.split() for row in
                (root / 'cpu.stat').read_text().splitlines())['usage_usec']),
            "memory_current_bytes": int((root / 'memory.current').read_text()),
            "memory_peak_bytes": int((root / 'memory.peak').read_text())}


def worker(args):
    cgroup_before = cgroup_stats()
    import psutil
    from pm.paths import store_root
    installed_tools = store_root()
    temp = tempfile.mkdtemp(prefix="hermes-browser-bench-")
    home = Path(temp)
    plugin = home / "plugins/resource-harness"
    shutil.copytree(Path(__file__).resolve().parent, plugin,
                    ignore=shutil.ignore_patterns("__pycache__", ".git"))
    os.environ.update(HERMES_HOME=temp, HERMES_RUNTIME_DIR=str(home / "runtime"),
                      LIGHTPANDA_DISABLE_TELEMETRY="true")
    for key in list(os.environ):
        if key.startswith(("BU_", "AGENT_BROWSER_")) or key in (
                "BROWSER_CDP_URL", "CAMOFOX_URL", "BH_AGENT_WORKSPACE"):
            os.environ.pop(key)
    runtime = home / "runtime"
    runtime.mkdir()
    for asset in installed_tools.iterdir():
        if asset.is_dir():
            (runtime / asset.name).symlink_to(asset, target_is_directory=True)
    if (installed_tools / "facts.json").is_file():
        shutil.copy2(installed_tools / "facts.json", runtime / "facts.json")
    import yaml
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["resource-harness"], "entries": {"resource-harness": {
            "settings": {"browsers": args.sessions, "locale_singleflight": True,
                         "queue_timeout_seconds": 90, "browser_timeout_seconds": 30}}}},
        "browser": {"backend": "browser-use", "engine": args.engine,
                    "cloud_provider": "local", "use_real_profile": False,
                    "allow_private_urls": True, "auto_local_for_private_urls": True},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
    }))
    from hermes_constants import set_hermes_home_override
    from agent.secret_scope import set_secret_scope
    set_hermes_home_override(home)
    set_secret_scope({}, profile_home=temp)
    import model_tools
    from hermes_cli.plugins import discover_plugins, invoke_hook
    model_tools.discover_builtin_tools()
    discover_plugins(force=True)
    from tools import browser_tool
    from browser_harness import _ipc
    from browser_harness.admin import daemon_alive
    # Read occupancy with the exact implementation used by the installed plugin.
    import importlib.util
    spec = importlib.util.spec_from_file_location("benchmark_pool", plugin / "pool.py")
    pools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pools)
    pool_root = runtime / "resource-harness"
    startup_s = time.perf_counter() - ENTRY
    run = "bench-" + uuid.uuid4().hex[:10]
    owners = [f"{run}-{n}:{run}-{n}-turn" for n in range(args.sessions)]
    names = ["rh-" + hashlib.sha256(owner.encode()).hexdigest()[:32] for owner in owners]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    manifest = args.out.with_suffix('.running.json')
    manifest.write_text(json.dumps({"isolated_workdir": temp, "owner_names": names,
        "worker_pid": os.getpid(), "cgroup": cgroup_before['path']}, indent=2) + '\n')
    tracked = {}
    peak = {"browser_leases": 0, "engine_instances": 0, "lightpanda_instances": 0}
    steady_memory = {}
    stop = threading.Event()
    release = threading.Event()
    ready = [threading.Event() for _ in owners]
    errors = []
    first_results = []
    lock = threading.Lock()
    hanging = threading.Event()
    finish_hang = threading.Event()

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/hang':
                hanging.set()
                finish_hang.wait(40)
                return
            if self.path == "/api":
                body, mime = b'{"status":"ready"}', "application/json"
            else:
                body, mime = (b'<html><head><title>Hermes engine fixture</title></head><body>'
                    b'<input id="field"><p id="status">loading</p>'
                    b'<script>fetch("/api").then(r=>r.json()).then(d=>'
                    b'document.getElementById("status").textContent=d.status)</script>'
                    b'</body></html>'), "text/html"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def remember(pid, target):
        try:
            process = psutil.Process(pid)
            for child in [process, *process.children(recursive=True)]:
                identity = (child.pid, child.create_time())
                tracked[identity] = child
                target[identity] = child
        except psutil.NoSuchProcess:
            pass

    def survivors():
        result = []
        for process in tracked.values():
            try:
                if process.pid != os.getpid() and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    result.append(process.pid)
            except psutil.NoSuchProcess:
                pass
        return result

    def sample_once(measure_memory=False):
        browsers = {}
        with browser_tool._cleanup_lock:
            infos = [dict(info) for info in browser_tool._active_sessions.values()]
        lp_count = 0
        for info in infos:
            session = info["session_name"]
            if info.get("features", {}).get("lightpanda"):
                from tools.browser_lightpanda import get_server
                lp = get_server(session)
                if lp and lp.is_alive():
                    lp_count += 1
                    remember(lp.proc.pid, browsers)
            else:
                path = Path(browser_tool._socket_safe_tmpdir()) / f"agent-browser-{session}" / f"{session}.pid"
                try:
                    remember(int(path.read_text().strip()), browsers)
                except FileNotFoundError:
                    pass
        for name in names:
            try:
                remember(int(_ipc.pid_path(name).read_text().strip()), browsers)
            except FileNotFoundError:
                pass
        total = dict(browsers)
        remember(os.getpid(), total)
        def pss(processes):
            result = 0
            for process in processes.values():
                try:
                    result += process.memory_full_info().pss
                except psutil.NoSuchProcess:
                    pass
            return result
        peak["browser_leases"] = max(peak["browser_leases"], len(pools.occupied(pool_root)["browser"]))
        peak["engine_instances"] = max(peak["engine_instances"], len(infos))
        peak["lightpanda_instances"] = max(peak["lightpanda_instances"], lp_count)
        # Read expensive smaps only after timed work finishes. Otherwise Chrome's
        # larger process tree pays more profiling overhead during the latency test.
        if measure_memory:
            steady_memory["browser_and_driver_pss_bytes"] = pss(browsers)
            steady_memory["whole_worker_pss_bytes"] = pss(total)

    def sampler():
        while not stop.wait(.2):
            try:
                sample_once()
            except Exception as exc:
                errors.append(repr(exc))
                return

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    wave_start = time.perf_counter()

    def dispatch(lane, code, timeout=30):
        value = model_tools.handle_function_call("browser_exec", {"code": code, "timeout_s": timeout},
                session_id=f"{run}-{lane}", task_id=f"{run}-{lane}", turn_id=f"{run}-{lane}-turn")
        return json.loads(value) if isinstance(value, str) else value

    def call(lane, code):
        value = dispatch(lane, code)
        if not value.get("success"):
            raise RuntimeError(str(value)[:1500])
        return value.get("output", "")

    def lane(index):
        set_hermes_home_override(home)
        set_secret_scope({}, profile_home=temp)
        token = f"owner{index}"
        try:
            start = time.perf_counter()
            out = call(index, f'new_tab({base!r}); wait_for_load(); '
                'print("TITLE", js("document.title")); '
                'print("CLEAN", js("localStorage.getItem(\'owner\') === null && document.cookie === \'\'")); '
                f'js("localStorage.setItem(\'owner\', \'{token}\'); document.cookie=\'owner={token};path=/\'")')
            first = time.perf_counter() - start
            assert "Hermes engine fixture" in out and "CLEAN True" in out, out
            with lock:
                first_results.append(time.perf_counter() - wave_start)
            start = time.perf_counter()
            out = call(index, f'print("SAME", js("localStorage.getItem(\'owner\') === \'{token}\' && document.cookie.includes(\'owner={token}\')")); '
                f'goto_url({(base + "/dynamic")!r}); wait_for_load(); '
                'import time\n'
                'for _ in range(100):\n'
                '    if js("document.getElementById(\'status\').textContent") == "ready": break\n'
                '    time.sleep(.02)\n'
                'print("DYNAMIC", js("document.getElementById(\'status\').textContent")); '
                'fill_input("#field", "typed-value"); print("FILLED", js("document.getElementById(\'field\').value"))')
            reused = time.perf_counter() - start
            assert "SAME True" in out and "DYNAMIC ready" in out and "FILLED typed-value" in out, out
            result = {"first_call_s": first, "reused_call_s": reused,
                      "completion_s": time.perf_counter() - wave_start, "semantics_passed": True}
            ready[index].set()
            assert release.wait(45), "qualification release deadline exceeded"
            return result
        finally:
            invoke_hook("on_session_end", session_id=f"{run}-{index}", task_id=f"{run}-{index}",
                        turn_id=f"{run}-{index}-turn")

    before = os.getloadavg()
    try:
        with ThreadPoolExecutor(max_workers=args.sessions) as executor:
            futures = [executor.submit(lane, n) for n in range(args.sessions)]
            try:
                deadline = time.monotonic() + 90
                while not all(event.is_set() for event in ready):
                    for future in futures:
                        if future.done():
                            future.result()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("browser workload deadline exceeded")
                    stop.wait(.05)
                stop.set()
                sampler_thread.join(5)
                sample_once(measure_memory=True)
            finally:
                release.set()
            lanes = [future.result(60) for future in futures]
        deadline = time.monotonic() + 30
        while any(pools.occupied(pool_root).values()):
            if time.monotonic() >= deadline:
                raise RuntimeError("Browser leases survived cleanup; retained " + temp)
            time.sleep(.05)
        for name in names:
            assert not daemon_alive(name), "browser-use daemon survived"
        from tools.browser_lightpanda import get_server
        for info in list(browser_tool._active_sessions.values()):
            assert not get_server(info["session_name"]), "Lightpanda server survived"
        assert not browser_tool._active_sessions, "engine tracking survived"
        stop.set()
        sampler_thread.join(5)
        assert not survivors(), f"browser/driver processes survived: {survivors()}"
        assert not errors, errors
        assert peak["browser_leases"] == args.sessions and peak["engine_instances"] == args.sessions, peak
        if args.engine == "lightpanda":
            assert peak["lightpanda_instances"] == args.sessions, peak
        workload_and_cleanup_s = time.perf_counter() - wave_start
        fault_checks = {}
        if args.failure_checks:
            from tools.interrupt import set_interrupt
            # A live session must not bypass cancellation merely because it has a lease.
            call(0, f'new_tab({base!r}); wait_for_load(); print(js("document.title"))')
            sample_once()
            try:
                set_interrupt(True)
                cancelled = dispatch(0, 'print("must not run")')
                assert (cancelled.get('error') or cancelled.get('success') is False) and 'capacity unavailable' in str(cancelled).lower(), cancelled
            finally:
                set_interrupt(False)
            fault_checks['cancelled_existing_owner_blocked'] = True
            start = time.perf_counter()
            with ThreadPoolExecutor(1) as executor:
                pending = executor.submit(dispatch, 0, f'goto_url({(base + "/hang")!r}); wait_for_load()', 10)
                assert hanging.wait(15), 'hanging fixture was never requested'
                timed_out = pending.result(30)
            assert timed_out.get('error') or timed_out.get('success') is False, timed_out
            assert not any(pools.occupied(pool_root).values()), 'timeout retained its lease'
            # Previously captured identity handles remain valid even when core metadata is gone.
            deadline = time.monotonic() + 8
            while survivors():
                if time.monotonic() >= deadline:
                    raise RuntimeError('timeout processes survived: ' + repr(
                        [(pid, psutil.Process(pid).name()) for pid in survivors()]))
                time.sleep(.05)
            fault_checks.update(timeout_cleanup_verified=True, timeout_cleanup_s=time.perf_counter() - start)
            call(0, f'new_tab({base!r}); wait_for_load(); print(js("document.title"))')
            sample_once()
            invoke_hook('on_session_end', session_id=f'{run}-0', task_id=f'{run}-0', turn_id=f'{run}-0-turn')
            deadline = time.monotonic() + 20
            while any(pools.occupied(pool_root).values()):
                if time.monotonic() >= deadline:
                    raise RuntimeError('replacement owner did not clean up')
                time.sleep(.05)
            fault_checks['replacement_session_passed'] = True
            assert not survivors(), f'replacement processes survived: {survivors()}'
        cgroup_after = cgroup_stats()
        receipt = {"passed": True, "engine": args.engine, "sessions": args.sessions,
                   "startup_s": startup_s, "first_useful_browser_result_s": min(first_results),
                   "all_browser_results_s": max(l["completion_s"] for l in lanes),
                   "workload_and_cleanup_s": workload_and_cleanup_s,
                   "script_wall_s": time.perf_counter() - ENTRY, "lanes": lanes, "peak": peak,
                   "steady_memory": steady_memory,
                   "failure_checks": fault_checks,
                   "trial_cgroup_cpu_s": (cgroup_after['cpu_usage_usec'] - cgroup_before['cpu_usage_usec']) / 1e6,
                   "trial_cgroup_peak_memory_bytes": cgroup_after['memory_peak_bytes'],
                   "host_load_before": before, "host_load_after": os.getloadavg(),
                   "locale_singleflight": bool(getattr(sys.modules.get("agent.i18n"), "_codon_locale_singleflight", False)),
                   "cleanup_verified": True,
                   "external_message_sends": 0, "model_calls": 0,
                   "scope": "real Hermes dispatch and browser; local dynamic HTTP fixture; no provider latency"}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(receipt, indent=2) + "\n")
        shutil.rmtree(home)
        manifest.unlink()
        print(json.dumps(receipt), flush=True)
    except BaseException:
        # Hooks are asynchronous. A failed lane must also run synchronous targeted
        # cleanup before returning; the enclosing unit is the last-resort boundary.
        from tools.browser_use_cli import _backend_cache_key, _sidecar_daemon_name
        from tools.browser_tool_lifecycle import cleanup_browser as cleanup_engine
        from browser_harness.admin import restart_daemon
        cleanup_errors = []
        for name in names:
            for daemon in (name, _sidecar_daemon_name(name)):
                try:
                    if daemon_alive(daemon):
                        restart_daemon(daemon, require_clean=True)
                except Exception as exc:
                    cleanup_errors.append(repr(exc))
            for sidecar in (False, True):
                try:
                    cleanup_engine(_backend_cache_key(None, name, sidecar))
                except Exception as exc:
                    cleanup_errors.append(repr(exc))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".incomplete.json").write_text(json.dumps({"isolated_workdir": temp,
            "peak": peak, "errors": errors, "cleanup_errors": cleanup_errors,
            "steady_memory": steady_memory,
            "owner_names": names, "tracked_process_ids": [list(k) for k in tracked]}, indent=2) + "\n")
        raise
    finally:
        release.set()
        stop.set()
        sampler_thread.join(5)
        server.shutdown()
        finish_hang.set()
        server.server_close()


def run_trial(args, engine, out):
    started = time.perf_counter()
    environment = {key: os.environ[key] for key in (
        'PATH', 'HERMES_DISABLE_LAZY_INSTALLS', 'LIGHTPANDA_DISABLE_TELEMETRY',
        'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS') if key in os.environ}
    command = [sys.executable, str(args.bootstrap), str(Path(__file__).resolve()),
        '--engine', engine, '--sessions', str(args.sessions), '--out', str(out)]
    if args.failure_checks:
        command.append('--failure-checks')
    out.parent.mkdir(parents=True, exist_ok=True)
    run_contained(command, 'hermes-browser-bench-' + uuid.uuid4().hex[:12],
                  out.with_suffix('.log'), environment)
    row = json.loads(out.read_text())
    row['process_wall_s'] = time.perf_counter() - started
    return row


def suite(args):
    if not args.bootstrap:
        raise ValueError("--suite needs the installed dependency --bootstrap")
    args.out.mkdir(parents=True, exist_ok=True)
    trials = []
    for pair in range(args.trials):
        order = ("auto", "lightpanda") if pair % 2 == 0 else ("lightpanda", "auto")
        for engine in order:
            out = args.out / f"pair-{pair + 1}-{engine}.json"
            row = run_trial(args, engine, out)
            row["pair"] = pair + 1
            trials.append(row)
    metrics = ("first_useful_browser_result_s", "all_browser_results_s", "workload_and_cleanup_s",
               "process_wall_s", "trial_cgroup_cpu_s")
    summary = {}
    for engine in ("auto", "lightpanda"):
        rows = [r for r in trials if r["engine"] == engine]
        summary[engine] = {key: {"median": statistics.median(r[key] for r in rows),
                                "min": min(r[key] for r in rows), "max": max(r[key] for r in rows)}
                           for key in metrics}
        summary[engine]["median_steady_browser_driver_pss_mib"] = statistics.median(
            r["steady_memory"]["browser_and_driver_pss_bytes"] / 2**20 for r in rows)
    receipt = {"passed": all(r["passed"] for r in trials), "paired_repetitions": args.trials,
               "sessions_per_trial": args.sessions, "summary": summary, "trials": trials,
               "scope": "controlled local-browser E2E; no estimate of user/model/network latency"}
    (args.out / "comparison.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("HERMES_BROWSER_COMPARISON_COMPLETE_20261115", flush=True)
    print(json.dumps(summary), flush=True)


def run_contained(command, unit, log, environment, runtime_seconds=150):
    """Use systemctl's private-manager transport on hosts without a user D-Bus.

    Runtime units still provide cgroup containment for detached browser children.
    Keep the unit file and failure log if stopping or independent cleanup fails.
    """
    def quoted(value):
        if any(char in str(value) for char in '\r\n\0'):
            raise ValueError('newline in unit argument')
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

    def ctl(*arguments):
        return subprocess.run(['systemctl', '--user', *arguments], check=True,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25).stdout

    units = Path(os.environ['XDG_RUNTIME_DIR']) / 'systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    path = units / (unit + '.service')
    path.write_text('[Unit]\nDescription=Isolated Hermes browser qualification\n[Service]\n'
        f'Type=exec\nRuntimeMaxSec={int(runtime_seconds)}\nMemoryMax=1536M\nCPUQuota=200%\nTasksMax=512\n'
        'KillMode=control-group\nTimeoutStopSec=10\n'
        + ''.join('Environment=' + quoted(key + '=' + value) + '\n' for key, value in environment.items())
        + 'ExecStart=' + ' '.join(quoted(value) for value in command) + '\n'
        + 'StandardOutput=append:' + str(log) + '\nStandardError=append:' + str(log) + '\n')
    group = None
    try:
        ctl('daemon-reload')
        ctl('start', '--no-block', unit)
        deadline = time.monotonic() + runtime_seconds + 30
        while True:
            state = dict(row.split('=', 1) for row in ctl('show', unit,
                '-p', 'ActiveState', '-p', 'ExecMainStatus', '-p', 'Result', '-p', 'ControlGroup').splitlines())
            if state.get('ControlGroup'):
                group = Path('/sys/fs/cgroup') / state['ControlGroup'].lstrip('/')
            if state['ActiveState'] in ('inactive', 'failed'):
                if state['Result'] != 'success' or state['ExecMainStatus'] != '0':
                    raise RuntimeError(f'{unit} failed: {state}; see {log}')
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f'{unit} exceeded controller deadline; see {log}')
            time.sleep(.25)
    finally:
        ctl('stop', unit)
        if group and group.exists():
            events = dict(row.split() for row in (group / 'cgroup.events').read_text().splitlines())
            if events.get('populated') != '0':
                raise RuntimeError(f'Trial cgroup still populated: {group}; unit retained at {path}')
        path.unlink()
        ctl('daemon-reload')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("auto", "lightpanda"), default="auto")
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--suite", action="store_true")
    parser.add_argument('--contained', action='store_true', help='run one trial in a private resource-limited unit')
    parser.add_argument("--bootstrap", type=Path)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument('--failure-checks', action='store_true', help='also exercise timeout, cancellation and replacement')
    args = parser.parse_args()
    if not 1 <= args.sessions <= 8 or args.trials < 1:
        parser.error("sessions must be 1..8 and trials must be positive")
    if (args.suite or args.contained) and not args.bootstrap:
        parser.error('--suite and --contained require --bootstrap')
    if args.suite and args.failure_checks:
        parser.error('keep failure checks out of comparative timing; use --contained')
    if args.suite:
        suite(args)
    elif args.contained:
        print(json.dumps(run_trial(args, args.engine, args.out)), flush=True)
        print('HERMES_BROWSER_SINGLE_TRIAL_COMPLETE', flush=True)
    else:
        worker(args)


if __name__ == "__main__":
    main()
