"""Failure boundaries that must not accidentally return a live browser's slot."""
import sys
import os
import threading
import types
import unittest
from unittest.mock import Mock, patch

from test_harness import harness
from resource_harness import browser_cleanup as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.name = 'test-owner'
        self.addCleanup(cleanup.forget_verified, self.name)
        self.infos = {'primary': {'session_name': 'lp-test', 'features': {'lightpanda': True}}}
        self.engine = Mock(pid=731)
        self.engine.create_time.return_value = 100
        self.engine.children.return_value = []
        self.engine.is_running.return_value = True
        self.engine.status.return_value = 'running'
        self.server = Mock(proc=Mock(pid=731))
        self.server.is_alive.return_value = True
        self.native_cleanup = Mock()
        self.admin = types.SimpleNamespace(daemon_alive=Mock(return_value=False), restart_daemon=Mock())
        ipc = types.SimpleNamespace(identify=Mock(return_value=None))
        self.modules = {
            'psutil': types.SimpleNamespace(Process=Mock(return_value=self.engine),
                NoSuchProcess=ProcessLookupError, STATUS_ZOMBIE='zombie'),
            'tools.browser_tool': types.SimpleNamespace(_cleanup_lock=threading.RLock(), _active_sessions=self.infos),
            'tools.browser_use_cli': types.SimpleNamespace(
                _backend_cache_key=lambda _, name, sidecar=False: 'sidecar' if sidecar else 'primary',
                _real_profile_consented=lambda: False,
                _sidecar_daemon_name=lambda name: name + '-sidecar'),
            'tools.browser_lightpanda': types.SimpleNamespace(get_server=lambda name: self.server),
            'tools.browser_tool_lifecycle': types.SimpleNamespace(
                _verify_reapable_browser_daemon=Mock(return_value=False), cleanup_browser=self.native_cleanup),
            'tools.browser_supervisor': types.SimpleNamespace(SUPERVISOR_REGISTRY=Mock()),
            'tools.browser_tool_cdp': types.SimpleNamespace(_get_cdp_override_raw=lambda: ''),
            'browser_harness.admin': self.admin,
            'browser_harness': types.SimpleNamespace(_ipc=ipc),
            'browser_harness._ipc': ipc,
        }

    def test_survivor_remains_tracked_when_core_erases_metadata(self):
        with patch.dict(sys.modules, self.modules):
            first = cleanup.capture_processes(self.name)
            self.infos.clear()
            second = cleanup.capture_processes(self.name)
            self.assertEqual(first, second)
            self.assertEqual(second, [self.engine])
            with self.assertRaisesRegex(RuntimeError, '731'):
                cleanup.verify_stopped(second, timeout=0)
            self.engine.is_running.return_value = False
            cleanup.verify_stopped(cleanup.capture_processes(self.name), timeout=0)
            cleanup.forget_verified(self.name)
            self.assertEqual(cleanup.capture_processes(self.name), [])

    def test_capture_failure_cannot_skip_driver_or_engine_teardown(self):
        with patch.dict(sys.modules, self.modules), \
                patch.object(cleanup, 'capture_processes', side_effect=ValueError('bad metadata')), \
                patch.object(cleanup, 'verify_stopped'):
            with self.assertRaisesRegex(RuntimeError, 'bad metadata'):
                harness.cleanup_browser(self.name)
        self.assertEqual(self.admin.daemon_alive.call_count, 2)
        self.assertEqual(self.native_cleanup.call_count, 2)

    def test_driver_teardown_failure_cannot_skip_engine_teardown(self):
        self.admin.daemon_alive.return_value = True
        self.admin.restart_daemon.side_effect = RuntimeError('driver failed')
        with patch.dict(sys.modules, self.modules), \
                patch.object(cleanup, 'capture_processes', return_value=[]), \
                patch.object(cleanup, 'verify_stopped'):
            with self.assertRaisesRegex(RuntimeError, 'driver failed'):
                harness.cleanup_browser(self.name)
        self.assertEqual(self.admin.restart_daemon.call_count, 2)
        self.assertEqual(self.native_cleanup.call_count, 2)

    def test_reused_pid_does_not_count_as_our_survivor(self):
        self.engine.is_running.return_value = False
        with patch.dict(sys.modules, self.modules):
            cleanup.verify_stopped([self.engine], timeout=0)

    def test_verified_survivor_is_terminated_before_returning_capacity(self):
        self.engine.terminate.side_effect = lambda: setattr(self.engine.is_running, 'return_value', False)
        with patch.dict(sys.modules, self.modules):
            cleanup.finish_cleanup([self.engine], grace=0)
        self.engine.terminate.assert_called_once()
        self.engine.kill.assert_not_called()

    def test_saved_shared_cdp_endpoint_is_rejected(self):
        self.modules['tools.browser_tool_cdp']._get_cdp_override_raw = lambda: 'http://127.0.0.1:9222'
        with patch.dict(sys.modules, self.modules):
            self.assertTrue(cleanup.shared_endpoint_configured())

    def test_environment_cdp_override_is_rejected(self):
        with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {'BU_CDP_WS':'ws://127.0.0.1:9222'}):
            self.assertTrue(cleanup.shared_endpoint_configured())

    def test_consented_real_profile_is_rejected(self):
        self.modules['tools.browser_use_cli']._real_profile_consented = lambda: True
        with patch.dict(sys.modules, self.modules):
            self.assertTrue(cleanup.shared_endpoint_configured())

    def test_persistent_browser_profile_is_rejected(self):
        with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {'AGENT_BROWSER_PROFILE':'/tmp/shared-profile'}):
            self.assertTrue(cleanup.shared_endpoint_configured())


if __name__ == '__main__':
    unittest.main()
