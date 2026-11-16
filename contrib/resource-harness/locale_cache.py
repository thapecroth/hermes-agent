"""Serialize cold locale builds and invalidation without changing catalog semantics."""
from functools import wraps
import threading

def install_locale_singleflight(module=None):
    if module is None:
        from agent import i18n as module
    # Pilot and plugin can import this file under different module names. Share
    # installation ownership on the target, just as we share the installed marker.
    install_lock = module.__dict__.setdefault('_resource_harness_locale_install_lock', threading.Lock())
    with install_lock:
        if getattr(module, '_codon_locale_singleflight', False):
            return False
        lock = threading.RLock()
        targets = [(module, name) for name in
                   ('_load_catalog', 'supported_languages', 'reset_language_cache')]
        targets += [(module.i18n_layers, name) for name in ('overlay_layer', 'overlay_languages')]
        originals = [(target, name, getattr(target, name)) for target, name in targets]

        def serialize(original):
            @wraps(original)
            def locked(*args, **kwargs):
                with lock:
                    return original(*args, **kwargs)
            return locked

        # All facade and direct overlay entrypoints share a lock: reset cannot
        # finish while an older build is still able to publish a cached value.
        for target, name, original in originals:
            setattr(target, name, serialize(original))
        # Retain the pilot marker so existing qualification scripts stay valid.
        module._codon_locale_singleflight = True
        return True
