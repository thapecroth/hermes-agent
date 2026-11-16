"""Report policy follows trusted intake routes into production agent construction."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from gateway.session_execution import GatewaySessionAgentMixin


def turn(report=True, *, trusted=True):
    adapter = object.__new__(WebhookAdapter)
    adapter._routes = {"work:external": {"agent_report": {"source": "worker-a"}} if report else {}}
    source = SimpleNamespace(platform=Platform.WEBHOOK, user_id="webhook:work:external",
        user_id_alt=None, user_name=None, chat_id="attacker:chosen", chat_name=None,
        chat_type="webhook", thread_id=None, metadata={"agent_report": True})
    runner = SimpleNamespace(_intake_adapter_for=lambda src: adapter if trusted else None,
        _session_db=None, _prefill_messages=None, _refresh_fallback_model=lambda: None)
    ctx = SimpleNamespace(source=source, user_config={}, enabled_toolsets=["memory"],
        disabled_toolsets=[], session_id=None, session_key="report-test", AIAgent=Mock())
    execution = GatewaySessionAgentMixin()
    execution._ctx, execution._runner = ctx, runner
    return execution, adapter


@pytest.mark.parametrize("report,trusted,isolated", [(True, True, True), (False, True, False), (True, False, False)])
def test_construction_uses_trusted_route_policy(report, trusted, isolated):
    execution, _ = turn(report, trusted=trusted)
    agent = execution._build_fresh_agent({"model": "fake", "runtime": {}}, "webhook", "owner prompt", 2, {}, {}, False)
    kwargs = execution._ctx.AIAgent.call_args.kwargs
    assert kwargs["skip_memory"] is isolated
    assert kwargs.get("skip_background_review", False) is isolated
    assert kwargs["skip_context_files"] is isolated
    assert kwargs["load_soul_identity"] is (not isolated)
    if isolated:
        assert "memory" in kwargs["disabled_toolsets"]
        assert agent._bot_mode_protocol is False
    assert execution._ctx.disabled_toolsets == []


def test_route_policy_changes_agent_cache_signature():
    execution, adapter = turn()
    runner = execution._runner
    runner._agent_config_signature = GatewayRunner._agent_config_signature
    runner._extract_cache_busting_config = lambda cfg: {}
    runner._apply_fallback_chain_to_agent = lambda *args: None
    execution._cached_sid_is_dead = lambda *args: (None, False)
    execution._current_message_count = lambda: None
    signatures = []

    def lookup(sig, *args):
        signatures.append(sig)
        return execution._CachedAgentLookup(agent=SimpleNamespace(), reused=True)

    execution._lookup_cached_agent = lookup
    args = ({"model": "fake", "runtime": {}}, "webhook", "same prompt", 2, {}, {})
    execution._resolve_turn_agent(*args)
    adapter._routes["work:external"] = {}
    execution._resolve_turn_agent(*args)
    assert signatures[0] != signatures[1]
