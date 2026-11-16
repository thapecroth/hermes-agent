"""Opt-in, isolated real-provider profile. Never constructs a messaging adapter.

Invoke through the installed Hermes dependency bootstrap. Artifacts contain timing,
identifiers, and fixed public-documentation task results, never prompts/config/secrets.
"""
import asyncio
import contextlib
import functools
import importlib.util
import json
import os
import resource
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
START = time.perf_counter()
EVENTS = []
LOCK = threading.Lock()
WAVE = "startup"


@contextlib.contextmanager
def span(name, **metadata):
    start, cpu = time.perf_counter(), time.thread_time()
    wave = WAVE
    try:
        yield
    finally:
        event = dict(name=name, wave=wave, start_s=start - START,
                     wall_s=time.perf_counter() - start,
                     thread_cpu_s=time.thread_time() - cpu,
                     thread=threading.current_thread().name, **metadata)
        with LOCK:
            EVENTS.append(event)


def wrap(module, name, label=None):
    original = getattr(module, name)
    @functools.wraps(original)
    def measured(*args, **kwargs):
        with span(label or name):
            return original(*args, **kwargs)
    setattr(module, name, measured)


async def main():
    global WAVE
    process_start = time.process_time()
    with span("startup_imports_and_discovery"):
        from hermes_cli.config import read_raw_config
        from hermes_cli.plugins import discover_plugins, PluginContext, _delivery_manager
        from hermes_cli.plugins_manifest import PluginManifest
        from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs
        from gateway.run_config_loaders import GatewayConfigLoadersMixin
        import model_tools
        model_tools.discover_builtin_tools()
        discover_plugins()
        config = read_raw_config()
        if 'PROFILE_RUNTIME' in globals():
            model, runtime_kwargs = PROFILE_RUNTIME
        else:
            model = _resolve_gateway_model(config)
            runtime_kwargs = _resolve_runtime_agent_kwargs()
        from hermes_constants import get_hermes_home
        source = Path(os.environ.get('HERMES_PROFILE_PLUGIN', str(get_hermes_home() / 'plugins/resource-harness')))
        spec = importlib.util.spec_from_file_location('codon_profile_harness', source / '__init__.py',
                                                    submodule_search_locations=[str(source)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from codon_profile_harness.manager_runtime import GatewayRuntime
        from codon_profile_harness.manager import Store
        import agent.conversation_loop as loop
        import agent.system_prompt as prompt
        import hermes_state
        import hermes_cli.middleware as middleware
        from run_agent import AIAgent

    original_phase = loop._run_phase
    def phase(fn, agent, state, **extra):
        with span('phase:' + fn.__name__, session_id=str(agent.session_id)):
            return original_phase(fn, agent, state, **extra)
    loop._run_phase = phase
    wrap(loop, 'build_turn_context', 'turn_context')
    wrap(prompt, 'build_system_prompt_parts', 'system_prompt')
    for name in ('_execute_write', '_read_retrying_ioerr'):
        wrap(hermes_state.SessionDB, name, 'database:' + name)
    original_chain = middleware._run_execution_chain
    def chain(kind, terminal_call, **kwargs):
        if kind == 'tool_execution' and kwargs.get('tool_name') == 'send_message':
            raise AssertionError('External messaging tools are disabled in this workload')
        def terminal(payload):
            label = 'provider_transport' if kind == 'llm_execution' else 'tool:' + kwargs.get('tool_name', 'unknown')
            with span(label, session_id=str(kwargs.get('session_id', ''))):
                return terminal_call(payload)
        return original_chain(kind, terminal, **kwargs)
    middleware._run_execution_chain = chain
    seen_classes = set()
    for loaded in list(sys.modules.values()):
        if getattr(loaded, '__file__', '') == str(source / 'pool.py'):
            pool_type = loaded.Pool
            if id(pool_type) in seen_classes:
                continue
            seen_classes.add(id(pool_type))
            original_acquire = pool_type.acquire
            def acquire(self, *args, _original=original_acquire, **kwargs):
                with span('admission:' + self.root.name):
                    return _original(self, *args, **kwargs)
            pool_type.acquire = acquire

    ctx = PluginContext(PluginManifest(name='codon-profile-observer'), _delivery_manager())
    hooks = []
    parents = {}
    def subagent_start(**kw):
        with LOCK:
            parents[str(kw['child_session_id'])] = str(kw['parent_session_id'])
    ctx.register_hook('subagent_start', subagent_start)
    def post_tool_call(**kw):
        evidence = {}
        raw = kw.get('result')
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(payload, dict):
                evidence['payload_success'] = payload.get('success') is True and not payload.get('error')
                if kw.get('tool_name') == 'web_search':
                    urls = [urlsplit(row.get('url', '')) for row in payload.get('data', {}).get('web', [])]
                    evidence['python_csv_url_seen'] = any(u.scheme in ('https', 'http') and u.hostname == 'docs.python.org'
                        and u.path.endswith('/library/csv.html') for u in urls)
                    evidence['sqlite_transaction_url_seen'] = any(u.scheme in ('https', 'http') and
                        u.hostname in ('sqlite.org', 'www.sqlite.org') and u.path.endswith('/lang_transaction.html') for u in urls)
                elif kw.get('tool_name') == 'browser_exec':
                    evidence['browser_title_seen'] = payload.get('exit_code') == 0 and 'Example Domain' in payload.get('output', '')
        except (TypeError, ValueError, AttributeError):
            evidence['payload_success'] = False
        with LOCK:
            hooks.append(dict(wave=WAVE, **{k: kw.get(k) for k in
                ('tool_name', 'duration_ms', 'status', 'session_id', 'task_id', 'turn_id')}, **evidence))
    ctx.register_hook('post_tool_call', post_tool_call)
    gateway = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False),
        _is_user_authorized_for_source=lambda source, **kw: True,
        _resolve_session_agent_runtime=lambda **kw: (model, runtime_kwargs),
        _load_reasoning_config=GatewayConfigLoadersMixin._load_reasoning_config)
    worker_ctx = SimpleNamespace(get_config=lambda key, default: default)
    goals = [
        'Use web_search once to find official Python CSV documentation. Return its URL and one sentence about newline handling. Do not delegate, write files, or send messages.',
        'Use web_search once to find official SQLite transaction documentation. Return its URL and one sentence about BEGIN IMMEDIATE. Do not delegate, write files, or send messages.',
        'Use browser_exec once with code new_tab("https://example.com"); wait_for_load(); print(js("document.title")). Return the title. Do not search, delegate, write files, or send messages.'
    ]
    waves = []
    temp = tempfile.mkdtemp(prefix='hermes-codon-profile-')
    try:
        home = Path(temp)
        store = Store(home / 'runtime/resource-harness')
        runtime = GatewayRuntime(worker_ctx, gateway, home, lambda: store)
        async def forbidden_send(*args, **kwargs):
            raise AssertionError('External delivery is disabled in this workload')
        runtime.send = forbidden_send
        try:
            for wave_index, wave in enumerate(('first_jobs', 'process_warm_new_sessions')):
                WAVE = wave
                cpu_start = time.process_time()
                start = time.perf_counter()
                jobs = []
                for lane, goal in enumerate(goals):
                    route = {'session_key': f'codon-profile-{wave}-{lane}', 'source': {
                        'platform': 'telegram', 'chat_type': 'dm', 'chat_id': 'local-profile', 'user_id': 'local-profile'}}
                    jobs.append({'id': 82000 + wave_index * 10 + lane, 'scope': 'local-profile',
                                 'attempt': 0, 'goal': goal, 'route': json.dumps(route)})
                pending = asyncio.gather(*(runtime.execute(j) for j in jobs))
                try:
                    results = await asyncio.wait_for(asyncio.shield(pending), 180)
                except asyncio.TimeoutError:
                    for job in jobs:
                        runtime.cancel(job['id'])
                    await asyncio.wait_for(asyncio.shield(pending), 60)
                    raise
                tool_events = [h for h in hooks if h['wave'] == wave and h['status'] == 'ok']
                def belongs_to(event, lane):
                    node = str(event['session_id'])
                    root = f"rh-manager-parent-{jobs[lane]['id']}-a0"
                    visited = set()
                    while node and node not in visited:
                        if node == root:
                            return True
                        visited.add(node)
                        node = parents.get(node)
                    return False
                semantic_checks = [any(belongs_to(h, lane) and h.get('payload_success') and h.get(flag)
                                       for h in tool_events) for lane, flag in enumerate(
                    ('python_csv_url_seen', 'sqlite_transaction_url_seen', 'browser_title_seen'))]
                result = {'wave': wave, 'wall_s': time.perf_counter() - start,
                          'process_cpu_s': time.process_time() - cpu_start,
                          'states': [r[0] for r in results],
                          'successful_search_calls': sum(h['tool_name'] == 'web_search' for h in tool_events),
                          'successful_browser_calls': sum(h['tool_name'] == 'browser_exec' for h in tool_events),
                          'per_job_tool_semantics': semantic_checks,
                          'browser_title_verified': 'Example Domain' in results[2][1]}
                if any(r[0] != 'completed' for r in results):
                    from agent.redact import redact_sensitive_text
                    result['failure_reasons'] = [redact_sensitive_text(r[1], force=True)[:1000]
                                                 for r in results if r[0] != 'completed']
                waves.append(result)
                print(json.dumps(result), flush=True)
        finally:
            runtime.close()
            from codon_profile_harness.pool import occupied
            from hermes_constants import get_hermes_home
            from browser_harness.admin import daemon_alive
            from tools.browser_use_cli import _sidecar_daemon_name
            owners = {module.owner_key(h.get('session_id'), h.get('task_id'), h.get('turn_id')) for h in hooks}
            browser_names = {module.browser_name(module.owner_key(h.get('session_id'), h.get('task_id'), h.get('turn_id')))
                             for h in hooks if h['tool_name'] == 'browser_exec'}
            deadline = time.monotonic() + 30
            while True:
                leases = occupied(get_hermes_home() / 'runtime/resource-harness')
                remaining_leases = [r for entries in leases.values() for r in entries if r.get('owner') in owners]
                alive = [name for name in browser_names if daemon_alive(name) or daemon_alive(_sidecar_daemon_name(name))]
                threads = [t for t in runtime.executor._threads if t.is_alive()]
                if not runtime.parents and not remaining_leases and not alive and not threads:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Profile cleanup incomplete; isolated workdir retained at ' + temp)
                await asyncio.sleep(.1)
        shutil.rmtree(home)
    except BaseException:
        (OUT / 'incomplete.json').write_text(json.dumps({'events': EVENTS, 'tool_events': hooks,
                                                       'isolated_workdir': temp}, indent=2) + '\n')
        raise
    receipt = {'model': model, 'waves': waves, 'events': EVENTS, 'tool_events': hooks,
               'locale_singleflight': bool(getattr(sys.modules.get('agent.i18n'), '_codon_locale_singleflight', False)),
               'embedded_cpython': '_hermes_codon_runtime_compat' in sys.modules,
               'process_cpu_s': time.process_time() - process_start,
               'wall_s': time.perf_counter() - START,
               'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
               'external_message_sends': 0, 'transcripts_isolated': True,
               'profile_scope': 'isolated actual GatewayRuntime workers; browser subprocess CPU excluded',
               'cleanup_verified': True,
               'passed': all(all(s == 'completed' for s in w['states']) and w['browser_title_verified']
                             and all(w['per_job_tool_semantics'])
                             and w['successful_search_calls'] >= 2 and w['successful_browser_calls'] >= 1 for w in waves)}
    (OUT / 'profile.json').write_text(json.dumps(receipt, indent=2) + '\n')
    assert receipt['passed'], waves
    print('HERMES_PROFILE_COMPLETE_20261114', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
