"""Bound API fan-out and whole browser sessions using Hermes middleware."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import weakref
from pathlib import Path

from .pool import Pool

log = logging.getLogger(__name__)


class Harness:
    def __init__(self, root: Path, *, agents=10, api=10, browsers=2, wait=600, browser_timeout=120):
        self.root = root
        self.pools = {name: Pool(root, name, size) for name, size in
                      (("agents", agents), ("api", api), ("browser", browsers))}
        self.wait = wait
        self.browser_timeout = browser_timeout
        self.mutex = threading.RLock()
        self.owners = {}
        self.owner_locks = weakref.WeakValueDictionary()

    def owner_lock(self, owner):
        with self.mutex:
            return self.owner_locks.setdefault(owner, threading.RLock())

    def retain(self, kind, owner, cancel=None):
        with self.owner_lock(owner):
            with self.mutex:
                existing = self.owners.get(owner, {}).get(kind)
            if existing:
                return existing
            lease = self.pools[kind].acquire(owner, self.wait, cancel)
            with self.mutex:
                self.owners.setdefault(owner, {})[kind] = lease
            return lease

    def release(self, owner, cleanup):
        with self.owner_lock(owner):
            with self.mutex:
                leases = dict(self.owners.get(owner, {}))
            if "browser" in leases:
                # A failed cleanup quarantines the slot; the independent watchdog recovers it.
                try:
                    cleanup(browser_name(owner))
                except Exception as exc:
                    self.root.joinpath("cleanup-failed").write_text(type(exc).__name__, encoding="utf-8")
                    log.exception("Browser cleanup failed; retaining its resource slot")
                    return False
            for lease in leases.values():
                lease.close()
            with self.mutex:
                self.owners.pop(owner, None)
            return True

    def release_browser(self, owner, cleanup):
        """A coordinator must not hold browser capacity while waiting for its children."""
        with self.owner_lock(owner):
            with self.mutex:
                lease = self.owners.get(owner, {}).get("browser")
            if lease is None:
                return True
            try:
                cleanup(browser_name(owner))
            except Exception as exc:
                self.root.joinpath("cleanup-failed").write_text(type(exc).__name__, encoding="utf-8")
                log.exception("Browser cleanup failed before delegation; retaining its slot")
                return False
            lease.close()
            with self.mutex:
                self.owners[owner].pop("browser", None)
                if not self.owners[owner]:
                    self.owners.pop(owner)
            return True


def browser_name(owner):
    return "rh-" + hashlib.sha256(owner.encode()).hexdigest()[:32]


def owner_key(session_id, task_id, turn_id):
    identity = session_id or task_id
    return f"{identity}:{turn_id}" if identity and turn_id else identity


def cleanup_browser(name, task_id="default"):
    """Hermes 0.22.2 adapter: close both the harness daemon and its managed engine."""
    from browser_harness.admin import daemon_alive, restart_daemon
    from tools.browser_use_cli import _backend_cache_key, _sidecar_daemon_name
    from tools.browser_tool_lifecycle import cleanup_browser as cleanup_engine
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    SUPERVISOR_REGISTRY.stop(task_id or "default")
    for daemon in (name, _sidecar_daemon_name(name)):
        if daemon_alive(daemon):
            restart_daemon(daemon, require_clean=True)
            if daemon_alive(daemon):
                raise RuntimeError("browser harness daemon survived cleanup")
    cleanup_engine(_backend_cache_key(None, name))
    cleanup_engine(_backend_cache_key(None, name, True))


def register(ctx):
    from hermes_constants import get_hermes_home

    harnesses = {}
    mutex = threading.Lock()

    def current():
        root = get_hermes_home() / "runtime" / "resource-harness"
        with mutex:
            if root not in harnesses:
                harnesses[root] = Harness(root,
                    agents=int(ctx.get_config("agents", 10)),
                    api=int(ctx.get_config("api", 10)),
                    browsers=int(ctx.get_config("browsers", 2)),
                    wait=float(ctx.get_config("queue_timeout_seconds", 600)),
                    browser_timeout=int(ctx.get_config("browser_timeout_seconds", 120)))
            return harnesses[root]

    def llm_execution(*, request, next_call, session_id="", task_id="", turn_id="", **_):
        from agent.delegation_context import is_delegated_child_context
        from hermes_cli.middleware import _DownstreamExecutionError
        from tools.interrupt import is_interrupted
        owner = owner_key(session_id, task_id, turn_id)
        try:
            h = current()
            if owner and is_delegated_child_context():
                h.retain("agents", owner, is_interrupted)
            lease = h.pools["api"].acquire(owner, h.wait, is_interrupted)
        except Exception as exc:
            # Hermes deliberately fails open on a plugin error before next_call. Admission errors
            # must use its downstream error contract, otherwise a full pool would execute anyway.
            raise _DownstreamExecutionError(exc) from exc
        with lease:
            return next_call(request)

    def tool_execution(*, tool_name, args, next_call, session_id="", task_id="", turn_id="", **_):
        search = tool_name in ("web_search", "web_extract")
        spawning = tool_name == "delegate_task" and (args.get("action") or "spawn") == "spawn"
        if not search and not spawning and not tool_name.startswith("browser_"):
            return next_call(args)
        from tools.registry import tool_error
        from tools.interrupt import is_interrupted
        owner = owner_key(session_id, task_id, turn_id)
        if spawning:
            if owner and not current().release_browser(owner, lambda name: cleanup_browser(name, task_id)):
                return tool_error("Browser cleanup failed before delegation; recovery is required")
            return next_call(args)
        if search:
            try:
                h = current()
                lease = h.pools["api"].acquire(owner, h.wait, is_interrupted)
            except Exception as exc:
                return tool_error(f"API capacity unavailable: {exc}")
            with lease:
                return next_call(args)
        if not owner:
            return tool_error("Browser admission requires a session or task identity")
        try:
            h = current()
            h.retain("browser", owner, is_interrupted)
        except Exception as exc:
            return tool_error(f"Browser capacity unavailable: {exc}")
        with h.owner_lock(owner):
            if tool_name != "browser_exec":
                return tool_error("This harness supports browser_exec; use the configured browser-use backend")
            routed = dict(args)
            routed["session"] = browser_name(owner)
            requested = routed.get("timeout_s", h.browser_timeout)
            try:
                routed["timeout_s"] = min(h.browser_timeout, max(10, int(requested)))
            except (TypeError, ValueError):
                routed["timeout_s"] = h.browser_timeout
            result = next_call(routed)
            # A timeout/error must not leave the engine running or retain a poisoned browser.
            try:
                decoded = json.loads(result) if isinstance(result, str) else result
                failed = isinstance(decoded, dict) and (decoded.get("error") or decoded.get("success") is False)
            except ValueError:
                failed = False
            if failed:
                h.release(owner, lambda name: cleanup_browser(name, task_id))
            return result

    def session_end(*, session_id="", task_id="", turn_id="", **_):
        owner = owner_key(session_id, task_id, turn_id)
        h = current()
        with h.mutex:
            if owner not in h.owners:
                return
        # Hooks may run on a short deadline. Cleanup keeps the lease until it really finishes.
        from agent.memory_provider import spawn_context_thread
        spawn_context_thread(target=lambda: h.release(owner, lambda name: cleanup_browser(name, task_id)),
                             name="resource-harness-cleanup", daemon=True).start()

    ctx.register_middleware("llm_execution", llm_execution)
    ctx.register_middleware("tool_execution", tool_execution)
    ctx.register_hook("on_session_end", session_end)
    ctx.register_system_prompt_section("resource-harness",
        Path(__file__).with_name("research_prompt.md").read_text(encoding="utf-8"))
    # Opt-in consumption happens after gateway authorization and releases the main chat
    # immediately. Disabled by default for existing standalone harness installations.
    from .manager_runtime import register_manager
    register_manager(ctx)
