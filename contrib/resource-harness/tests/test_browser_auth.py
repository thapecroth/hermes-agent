"""Boundaries for private state, logout, stale concurrent writers and imports."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_harness import harness
from resource_harness.browser_auth import (
    ProtocolError, cookie_parameters, executor_auth, loopback_endpoint,
    owner_marker, read_private, snapshot, state_path, write_private,
)
from resource_harness.interactive_browser import evaluate_document, http_url, RenderedBrowser


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_snapshot_is_private_and_rejects_public_or_symlink_inputs(self):
        path = self.home / "auth/state.json"
        state = {"version": 1, "cookies": [], "origins": []}
        write_private(path, state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(read_private(path), state)
        alias = self.home / "auth/link.json"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            read_private(alias)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_private(path)

    def test_partitioned_cookie_is_never_flattened_into_lightpanda(self):
        cookie = {"name": "x", "value": "synthetic", "domain": "example.test", "path": "/",
                  "partitionKey": {"topLevelSite": "https://other.test", "hasCrossSiteAncestor": False}}
        self.assertIsNone(cookie_parameters(cookie))
        self.assertEqual(cookie_parameters(cookie, lightpanda=False)["partitionKey"], cookie["partitionKey"])

    def test_credential_transport_refuses_remote_cdp_and_embedded_credentials(self):
        self.assertEqual(loopback_endpoint("http://127.0.0.1:9222"), "http://127.0.0.1:9222")
        for value in ("https://example.test:443", "http://127.0.0.1.evil:9222", "http://u:p@127.0.0.1:9222", "http://127.0.0.1:9222?token=x"):
            with self.assertRaises(ValueError):
                loopback_endpoint(value)
        with self.assertRaises(ValueError):
            http_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            http_url("https://user:secret@example.test")

    def test_stale_owner_cannot_overwrite_newer_logout(self):
        identity, owner = "conversation", "conversation:old-turn"
        path = state_path(self.home, identity)
        write_private(path, {"version": 1, "saved_at": 2, "cookies": [], "origins": []})
        write_private(owner_marker(self.home, owner), {"revision": 1, "consumed": []})
        with self.assertRaisesRegex(ProtocolError, "stale save"):
            executor_auth(lambda *_a, **_k: {}, home=self.home, identity=identity, owner=owner, action="save")
        self.assertEqual(read_private(path)["saved_at"], 2)

    def test_existing_owner_does_not_reimport_seed_after_logout(self):
        write_private(owner_marker(self.home, "o"), {"revision": 1, "consumed": []})
        def must_not_call(*_a, **_k):
            raise AssertionError("credentials were reimported")
        self.assertEqual(executor_auth(must_not_call, home=self.home, identity="i", owner="o", action="restore"), {"already_restored": True})

    def test_rendered_owner_cannot_overwrite_newer_headless_logout(self):
        browser = RenderedBrowser.__new__(RenderedBrowser)
        browser.state = state_path(self.home, "conversation")
        browser.revision = 1
        write_private(browser.state, {"version": 1, "saved_at": 2, "cookies": [], "origins": []})
        with self.assertRaisesRegex(ProtocolError, "stale save"):
            browser.save()
        self.assertEqual(read_private(browser.state)["saved_at"], 2)

    def test_human_logout_consumes_pending_origin_before_agent_navigation(self):
        browser = RenderedBrowser.__new__(RenderedBrowser)
        browser.state = state_path(self.home, "conversation")
        browser.revision = 1
        browser.profile = self.home / "rendered"
        browser.seeded = browser.profile / ".auth-imported"
        visited = {"origin": "https://visited.test", "localStorage": [], "sessionStorage": []}
        untouched = {"origin": "https://untouched.test", "localStorage": [], "sessionStorage": []}
        stale = {**visited, "localStorage": [{"name": "token", "value": "synthetic-old-token"}]}
        write_private(browser.state, {"version": 1, "saved_at": 1, "cookies": [], "origins": [stale, untouched]})
        pending = browser.profile / ".pending-storage.json"
        write_private(pending, {"version": 1, "cookies": [], "origins": [stale, untouched]})
        page = Mock()
        page.is_closed.return_value = False
        page.evaluate.return_value = visited
        browser.page = page
        browser.context = Mock(pages=[page])
        browser.context.cookies.return_value = []
        browser.inspect = Mock(return_value={})
        browser.save()
        self.assertEqual(read_private(pending)["origins"], [untouched])
        self.assertEqual(read_private(browser.state)["origins"], [visited, untouched])
        browser.navigate("https://visited.test/again")
        for call in page.add_init_script.call_args_list:
            self.assertNotIn("synthetic-old-token", call.kwargs["script"])
            self.assertNotIn("https://visited.test", call.kwargs["script"])

    def test_unresponsive_chromium_page_does_not_discard_cookie_export(self):
        class Connection:
            async def pages(self): return [{"targetId": "paused-tab"}]
            async def attach(self, _): return "session"
            async def evaluate(self, *_): raise TimeoutError()
            async def call(self, *_): return {}
            async def cookies(self): return []
        state = asyncio.run(snapshot(Connection()))
        self.assertEqual(state["unreadable_pages"], 1)
        self.assertEqual(state["cookies"], [])

    def test_document_reload_retries_reads_but_not_unrelated_errors(self):
        page = Mock()
        page.evaluate.side_effect = [RuntimeError("Execution context was destroyed"), {"ready": True}]
        with patch("resource_harness.interactive_browser.time.sleep"):
            self.assertEqual(evaluate_document(page, "document.readyState"), {"ready": True})
        self.assertEqual(page.evaluate.call_count, 2)
        page.evaluate.reset_mock(side_effect=True)
        page.evaluate.side_effect = ValueError("synthetic non-navigation error")
        with self.assertRaisesRegex(ValueError, "non-navigation error"):
            evaluate_document(page, "document.readyState")
        self.assertEqual(page.evaluate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
