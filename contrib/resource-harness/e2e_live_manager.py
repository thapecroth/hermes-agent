"""Opt-in configured-provider canary; no gateway intake or external message delivery.

Run with the installed Hermes environment. Uses the profile's provider credentials
without copying or printing them, and keeps the worker transcript in a temporary DB.
"""
import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


async def main():
    from hermes_constants import get_hermes_home
    from hermes_cli.config import read_raw_config
    from hermes_cli.plugins import discover_plugins
    from agent.plugin_llm import PluginLlm
    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    import model_tools
    model_tools.discover_builtin_tools()
    discover_plugins()
    config = read_raw_config()
    model = _resolve_gateway_model(config)
    runtime_kwargs = _resolve_runtime_agent_kwargs()
    source = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("resource_harness_live_canary", source / "__init__.py",
                                                submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from resource_harness_live_canary.manager_runtime import GatewayRuntime
    from resource_harness_live_canary.manager import Store
    # The shared pools still belong to the actual profile. Only this test's session DB
    # and transcripts are isolated; no production Telegram route is constructed.
    gateway = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False),
        _is_user_authorized_for_source=lambda source, **kw: True,
        _resolve_session_agent_runtime=lambda **kw: (model, runtime_kwargs),
        _load_reasoning_config=GatewayConfigLoadersMixin._load_reasoning_config)
    ctx = SimpleNamespace(get_config=lambda key, default: default, llm=PluginLlm(plugin_id="resource-harness"))
    with tempfile.TemporaryDirectory(prefix="rh-live-manager-") as temp:
        home = Path(temp)
        store = Store(home / "runtime/resource-harness")
        runtime = GatewayRuntime(ctx, gateway, home, lambda: store)
        jobs = [{"id": 1, "state": "running", "goal": "Compare local browser automation options", "result": ""},
                {"id": 2, "state": "running", "goal": "Find Python CSV documentation", "result": ""}]
        examples = [("Also find SQLite transaction documentation as a separate task.", "new", None),
                    ("For the browser comparison in J1, focus on local ARM64 support.", "steer", 1),
                    ("Please cancel the Python CSV documentation job J2.", "cancel", 2)]
        decisions = []
        try:
            for text, action, target in examples:
                value = await runtime.classify(text, jobs)
                assert value.get("action") == action and (target is None or value.get("target") == target), value
                decisions.append({"action": value["action"], "target": value.get("target")})
            route = {"session_key": "rh-live-manager-canary", "source": {"platform": "telegram",
                "chat_type": "dm", "chat_id": "local-captured-canary", "user_id": "local-captured-canary"}}
            result = await asyncio.wait_for(runtime.execute({"id": 81001, "scope": "local-canary", "attempt": 0,
                "goal": "Return exactly LIVE_MANAGER_OK. Do not use tools, search, delegate, write files or send messages.",
                "route": json.dumps(route)}), timeout=90)
            assert result[0] == "completed" and "LIVE_MANAGER_OK" in result[1], result
            assert not runtime.parents, "live worker parent survived completion"
            receipt = {"passed": True, "uses_real_model_provider": True, "model": model,
                       "routing_decisions": decisions, "real_delegated_worker_completed": True,
                       "external_message_sends": 0, "transcript_isolated": True}
            Path(sys.argv[1]).write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt), flush=True)
        finally:
            runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
