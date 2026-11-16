"""Supervisor cleanup and close/reopen races without launching a browser."""
import fcntl
import json
import multiprocessing
import signal
import sys
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from test_harness import harness
from resource_harness import interactive_browser as browser


class InteractiveLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = browser.runtime_dir(self.home)
        self.root.mkdir(parents=True)

    def owner(self, pid=123, born=456):
        (self.root / "owner.json").write_text(json.dumps({"pid": pid, "born": born}))
        (self.root / "worker.sock").touch()

    def test_cleanup_cannot_remove_a_successors_ipc_or_reused_pid(self):
        self.owner()
        browser.clear_worker_files(self.home, 999, 456)
        browser.clear_worker_files(self.home, 123, 777)
        self.assertTrue((self.root / "worker.sock").exists())
        self.assertTrue((self.root / "owner.json").exists())
        browser.clear_worker_files(self.home, 123, 456)
        self.assertFalse((self.root / "worker.sock").exists())
        self.assertFalse((self.root / "owner.json").exists())

    def test_malformed_owner_does_not_blindly_remove_ipc(self):
        self.owner()
        (self.root / "owner.json").write_text("{")
        with self.assertRaises(ValueError):
            browser.clear_worker_files(self.home, 123, 456)
        self.assertTrue((self.root / "worker.sock").exists())

    def test_reopen_waits_for_prior_supervisor_to_finish(self):
        self.owner()
        lifetime = (self.root / "lifetime.lock").open("a+")
        self.addCleanup(lifetime.close)
        fcntl.flock(lifetime, fcntl.LOCK_EX)
        waiting, released = threading.Event(), threading.Event()
        site = self.home / "site"
        (site / "invisible_playwright").mkdir(parents=True)
        binary = self.home / "firefox"
        binary.touch()
        config = {"interactive_site_packages": str(site), "interactive_binary": str(binary)}
        def wait_for_cleanup(_):
            waiting.set()
            if not released.wait(5):
                raise AssertionError("test did not release prior supervisor")
        process = Mock()
        process.poll.return_value = None
        def launch(*_a, **_kw):
            self.assertTrue(released.is_set())
            self.assertFalse((self.root / "owner.json").exists())
            return process
        with patch.object(browser, "rpc", side_effect=[FileNotFoundError(), FileNotFoundError(), {"success": True}]), \
             patch.object(browser, "settings", return_value=config), \
             patch.object(browser, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=wait_for_cleanup)), \
             patch.object(browser.subprocess, "Popen", side_effect=launch) as spawn, \
             ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(browser.request, self.home, "open", url="https://example.test")
            try:
                self.assertTrue(waiting.wait(5))
                spawn.assert_not_called()
                browser.clear_worker_files(self.home, 123, 456)
            finally:
                lifetime.close()
                released.set()
            self.assertEqual(task.result(timeout=5), {"success": True})

    def test_supervisor_retries_cleanup_before_removing_owner_and_releasing_admission(self):
        self.owner()
        worker = Mock(pid=123, exitcode=0)
        worker.is_alive.return_value = False
        admission = Mock()
        attempts = []
        def cleanup(_):
            self.assertTrue((self.root / "owner.json").exists())
            admission.close.assert_not_called()
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("synthetic cleanup failure")
        def released():
            self.assertEqual(len(attempts), 2)
            self.assertFalse((self.root / "owner.json").exists())
            self.assertFalse((self.root / "worker.sock").exists())
        admission.close.side_effect = released
        with patch("ctypes.CDLL", return_value=Mock(prctl=Mock(return_value=0))), \
             patch.object(browser, "Pool", return_value=Mock(acquire=Mock(return_value=admission))), \
             patch("multiprocessing.get_context", return_value=Mock(Process=Mock(return_value=worker))), \
             patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Mock(return_value=Mock(create_time=Mock(return_value=456))))}), \
             patch.object(browser, "firefox_processes", return_value=[]), \
             patch("resource_harness.browser_cleanup.finish_cleanup", side_effect=cleanup), \
             patch.object(browser.signal, "signal"), patch.object(browser.time, "sleep"):
            with self.assertRaises(SystemExit) as stopped:
                browser.supervise(self.home, "test")
        self.assertEqual(stopped.exception.code, 0)
        admission.close.assert_called_once()

    def test_startup_fingerprint_failure_stops_worker_before_releasing_capacity(self):
        self.owner()
        worker = Mock(pid=123)
        worker.is_alive.side_effect = [True, False]
        admission = Mock()
        def cleanup(_):
            worker.terminate.assert_called_once()
            worker.join.assert_called_once_with(8)
            admission.close.assert_not_called()
        process = Mock(create_time=Mock(side_effect=RuntimeError("synthetic fingerprint failure")))
        with patch("ctypes.CDLL", return_value=Mock(prctl=Mock(return_value=0))), \
             patch.object(browser, "Pool", return_value=Mock(acquire=Mock(return_value=admission))), \
             patch("multiprocessing.get_context", return_value=Mock(Process=Mock(return_value=worker))), \
             patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Mock(return_value=process))}), \
             patch.object(browser, "firefox_processes", return_value=[]), \
             patch("resource_harness.browser_cleanup.finish_cleanup", side_effect=cleanup), \
             patch.object(browser.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "synthetic fingerprint failure"):
                browser.supervise(self.home, "test")
        admission.close.assert_called_once()
        self.assertFalse((self.root / "owner.json").exists())
        self.assertFalse((self.root / "worker.sock").exists())

    def test_start_timeout_preserves_supervisor_and_returns_cleanup_handle(self):
        site = self.home / "site"
        (site / "invisible_playwright").mkdir(parents=True)
        binary = self.home / "firefox"
        binary.touch()
        process = Mock(pid=4321)
        process.wait.side_effect = browser.subprocess.TimeoutExpired("supervisor", 15)
        clock = types.SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 41]))
        with patch.object(browser, "rpc", side_effect=FileNotFoundError()), \
             patch.object(browser, "settings", return_value={"interactive_site_packages": str(site), "interactive_binary": str(binary)}), \
             patch.object(browser.subprocess, "Popen", return_value=process), \
             patch.object(browser, "time", clock), patch.object(browser.os, "killpg") as kill_group:
            result = browser.request(self.home, "open", url="https://example.test")
        self.assertEqual(result, {"error": "rendered_browser_start_timeout", "cleanup_pending": True, "supervisor_pid": 4321})
        process.send_signal.assert_called_once_with(browser.signal.SIGTERM)
        process.kill.assert_not_called()
        kill_group.assert_not_called()

    def test_forked_worker_resets_inherited_handlers_before_browser_startup(self):
        context = multiprocessing.get_context("fork")
        observed, ready = context.Pipe(duplex=False)
        def starting(*_):
            ready.send(True)
            time.sleep(10)
            raise RuntimeError("startup should have been terminated")
        old = signal.signal(signal.SIGTERM, lambda *_: None)
        process = context.Process(target=browser.serve, args=(self.home, "test", context.Event()))
        try:
            with patch.object(browser, "RenderedBrowser", side_effect=starting), \
                 patch.dict(sys.modules, {"tools.bot_desktop": types.SimpleNamespace(lease=Mock())}):
                process.start()
        finally:
            signal.signal(signal.SIGTERM, old)
        try:
            self.assertTrue(observed.poll(5))
            self.assertTrue(observed.recv())
            process.terminate()
            process.join(3)
            self.assertFalse(process.is_alive(), "worker retained the supervisor's signal handler")
            self.assertEqual(process.exitcode, -signal.SIGTERM)
        finally:
            if process.is_alive():
                process.kill()
            process.join(5)
            ready.close()
            observed.close()


if __name__ == "__main__":
    unittest.main()
