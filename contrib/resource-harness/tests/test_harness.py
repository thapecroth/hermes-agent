"""Full standalone suite: run with python -m unittest discover -s tests -v."""
from __future__ import annotations

import importlib.util
import multiprocessing
import sys
import tempfile
import threading
import unittest
import types
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("resource_harness", ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
harness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = harness
spec.loader.exec_module(harness)
from resource_harness.pool import CapacityTimeout, Pool, occupied
from resource_harness.watchdog import assess


def hold_in_child(root, ready, release):
    lease = Pool(Path(root), "browser", 1).acquire("crash-canary", 5)
    ready.set()
    release.wait(30)
    lease.close()


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_ten_api_calls_overlap_and_eleventh_waits(self):
        pool = Pool(self.root, "api", 10)
        barrier = threading.Barrier(11)
        release = threading.Event()
        def work(index):
            with pool.acquire(str(index), 5):
                barrier.wait(timeout=10)
                self.assertTrue(release.wait(10))
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(work, n) for n in range(10)]
            try:
                barrier.wait(timeout=10)
                self.assertEqual(len(occupied(self.root)["api"]), 10)
                with self.assertRaises(CapacityTimeout):
                    pool.acquire("eleventh", 0)
            finally:
                release.set()
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(occupied(self.root)["api"], [])

    def test_browser_slots_cover_session_lifetime_and_cleanup(self):
        h = harness.Harness(self.root, wait=0)
        h.retain("browser", "one")
        h.retain("browser", "two")
        self.assertIs(h.retain("browser", "one"), h.retain("browser", "one"))
        with self.assertRaises(CapacityTimeout):
            h.retain("browser", "three")
        with h.pools["api"].acquire("API-still-progresses", 0):
            self.assertEqual(len(occupied(self.root)["browser"]), 2)
        def cleanup(_):
            with self.assertRaises(CapacityTimeout):
                h.retain("browser", "three")
        self.assertTrue(h.release("one", cleanup))
        h.retain("browser", "three")
        h.release("two", lambda _: None)
        h.release("three", lambda _: None)
        self.assertEqual(occupied(self.root)["browser"], [])

    def test_failed_cleanup_does_not_release_capacity(self):
        h = harness.Harness(self.root, browsers=1, wait=0)
        h.retain("browser", "broken")
        def broken(_):
            raise RuntimeError("injected cleanup failure")
        with self.assertLogs(harness.log, level="ERROR"):
            self.assertFalse(h.release("broken", broken))
        with self.assertRaises(CapacityTimeout):
            h.retain("browser", "replacement")
        self.assertTrue((self.root / "cleanup-failed").exists())
        h.release("broken", lambda _: None)

    def test_process_crash_releases_file_lock(self):
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        child = ctx.Process(target=hold_in_child, args=(str(self.root), ready, release))
        child.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaises(CapacityTimeout):
                Pool(self.root, "browser", 1).acquire("blocked", 0)
            child.kill()
            child.join(10)
            self.assertFalse(child.is_alive())
            with Pool(self.root, "browser", 1).acquire("replacement", 2):
                self.assertEqual(len(occupied(self.root)["browser"]), 1)
        finally:
            if child.is_alive():
                child.kill()
                child.join(10)

    def test_cancelled_wait_never_enters_pool(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CapacityTimeout):
            Pool(self.root, "api", 1).acquire("cancelled", 5, cancel)
        self.assertEqual(occupied(self.root)["api"], [])

    def test_downstream_failure_releases_api_slot(self):
        pool = Pool(self.root, "api", 1)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with pool.acquire("failure", 0):
                raise RuntimeError("injected")
        with pool.acquire("replacement", 0):
            self.assertEqual(len(occupied(self.root)["api"]), 1)

    def test_watchdog_preserves_manual_stop_and_startup_grace(self):
        fatal = {"pid": 3, "platforms": {"telegram": {"state": "fatal"}}}
        unit = {"MainPID": "3", "ActiveState": "inactive"}
        self.assertIsNone(assess(unit, fatal, False, True, uptime=1000))
        unit["ActiveState"] = "active"
        self.assertIsNone(assess(unit, fatal, False, True, uptime=100))
        self.assertIn("telegram", assess(unit, fatal, True, False, uptime=1000))
        fatal["platforms"]["telegram"]["state"] = "connected"
        self.assertIsNone(assess(unit, fatal, True, False, uptime=1000))

    def test_session_end_starts_cleanup_and_releases_agent_slot(self):
        callbacks = {}
        done = threading.Event()
        class Context:
            def get_config(self, _, default): return default
            def register_middleware(self, name, cb): callbacks[name] = cb
            def register_hook(self, name, cb): callbacks[name] = cb
            def register_system_prompt_section(self, *_): pass
        def module(name, **values):
            result = types.ModuleType(name)
            result.__dict__.update(values)
            return result
        def spawn_context_thread(target, **kw):
            def run():
                try: target()
                finally: done.set()
            return threading.Thread(target=run, **kw)
        modules = {
            "hermes_constants": module("hermes_constants", get_hermes_home=lambda: self.root),
            "agent": module("agent"),
            "agent.delegation_context": module("agent.delegation_context", is_delegated_child_context=lambda: True),
            "agent.memory_provider": module("agent.memory_provider", spawn_context_thread=spawn_context_thread),
            "hermes_cli": module("hermes_cli"),
            "hermes_cli.middleware": module("hermes_cli.middleware", _DownstreamExecutionError=RuntimeError),
            "tools": module("tools"),
            "tools.interrupt": module("tools.interrupt", is_interrupted=lambda: False),
        }
        with patch.dict(sys.modules, modules):
            harness.register(Context())
            callbacks["llm_execution"](request={}, next_call=lambda _: "completed",
                                       session_id="child", turn_id="turn")
            root = self.root / "runtime/resource-harness"
            self.assertEqual(len(occupied(root)["agents"]), 1)
            callbacks["on_session_end"](session_id="child", turn_id="turn")
            self.assertTrue(done.wait(5))
            self.assertEqual(occupied(root)["agents"], [])


if __name__ == "__main__":
    unittest.main()
