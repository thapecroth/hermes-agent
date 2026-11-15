"""Opt-in compatibility fixes for the isolated CPython-in-Codon launcher.

Install before plugin discovery or worker creation. The shared lock prevents
duplicate cold YAML parsing and prevents an old build publishing after a reset.
This module does not replace the YAML parser or change catalog layering.
"""
from functools import wraps
import threading


def install_locale_singleflight(module=None):
    if module is None:
        from agent import i18n as module
    if getattr(module, '_codon_locale_singleflight', False):
        return False
    lock = threading.RLock()

    def serialize(original):
        @wraps(original)
        def locked(*args, **kwargs):
            with lock:
                return original(*args, **kwargs)
        return locked

    # supported_languages also publishes a cached view that reset invalidates.
    for name in ('_load_catalog', 'supported_languages', 'reset_language_cache'):
        setattr(module, name, serialize(getattr(module, name)))
    # Surface APIs can load these layers directly, bypassing _load_catalog.
    # Their parse/build/publish must finish before a facade reset clears them.
    for name in ('overlay_layer', 'overlay_languages'):
        setattr(module.i18n_layers, name, serialize(getattr(module.i18n_layers, name)))
    module._codon_locale_singleflight = True
    return True
