"""Run with the installed Hermes dependency bootstrap; uses real catalog code.

HERMES_TEST_UNPATCHED=1 demonstrates the concurrent cold-load failure before
the adapter. Run the normal command afterward to verify the corrected boundary.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent import i18n
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_compat import install_locale_singleflight


class LocaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('HERMES_TEST_UNPATCHED') != '1':
            install_locale_singleflight()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        i18n.reset_language_cache()
        self.addCleanup(i18n.reset_language_cache)

    def test_concurrent_cold_load_parses_once(self):
        entered, release = threading.Event(), threading.Event()
        start = threading.Barrier(4)
        calls = []
        def bundled(lang):
            calls.append(lang)
            entered.set()
            self.assertTrue(release.wait(3))
            return {'test': 'bundled'}
        def load():
            start.wait(3)
            return i18n._load_catalog('en', self.home)
        with patch.object(i18n, '_load_bundled', bundled), ThreadPoolExecutor(3) as pool:
            pending = [pool.submit(load) for _ in range(3)]
            start.wait(3)
            try:
                self.assertTrue(entered.wait(3))
                # Block the parse until all callers have entered the original
                # unpatched loader, or are queued on the adapter's shared lock.
                if not getattr(i18n, '_codon_locale_singleflight', False):
                    import time
                    deadline = time.monotonic() + 2
                    while len(calls) < 3 and time.monotonic() < deadline:
                        threading.Event().wait(.005)
            finally:
                release.set()
            values = [f.result(3) for f in pending]
        self.assertEqual(calls, ['en'])
        self.assertTrue(all(v is values[0] for v in values))

    def test_reset_cannot_be_overtaken_by_inflight_publish(self):
        entered, release, resetting = threading.Event(), threading.Event(), threading.Event()
        cleared = threading.Event()
        original_clear = i18n.i18n_layers.clear_cache
        def clear():
            original_clear()
            cleared.set()
        def bundled(lang):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'test': 'old'}
        def reset():
            resetting.set()
            i18n.reset_language_cache()
        with patch.object(i18n, '_load_bundled', bundled), patch.object(i18n.i18n_layers, 'clear_cache', clear), ThreadPoolExecutor(2) as pool:
            loading = pool.submit(i18n._load_catalog, 'en', self.home)
            try:
                self.assertTrue(entered.wait(3))
                invalidating = pool.submit(reset)
                self.assertTrue(resetting.wait(3))
                # In the unpatched implementation this observes the real reset
                # boundary before allowing the stale parse to publish.
                if getattr(i18n, '_codon_locale_singleflight', False):
                    self.assertFalse(cleared.wait(.1))
                else:
                    self.assertTrue(cleared.wait(3))
            finally:
                release.set()
            loading.result(3)
            invalidating.result(3)
        self.assertNotIn((self.home, 'en'), i18n._catalog_cache)

    def test_overlay_pack_home_and_reset_semantics(self):
        from agent import i18n_layers
        other = Path(self.home) / 'other'
        locales = Path(self.home) / 'locales'
        locales.mkdir()
        overlay = locales / 'en.yaml'
        overlay.write_text('test: overlay\n', encoding='utf-8')
        with patch.object(i18n, '_load_bundled', side_effect=lambda lang: {'test': 'bundled'}):
            self.assertEqual(i18n._load_catalog('en', self.home)['test'], 'overlay')
            self.assertEqual(i18n._load_catalog('en', str(other))['test'], 'bundled')
            pack = i18n_layers.register_pack('en', 'core', {'test': 'pack'}, source='codon-test')
            try:
                self.assertEqual(i18n._load_catalog('en', self.home)['test'], 'pack')
            finally:
                i18n_layers.unregister_pack(pack)
            overlay.write_text('test: updated\n', encoding='utf-8')
            i18n.reset_language_cache()
            self.assertEqual(i18n._load_catalog('en', self.home)['test'], 'updated')

    def test_install_is_idempotent(self):
        previous = i18n._load_catalog
        self.assertFalse(install_locale_singleflight())
        self.assertIs(previous, i18n._load_catalog)

    def test_direct_surface_load_cannot_publish_after_reset(self):
        layers = i18n.i18n_layers
        locales = Path(self.home) / 'locales'
        locales.mkdir()
        (locales / 'en.tui.yaml').write_text('test: old\n', encoding='utf-8')
        entered, release, cleared, resetting = (threading.Event() for _ in range(4))
        original_clear = layers.clear_cache
        def parse(path):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'test': 'old'}
        def clear():
            original_clear()
            cleared.set()
        def reset():
            resetting.set()
            i18n.reset_language_cache()
        with patch.object(layers, 'parse_locale_file', parse), patch.object(layers, 'clear_cache', clear), ThreadPoolExecutor(2) as pool:
            loading = pool.submit(layers.overlay_layer, self.home, 'en', 'tui')
            try:
                self.assertTrue(entered.wait(3))
                invalidating = pool.submit(reset)
                self.assertTrue(resetting.wait(3))
                if getattr(i18n, '_codon_locale_singleflight', False):
                    self.assertFalse(cleared.wait(.1))
                else:
                    self.assertTrue(cleared.wait(3))
            finally:
                release.set()
            loading.result(3)
            invalidating.result(3)
        self.assertNotIn((self.home, 'en', 'tui'), layers._overlay_cache)


if __name__ == '__main__':
    unittest.main()
