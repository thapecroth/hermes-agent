"""Capture process identities before core cleanup can remove its tracking records."""
import time
import threading
import os
from pathlib import Path

_CAPTURED = {}
_LOCK = threading.Lock()


def shared_endpoint_configured():
    """Shared endpoints and persistent profiles bypass per-owner isolation."""
    from tools.browser_tool_cdp import _get_cdp_override_raw
    from tools.browser_use_cli import _real_profile_consented
    return bool(_get_cdp_override_raw() or _real_profile_consented() or
                any(os.environ.get(key, '').strip() for key in
                    ('BU_CDP_WS', 'BU_CDP_URL', 'BROWSER_CDP_URL', 'AGENT_BROWSER_PROFILE')))


def forget_verified(name):
    with _LOCK:
        _CAPTURED.pop(name, None)


def capture_processes(name):
    import psutil
    from tools import browser_tool
    from tools.browser_use_cli import _backend_cache_key
    from tools.browser_lightpanda import get_server
    from tools.browser_tool_lifecycle import _verify_reapable_browser_daemon
    from browser_harness import _ipc
    from tools.browser_use_cli import _sidecar_daemon_name

    keys = [_backend_cache_key(None, name), _backend_cache_key(None, name, True)]
    with browser_tool._cleanup_lock:
        infos = [dict(browser_tool._active_sessions[key]) for key in keys
                 if key in browser_tool._active_sessions]
    # Core teardown removes its metadata before we can verify child death. Retain
    # identities across retries so a second attempt cannot accidentally verify [].
    with _LOCK:
        processes = _CAPTURED.setdefault(name, {})
    def remember(pid):
        try:
            process = psutil.Process(pid)
            process.create_time()
            for child in [process, *process.children(recursive=True)]:
                with _LOCK:
                    processes[(child.pid, child.create_time())] = child
        except psutil.NoSuchProcess:
            pass

    # Driver shutdown may remove its socket before a blocked execution child dies.
    # Use the authenticated daemon handshake, never an unverified PID file.
    for daemon in (name, _sidecar_daemon_name(name)):
        pid = _ipc.identify(daemon, timeout=1)
        if pid:
            remember(pid)
    for info in infos:
        session = info["session_name"]
        if info.get("features", {}).get("lightpanda"):
            server = get_server(session)
            pid = server.proc.pid if server and server.is_alive() else None
        else:
            socket_dir = Path(browser_tool._socket_safe_tmpdir()) / f"agent-browser-{session}"
            path = socket_dir / f"{session}.pid"
            try:
                pid = int(path.read_text().strip())
            except FileNotFoundError:
                pid = None
            if pid and not _verify_reapable_browser_daemon(pid, str(socket_dir), session):
                pid = None
        if pid:
            remember(pid)
    with _LOCK:
        return list(processes.values())


def verify_stopped(processes, timeout=5):
    import psutil
    deadline = time.monotonic() + timeout
    while True:
        remaining = []
        for process in processes:
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    remaining.append(process.pid)
            except psutil.NoSuchProcess:
                pass
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Browser processes survived cleanup: {remaining}")
        time.sleep(.05)


def finish_cleanup(processes, grace=1):
    """Reap only fingerprinted descendants left after the normal backend shutdown."""
    import psutil
    try:
        verify_stopped(processes, timeout=grace)
        return
    except RuntimeError:
        pass
    for method in ('terminate', 'kill'):
        for process in processes:
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    getattr(process, method)()  # psutil checks the captured start-time before signals.
            except psutil.NoSuchProcess:
                pass
        try:
            verify_stopped(processes, timeout=2)
            return
        except RuntimeError:
            if method == 'kill':
                raise
