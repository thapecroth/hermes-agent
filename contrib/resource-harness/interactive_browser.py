"""A local, resource-bounded rendered browser on Hermes Bot Screen.

The worker owns one browser lease for its entire lifetime. Firefox uses the
pinned oss-invisible Playwright API, not CDP; Lightpanda remains independent.
IPC and profiles are private to the OS user. No remote browser service is used.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .browser_auth import (private_directory, read_private, require_revision, state_lock,
                           state_path, storage_script, write_private)
from .pool import Pool

MAX_MESSAGE = 1024 * 1024
TRANSPORT_GONE = (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError)


def firefox_processes():
    import psutil
    captured = {}
    for child in psutil.Process().children(recursive=True):
        try:
            if "firefox" in child.name().lower():
                for process in [child, *child.children(recursive=True)]:
                    captured[(process.pid, process.create_time())] = process
        except psutil.NoSuchProcess:
            pass
    return list(captured.values())


def http_url(value):
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use an http or https URL without embedded credentials")
    return value


def runtime_dir(home):
    return Path(home) / "runtime/resource-harness/interactive"


def evaluate_document(page, expression, timeout=5):
    """Retry document reads/idempotent cleanup during a page-triggered reload."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return page.evaluate(expression)
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def clear_worker_files(home, pid, born):
    root = runtime_dir(home)
    try:
        owner = json.loads((root / "owner.json").read_text())
    except FileNotFoundError:
        return
    if owner.get("pid") == pid and owner.get("born") == born:
        (root / "worker.sock").unlink(missing_ok=True)
        (root / "owner.json").unlink(missing_ok=True)


def rpc(home, payload, timeout=45):
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(timeout)
        stream.connect(str(runtime_dir(home) / "worker.sock"))
        wire = json.dumps(payload).encode() + b"\n"
        if len(wire) > MAX_MESSAGE:
            raise ValueError("Browser request too large")
        stream.sendall(wire)
        with stream.makefile("rb") as reader:
            data = reader.readline(MAX_MESSAGE + 1)
        if not data:
            raise ConnectionResetError("Browser worker closed the connection")
        if len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
            raise RuntimeError("Invalid browser worker response")
        return json.loads(data)


def settings(home):
    import yaml
    config = yaml.safe_load((Path(home) / "config.yaml").read_text()) or {}
    return config.get("plugins", {}).get("entries", {}).get("resource-harness", {}).get("settings", {})


def request(home, action, *, identity="dashboard", **arguments):
    """Start only on an explicit open; status and other commands never spawn."""
    home = Path(home)
    payload = {"action": action, "identity": identity, **arguments}
    root = runtime_dir(home)
    private_directory(root)
    try:
        return rpc(home, payload)
    except TRANSPORT_GONE:
        if action == "status":
            return {"running": False, "engine": "oss-invisible Firefox", "local_only": True}
        if action != "open":
            return {"error": "interactive_browser_not_running"}
    http_url(arguments.get("url", ""))
    # Serialize launch across dashboard and gateway processes.
    with (root / "launch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return rpc(home, payload)
        except TRANSPORT_GONE:
            pass
        # A closing worker can stop accepting RPC before Firefox has exited.
        # Wait for its supervisor, rather than overlapping profiles or deleting
        # a successor's socket when the old cleanup eventually completes.
        with (root / "lifetime.lock").open("a+") as lifetime:
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return {"error": "rendered_browser_cleanup_in_progress"}
                    time.sleep(.1)
        config = settings(home)
        site = config.get("interactive_site_packages", "")
        if not site or not (Path(site) / "invisible_playwright").is_dir():
            return {"error": "oss_invisible_runtime_missing"}
        if not Path(config.get("interactive_binary", "")).is_file():
            return {"error": "oss_invisible_engine_missing"}
        log_path = root / "worker.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        # Carry Hermes's activated dependency environment; isolate the optional
        # Firefox wrapper in the child so it cannot change gateway dependencies.
        plugin = Path(__file__).resolve().parent
        code = ("import sys,importlib.util; sys.path=" + repr([site, *sys.path]) + "; "
                "spec=importlib.util.spec_from_file_location('resource_harness_worker'," + repr(str(plugin / "__init__.py")) +
                ",submodule_search_locations=[" + repr(str(plugin)) + "]); "
                "module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module); "
                "from resource_harness_worker.interactive_browser import supervise; "
                "supervise(" + repr(str(home)) + "," + repr(identity) + ")")
        with os.fdopen(fd, "ab") as log:
            process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True,
                env={**os.environ, "HERMES_HOME": str(home)})
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return {"error": "rendered_browser_start_failed", "exit_code": process.returncode}
            try:
                return rpc(home, payload)
            except TRANSPORT_GONE:
                time.sleep(.1)
        # Leave the independent supervisor alive until its descendants are
        # verified stopped. Killing its group would discard the admission lock
        # before a stuck engine has necessarily exited.
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            return {"error": "rendered_browser_start_timeout", "cleanup_pending": True,
                    "supervisor_pid": process.pid}
        return {"error": "rendered_browser_start_timeout", "cleanup_pending": False}


class RenderedBrowser:
    def __init__(self, home, identity, shutdown=None):
        from invisible_playwright import InvisiblePlaywright
        from tools.bot_desktop import runtime
        self.home, self.identity = Path(home), identity
        self.shutdown = shutdown
        self.config = settings(self.home)
        self.launcher = None
        try:
            screen = runtime.start()
            # Only the worker receives the display; research stays headless.
            os.environ.update(runtime.desktop_env())
            self.profile = self.home / "browser-auth/rendered" / hashlib.sha256(identity.encode()).hexdigest()
            private_directory(self.profile)
            dimensions = screen.geometry.split("x")
            self.launcher = InvisiblePlaywright(headless=False, seed=42,
                pin={"screen.width": int(dimensions[0]), "screen.height": int(dimensions[1])},
                locale="en-US", timezone="America/Los_Angeles", humanize=False,
                binary_path=self.config["interactive_binary"],
                profile_dir=str(self.profile))
            self.context = self.launcher.__enter__()
            self.context.set_default_timeout(15000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.seed = self.home / "browser-auth/chromium-seed.json"
            self.state = state_path(self.home, identity)
            self.seeded = self.profile / ".auth-imported"
            self.consumed = set()
            self.source = None
            with state_lock(self.state):
                source = self.state if self.state.exists() else self.seed
                current = read_private(source) if source.exists() else None
                self.revision = current.get("saved_at") if current else None
                imported = json.loads(self.seeded.read_text()) if self.seeded.exists() else {}
                needs_import = not self.seeded.exists() or imported.get("revision") != self.revision
                if current and not self.state.exists():
                    write_private(self.state, current)
                self.source = current if needs_import else None
                if needs_import:
                    old_pages = list(self.context.pages)
                    self.page = self.context.new_page()
                    for old_page in old_pages:
                        old_page.close()
                    self.context.clear_cookies()
                if self.source:
                    cookies = []
                    for row in self.source["cookies"]:
                        if "partitionKey" in row or row.get("partitionKeyOpaque"):
                            continue
                        if row.get("expires", -1) > 0 and row["expires"] < time.time():
                            continue
                        c = {k: row[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite") if k in row}
                        c["expires"] = row.get("expires", -1)
                        cookies.append(c)
                    if cookies:
                        self.context.add_cookies(cookies)
                    write_private(self.profile / ".pending-storage.json", self.source)
                write_private(self.seeded, {"revision": self.revision})
            pending = self.profile / ".pending-storage.json"
            if pending.exists():
                self.source = read_private(pending)
            self.last_used = time.monotonic()
        except BaseException:
            if self.launcher:
                self.launcher.__exit__(*sys.exc_info())
            raise

    def save(self):
        with state_lock(self.state):
            previous = read_private(self.state) if self.state.exists() else None
            require_revision(previous, self.revision)
            origins = {r["origin"]: r for r in (previous or {}).get("origins", [])}
            visited = set()
            from .browser_auth import STORAGE_EXPRESSION
            for page in self.context.pages:
                if not page.is_closed():
                    row = evaluate_document(page, STORAGE_EXPRESSION)
                    if row:
                        origins[row["origin"]] = row
                        visited.add(row["origin"])
            state = {"version": 1, "saved_at": time.time(),
                "cookies": self.context.cookies(), "origins": list(origins.values())}
            write_private(self.state, state)
            self.revision = state["saved_at"]
            write_private(self.seeded, {"revision": self.revision})
            pending = self.profile / ".pending-storage.json"
            if pending.exists():
                source = read_private(pending)
                source["origins"] = [r for r in source["origins"] if r["origin"] not in visited]
                write_private(pending, source)
        return {"saved": True}

    def navigate(self, url):
        http_url(url)
        self.save()
        pending = self.profile / ".pending-storage.json"
        source = read_private(pending) if pending.exists() else {"origins": []}
        rows = source["origins"]
        marker = "__hermes_auth_import_" + os.urandom(12).hex()
        # A transferred snapshot supersedes stale storage in a previous Firefox
        # profile, including empty rows recording a logout. A navigation may
        # redirect to another pending origin; seed the actual top-level origin
        # and let save() consume only origins whose current state we observed.
        script = ("if (window === window.top && " + json.dumps([r["origin"] for r in rows]) +
                  ".includes(location.origin) && sessionStorage.getItem(" + json.dumps(marker) +
                  ") !== '1') { localStorage.clear(); sessionStorage.clear();\n" +
                  storage_script(rows) + "\nsessionStorage.setItem(" + json.dumps(marker) + ", '1');\n}")
        disposable = self.page.add_init_script(script=script) if rows else None
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        finally:
            if disposable:
                disposable.dispose()
        if rows:
            evaluate_document(self.page, "sessionStorage.removeItem(" + json.dumps(marker) + ")")
        self.save()
        return self.inspect()

    def inspect(self):
        return evaluate_document(self.page, """() => ({url:location.href,title:document.title,
          text:document.body.innerText.slice(0,7000),
          controls:Array.from(document.querySelectorAll('input,select,textarea,button,a[href]')).slice(0,80)
           .map(e=>({tag:e.tagName.toLowerCase(),id:e.id,name:e.name||'',type:e.type||'',
             label:e.getAttribute('aria-label')||e.innerText?.slice(0,100)||e.placeholder||''}))})""")

    def act(self, payload):
        from tools.bot_desktop import lease, runtime
        action = payload.get("action")
        self.last_used = time.monotonic()
        runtime.touch_activity()
        if action == "status":
            return {"running": True, "engine": "oss-invisible Firefox", "local_only": True,
                    "lease": lease.public_view(lease.get())}
        # A dashboard can inspect status, but browser actions during takeover
        # are input through noVNC only. Captures and automation are fenced.
        before = lease.assert_agent_may_act()
        if payload.get("identity") not in {self.identity, "dashboard"}:
            return {"error": "interactive_browser_busy_with_another_job"}
        available = [page for page in self.context.pages if not page.is_closed()]
        if available:
            self.page = next((page for page in available if evaluate_document(page, "document.hasFocus()")), available[-1])
        elif action not in {"close", "save"}:
            self.page = self.context.new_page()
        if action == "open" or action == "goto":
            result = self.navigate(payload["url"])
        elif action == "inspect":
            result = self.inspect()
        elif action == "fill":
            self.page.locator(payload["selector"]).fill(payload["value"])
            result = {"filled": True}
        elif action == "click":
            self.page.locator(payload["selector"]).click()
            result = self.inspect()
        elif action == "press":
            self.page.keyboard.press(payload["key"])
            result = self.inspect()
        elif action == "save":
            result = self.save()
        elif action == "close":
            if self.shutdown:
                self.shutdown.set()
            self.save()
            return {"closing": True}
        else:
            return {"error": "unknown_interactive_action"}
        if lease.get().epoch != before.epoch:
            return {"error": "human_has_control"}
        self.save()
        return {"success": True, **result}

    def close(self):
        # Save through browser internals only while the human has handed back.
        from tools.bot_desktop import lease
        from .browser_cleanup import finish_cleanup
        # Capture only Firefox descendants, never the Xfce display processes.
        processes = firefox_processes()
        try:
            if lease.get().holder != lease.HUMAN:
                self.save()
        finally:
            self.launcher.__exit__(None, None, None)
            finish_cleanup(processes)


def supervise(home, identity):
    """Retain admission if the execution worker crashes, until Firefox is dead."""
    import ctypes
    import multiprocessing
    import psutil
    from .browser_cleanup import finish_cleanup
    # Orphaned grandchildren return here even if the worker dies between samples.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError("Could not enable rendered browser process supervision")
    lifetime = (runtime_dir(home) / "lifetime.lock").open("a+")
    fcntl.flock(lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
    admission = Pool(Path(home) / "runtime/resource-harness", "browser", 2).acquire("interactive:" + identity, 0)
    context = multiprocessing.get_context("fork")
    shutdown = context.Event()
    worker = context.Process(target=serve, args=(home, identity, shutdown))
    worker_born = None
    def stop_worker(*_):
        shutdown.set()
        if worker.is_alive():
            worker.terminate()
    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    captured = {}
    def remember():
        for child in firefox_processes():
            captured[(child.pid, child.create_time())] = child
    try:
        worker.start()
        worker_born = psutil.Process(worker.pid).create_time()
        closing_since = None
        while worker.is_alive():
            remember()
            if shutdown.is_set():
                closing_since = closing_since or time.monotonic()
                elapsed = time.monotonic() - closing_since
                if elapsed > 12:
                    worker.kill()
                elif elapsed > 8:
                    worker.terminate()
            worker.join(.1)
        remember()
    finally:
        if worker.pid is not None and worker.is_alive():
            shutdown.set()
            worker.terminate()
            worker.join(8)
            if worker.is_alive():
                worker.kill()
                worker.join()
        # A cleanup failure keeps this process and its lease quarantined; never
        # return live Firefox capacity to another owner.
        while True:
            try:
                remember()
                finish_cleanup(list(captured.values()))
                if worker_born is None and worker.pid is not None:
                    # Startup fingerprinting can fail after fork. Only this
                    # supervisor holds lifetime.lock, and the worker has been
                    # joined; no successor can publish ownership in this gap.
                    path = runtime_dir(home) / "owner.json"
                    if path.exists():
                        metadata = json.loads(path.read_text())
                        if metadata.get("pid") == worker.pid:
                            worker_born = metadata.get("born")
                clear_worker_files(home, worker.pid, worker_born)
                break
            except Exception:
                time.sleep(1)
        admission.close()
        lifetime.close()
    raise SystemExit(worker.exitcode or 0)


def serve(home, identity, shutdown):
    # Fork inherits the supervisor's handlers, whose Process object belongs to
    # the parent. Startup must remain terminable before our own handler is ready.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    from tools.bot_desktop import lease
    import faulthandler
    import ctypes
    ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
    if os.environ.get("HERMES_BROWSER_DIAGNOSTICS") == "1":
        faulthandler.dump_traceback_later(20)
    root = runtime_dir(home)
    private_directory(root)
    browser = RenderedBrowser(home, identity, shutdown)
    faulthandler.cancel_dump_traceback_later()
    sock = root / "worker.sock"
    import psutil
    born = psutil.Process().create_time()
    write_private(root / "owner.json", {"identity": identity, "pid": os.getpid(), "born": born})
    sock.unlink(missing_ok=True)
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            nonlocal stopped
            self.connection.settimeout(10)
            wire = self.rfile.readline(MAX_MESSAGE + 1)
            try:
                if len(wire) > MAX_MESSAGE or not wire.endswith(b"\n"):
                    raise ValueError("request too large")
                payload = json.loads(wire)
                response = browser.act(payload)
                if response.get("closing"):
                    stopped = True
            except lease.HumanHasControl:
                response = {"error": "human_has_control", "message": "Hand back control in Bot Screen before the agent continues."}
            except Exception as exc:
                # Browser exceptions can include credentials/selectors/URLs.
                response = {"error": "interactive_action_failed", "type": type(exc).__name__}
            self.wfile.write(json.dumps(response).encode() + b"\n")

    try:
        with socketserver.UnixStreamServer(str(sock), Handler) as server:
            sock.chmod(0o600)
            server.timeout = 1
            while not stopped:
                server.handle_request()
                idle = time.monotonic() - browser.last_used
                if idle > 1800 and lease.get().holder != lease.HUMAN:
                    stopped = True
    finally:
        # The supervisor removes ownership only after verified process cleanup.
        browser.close()


def register_interactive(ctx):
    from hermes_constants import get_hermes_home
    def handle(args, **kw):
        identity = args.get("_harness_identity") or kw.get("session_id") or kw.get("task_id") or "agent"
        try:
            return json.dumps(request(get_hermes_home(), args.get("action", "status"), identity=identity,
                **{k: v for k, v in args.items() if k not in {"action", "_harness_identity"}}))
        except Exception as exc:
            return json.dumps({"error": "interactive_browser_unavailable", "type": type(exc).__name__})
    ctx.register_tool(name="interactive_browser", toolset="browser", handler=handle,
        schema={"name": "interactive_browser", "description":
            "Local rendered Firefox on Bot Screen. Use for sites requiring full rendering or human login/CAPTCHA. "
            "Open with a URL, inspect controls, fill/click/press, save or close. A human takeover fences all agent actions. "
            "Moving from a headless engine transfers authentication and URL; live DOM/form state must be recreated.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["status", "open", "goto", "inspect", "fill", "click", "press", "save", "close"]},
                "url": {"type": "string"}, "selector": {"type": "string"},
                "value": {"type": "string"}, "key": {"type": "string"}}, "required": ["action"]}})
