"""Report receiver grants reach the real tool resolver without implicit defaults."""
from types import SimpleNamespace

import pytest

from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from model_tools import _select_tool_names


@pytest.mark.parametrize("settings,expected", [({}, []), ({"toolsets": []}, []),
    ({"toolsets": "terminal"}, []), ({"toolsets": ["web"]}, ["web"])])
def test_report_grants_override_privileged_platform_defaults(settings, expected):
    adapter = object.__new__(WebhookAdapter)
    adapter._routes = {"work:external": {"agent_report": {"source": "worker-a"}, **settings}}
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    source = SimpleNamespace(user_id="webhook:work:external", chat_id="attacker:chosen:route")
    config = {"platform_toolsets": {"webhook": ["terminal", "file", "memory", "messaging"]}}
    enabled = runner._resolve_enabled_toolsets_for_source(config, source, "webhook")
    assert enabled == expected
    names = _select_tool_names(enabled, None, quiet_mode=True)
    assert not {"terminal", "read_file", "write_file", "patch", "memory", "send_message", "message_agent"} & set(names)
    if not expected:
        assert not names
