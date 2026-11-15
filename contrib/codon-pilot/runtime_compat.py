"""Compatibility import for the locale fix, now owned by the production plugin."""
import importlib.util
from pathlib import Path
import threading

_ADAPTER = None
_LOAD_LOCK = threading.Lock()


def install_locale_singleflight(module=None):
    global _ADAPTER
    with _LOAD_LOCK:
        if _ADAPTER is None:
            path = Path(__file__).resolve().parents[1] / 'resource-harness' / 'locale_cache.py'
            if not path.is_file():
                from hermes_constants import get_hermes_home
                path = get_hermes_home() / 'plugins/resource-harness/locale_cache.py'
            spec = importlib.util.spec_from_file_location('_hermes_locale_cache', path)
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            _ADAPTER = adapter
    return _ADAPTER.install_locale_singleflight(module)
