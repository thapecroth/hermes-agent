"""Worker sender validates locally and uses the existing generic HMAC wire contract."""
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.send_agent_report import main


def test_dry_run_needs_no_secret_or_transport(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_REPORT_SECRET", raising=False)
    def no_network(*args, **kwargs):
        raise AssertionError("dry run must not send")
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    fixture = Path(__file__).resolve().parents[2] / "scripts/fixtures/agent-report.json"
    assert main([str(fixture), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["report_id"] == "report-001"


def test_send_signs_exact_validated_bytes(tmp_path, monkeypatch):
    fixture = Path(__file__).resolve().parents[2] / "scripts/fixtures/agent-report.json"
    monkeypatch.setenv("AGENT_REPORT_SECRET", "fixture-only-secret")
    response = MagicMock()
    response.__enter__.return_value = SimpleNamespace(status=202)
    calls = []
    def send(request, timeout):
        calls.append(request)
        return response
    monkeypatch.setattr("urllib.request.urlopen", send)
    assert main([str(fixture), "--url", "https://reviewer.example/webhooks/work-reports"]) == 0
    request = calls[0]
    headers = dict((key.lower(), value) for key, value in request.header_items())
    timestamp = headers["x-webhook-timestamp"]
    expected = hmac.new(b"fixture-only-secret", timestamp.encode() + b"." + request.data, hashlib.sha256).hexdigest()
    assert headers["x-webhook-signature-v2"] == expected
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"toolsets": ["terminal"]}')
    assert main([str(invalid), "--url", "https://reviewer.example/webhooks/work-reports"]) == 1
    assert len(calls) == 1
