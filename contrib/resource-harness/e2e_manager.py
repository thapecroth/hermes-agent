"""Actual Hermes admission, delegation, steering and approval queues; captured Telegram sends.

Provider responses are deterministic fixtures. No Telegram connection or model credentials
are used. Run with the installed Hermes Python and checkout on sys.path.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import weakref
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


async def qualify(source, home):
    from agent.secret_scope import set_secret_scope
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(home)
    set_secret_scope({}, profile_home=str(home))
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    import gateway.run as gateway_module
    from gateway.run import GatewayRunner
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from run_agent import AIAgent
    from openai.types.chat import ChatCompletion
    import model_tools
    model_tools.discover_builtin_tools()

    captured = []
    async def send_final(event, session_key, text, metadata, **kwargs):
        captured.append({"text": text, "id": event.message_id, "session": session_key})
        return SimpleNamespace(success=True), None
    adapter = SimpleNamespace(send=AsyncMock(), send_final_ledgered=send_final)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)}, multiplex_profiles=False)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.pairing_store = MagicMock()
    runner.session_store = MagicMock()
    runner._is_user_authorized_for_source = lambda s, **kw: s.user_id in {"harness-user", "other-user"}
    runner._admit_bot_message_for_source = lambda s: True
    runner._delivery_adapter_for = lambda s: adapter
    runner._intake_adapter_for = lambda s: adapter
    runner._rescue_orphaned_overflow = lambda *a: None
    runner._enqueue_fifo = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="UNEXPECTED MAIN TURN")
    runner._run_post_turn_hooks = AsyncMock()
    runner._thread_metadata_for_source = lambda s: {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._resolve_session_agent_runtime = lambda **kw: ("gpt-4.1-mini", {
        "provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key": "fixture-only",
        "api_mode": "chat_completions"})
    gateway_module._gateway_runner_ref = weakref.ref(runner)
    src = SessionSource(platform=Platform.TELEGRAM, user_id="harness-user", chat_id="harness-chat",
                        user_name="fixture", chat_type="dm")
    key = runner._session_key_for_source(src)
    gates = defaultdict(threading.Event)
    entered = defaultdict(threading.Event)
    calls = defaultdict(int)
    classifier_inputs = []
    worker_approval_keys = {}
    interrupted = {}
    review_calls = defaultdict(int)
    review_modes = []
    review_entered = threading.Event()
    review_release = threading.Event()
    active_review = []
    skill_file = home / "skills/fixture-learning/SKILL.md"

    def completion(text=None, tool=None, arguments=None):
        message = {"role": "assistant", "content": text}
        if tool:
            message["tool_calls"] = [{"id": "fixture-call", "type": "function", "function": {
                "name": tool if isinstance(tool, str) else "terminal", "arguments": json.dumps(arguments or {
                    "command": "printf 'fixture-safe-boundary'", "timeout": 5})}}]
        return ChatCompletion.model_validate({"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "gpt-4.1-mini", "choices": [{"index": 0, "message": message,
            "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

    def provider(agent, kwargs, *args, **kw):
        from tools.approval_context import get_current_session_key
        messages = kwargs.get("messages", [])
        if getattr(agent, "_turn_origin", None) == "background_review":
            identity = agent.session_id
            review_calls[identity] += 1
            count = review_calls[identity]
            mode = ("cancel" if "CANCEL_SKILL_REVIEW" in json.dumps(messages) else
                    "reject" if "REJECT_SKILL_PATCH" in json.dumps(messages) else
                    "update" if "E2E_SKILL" in json.dumps(messages) else "noop")
            if count == 1:
                review_modes.append(mode)
            if mode == "cancel":
                active_review.append(agent)
                review_entered.set()
                assert review_release.wait(25), "review cancellation fixture timed out"
                if agent.is_interrupted:
                    raise InterruptedError("review observed the stop fence")
                return completion("Nothing to save.")
            if mode == "noop":
                return completion("Nothing to save.")
            if count == 1:
                return completion(tool="skill_view", arguments={"name": "fixture-learning"})
            if count == 2:
                return completion(tool="skill_manage", arguments={"action": "patch", "name": "fixture-learning",
                    "old_string": "MISSING_MARKER" if mode == "reject" else "BEFORE_MANAGER_LEARNING",
                    "new_string": "AFTER_MANAGER_LEARNING"})
            return completion("Skill review finished.")
        users = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
        initial = users[0] if users else ""
        label = "B" if "E2E_JOB_B" in initial else "A" if "E2E_JOB_A" in initial else "MEDIA"
        worker_approval_keys[label] = get_current_session_key()
        interrupted[label] = agent
        calls[agent.session_id] += 1
        count = calls[agent.session_id]
        entered[label].set()
        if label in {"A", "B"} and count == 1:
            assert gates[label].wait(45), f"{label} fixture gate timed out"
        if agent.is_interrupted:
            raise InterruptedError("fixture provider observed the hard interrupt")
        if label == "A" and count == 1:
            return completion(tool=True)
        return completion("RESULT " + label + (" STEER_MARKER" if "STEER_MARKER" in json.dumps(messages) else ""))

    def classify(plugin_llm, kwargs):
        payload = json.dumps(kwargs["messages"])
        classifier_inputs.append(payload)
        if "Add STEER_MARKER" in payload:
            directive = {"action": "steer", "target": 1, "message": "Add STEER_MARKER to the result"}
        elif "cancel the second" in payload:
            directive = {"action": "cancel", "target": 2}
        else:
            directive = {"action": "new", "title": "Research Beta" if "E2E_JOB_B" in payload else "Research Alpha"}
        return "fixture", "fixture", completion(json.dumps(directive))

    spec = importlib.util.spec_from_file_location("manager_e2e_store", source / "manager.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = module.Store(home / "runtime/resource-harness")
    async def until(check, description, timeout=35):
        deadline = time.monotonic() + timeout
        while not check():
            assert time.monotonic() < deadline, description
            await asyncio.sleep(.05)
    async def inbound(text, identity, source=src, **kw):
        before = time.monotonic()
        reply = await asyncio.wait_for(runner._handle_message(MessageEvent(text=text, source=source,
                        message_id=identity, **kw)), 2)
        assert not runner._is_session_running(runner._session_key_for_source(source)), "main chat remains busy"
        return reply, time.monotonic() - before
    def job(identity):
        rows = store.rows("SELECT * FROM jobs WHERE id=?", (identity,))
        return rows[0] if rows else {}

    from agent.plugin_llm import PluginLlm
    with patch.object(AIAgent, "_interruptible_api_call", provider), \
         patch.object(AIAgent, "_interruptible_streaming_api_call", provider), \
         patch.object(PluginLlm, "_invoke_sync", classify):
        # Discovery may precede gateway.run's runner reference. Bootstrap must
        # survive that and start the scheduler without waiting for intake.
        del gateway_module._gateway_runner_ref
        try:
            discover_plugins(force=True)
        finally:
            gateway_module._gateway_runner_ref = weakref.ref(runner)
        manager = get_plugin_manager()
        loaded = manager._plugins["resource-harness"]
        assert loaded.enabled and not loaded.error, loaded.error
        from tools import approval
        from tools.approval_gateway_wait import _ApprovalEntry
        try:
            pulse = home / "runtime/resource-harness/manager-status.json"
            await until(pulse.exists, "early-discovered manager did not bootstrap before intake")
            reply, first_latency = await inbound("E2E_JOB_A", "m1")
            assert not reply, reply
            await until(lambda: entered["A"].is_set(), "real delegated worker A did not start")
            assert job(1)["title"] == "Research Alpha", job(1)
            reply, second_latency = await inbound("Also do independent E2E_JOB_B", "m2")
            assert not reply, reply
            await until(lambda: entered["B"].is_set(), "independent real worker B did not start")
            await inbound("Add STEER_MARKER to the first job", "m3")
            await until(lambda: bool(store.rows("SELECT * FROM steers WHERE job_id=1 AND delivered=1")), "steering never reached live child")
            reply, status_latency = await inbound("status", "m4")
            assert "Research Alpha running" in reply and "Research Beta running" in reply, reply

            # Exercise the actual approval queues while a normal turn registers and
            # unregisters its own notifier. Only custom job commands resolve these.
            assert worker_approval_keys["A"] == key + ":rh-job:1", worker_approval_keys
            assert worker_approval_keys["B"] == key + ":rh-job:2", worker_approval_keys
            entries = {}
            for identity in (1, 2):
                entry = _ApprovalEntry({"command": "fixture review", "request_id": f"fixture-{identity}"})
                entries[identity] = entry
                with approval._lock:
                    approval._gateway_queues.setdefault(key + f":rh-job:{identity}", []).append(entry)
            approval.register_gateway_notify(key, lambda data: None)
            approval.unregister_gateway_notify(key)
            assert all(not e.event.is_set() for e in entries.values()), "ordinary turn withdrew job approval"
            other = SessionSource(platform=Platform.TELEGRAM, user_id="other-user", chat_id="other-chat", chat_type="dm")
            reply, _ = await inbound("/job_approve fixture-1", "foreign-approval", source=other)
            assert "No active job" in reply and not entries[1].event.is_set(), reply
            reply, _ = await inbound("/job_approve fixture-1", "approval-1")
            assert entries[1].result == "once" and not entries[2].event.is_set(), reply
            reply, _ = await inbound("/job_deny fixture-2 fixture refusal", "deny-2")
            assert entries[2].result == "deny" and entries[2].reason == "fixture refusal", reply

            await inbound("Please cancel the second job", "m5")
            await until(lambda: job(2).get("state") == "cancelling", "natural cancellation was not routed")
            await until(lambda: interrupted["B"].is_interrupted, "cancellation did not reach the real child")
            gates["B"].set()
            await until(lambda: job(2).get("state") in {"completed", "cancelled", "failed", "interrupted"}, "real worker B did not stop")
            assert job(2)["state"] == "cancelled", job(2)
            assert job(1)["state"] == "running", "sibling cancelled accidentally"
            gates["A"].set()
            await until(lambda: job(1).get("state") == "completed", "real worker A did not complete")
            assert "STEER_MARKER" in job(1)["result"], job(1)
            await inbound("E2E_JOB_A", "m1")
            assert len(store.rows("SELECT * FROM jobs")) == 2, "duplicate message executed twice"
            attachment = home / "attachment.txt"
            attachment.write_text("fixture attachment")
            await inbound("task: Summarize attachment", "media", media_urls=[str(attachment)], media_types=["text/plain"])
            await until(lambda: job(3).get("state") == "completed", "media task did not finish in worker")
            assert json.loads(job(3)["route"])["attachments"][0]["path"] == str(attachment)
            await until(lambda: all(r["state"] == "completed" for r in store.rows("SELECT state FROM skill_reviews")),
                        "initial completed jobs were not reviewed")
            # Reuse a display title and task lineage without reusing a run ID.
            await inbound("continue Research Alpha", "resume")
            await until(lambda: job(4).get("state") == "completed", "titled continuation failed")
            assert job(4)["title"] == job(1)["title"] and job(4)["task_key"] == job(1)["task_key"]
            assert json.loads(job(4)["route"])["reference_jobs"] == [1]
            await until(lambda: store.rows("SELECT state FROM skill_reviews WHERE job_id=4")[0]["state"] == "completed",
                        "continuation review did not finish")
            # Real native review, real skill tools, durable notices; no live skills or messages.
            assert "BEFORE_MANAGER_LEARNING" in skill_file.read_text()
            await inbound("task: E2E_SKILL capture the reusable lesson", "skill-change")
            await until(lambda: job(5).get("state") == "completed", "skill fixture task did not finish")
            await until(lambda: "AFTER_MANAGER_LEARNING" in skill_file.read_text(), "native review did not update the real isolated skill")
            await until(lambda: any("fixture-learning" in row["text"] for row in captured), "skill notice did not reach captured Telegram adapter")
            notices = [r for r in captured if "fixture-learning" in r["text"]]
            assert len(notices) == 1 and notices[0]["text"].startswith(job(5)["title"] + "\n"), notices
            result_position = next(i for i, row in enumerate(captured) if row["text"].startswith(job(5)["title"] + " completed"))
            assert result_position < captured.index(notices[0]), "review notice preceded task result"
            await inbound("task: REJECT_SKILL_PATCH verify failed update", "skill-reject")
            await until(lambda: job(6).get("state") == "completed", "rejected patch task did not finish")
            await until(lambda: store.rows("SELECT state FROM skill_reviews WHERE job_id=6")[0]["state"] == "completed",
                        "rejected patch review did not settle")
            assert "reject" in review_modes and "update" in review_modes, review_modes
            assert len([r for r in captured if "fixture-learning" in r["text"]]) == 1, "failed review claimed a skill change"
            await inbound("task: CANCEL_SKILL_REVIEW check stop", "review-cancel-task")
            await until(review_entered.is_set, "cancellation review did not start")
            # Exercise the native /stop pre-command hook. Its cancellation waits
            # run outside the gateway loop while the reviewer owns its resources.
            from hermes_cli.lifecycle import invoke_hook
            before_stop = time.monotonic()
            invoke_hook("pre_command", command="stop", session_key=key, platform="telegram")
            assert time.monotonic() - before_stop < 1, "review stop blocked the gateway loop"
            await until(lambda: active_review[0].is_interrupted, "native stop did not interrupt active review")
            review_release.set()
            await until(lambda: store.rows("SELECT state FROM skill_reviews WHERE job_id=7")[0]["state"] == "completed",
                        "stopped review did not settle")
            assert len([r for r in captured if "fixture-learning" in r["text"]]) == 1, "cancelled review claimed a skill change"
            import yaml
            config = yaml.safe_load((home / "config.yaml").read_text())
            config.setdefault("auxiliary", {})["background_review"] = {"enabled": False}
            (home / "config.yaml").write_text(yaml.safe_dump(config))
            before_disabled = len(review_modes)
            await inbound("task: Review Disabled", "skill-disabled")
            await until(lambda: job(8).get("state") == "completed", "disabled-review task did not finish")
            await until(lambda: store.rows("SELECT state FROM skill_reviews WHERE job_id=8")[0]["state"] == "completed",
                        "disabled review did not settle")
            assert store.rows("SELECT result FROM skill_reviews WHERE job_id=8")[0]["result"] == "disabled"
            assert len(review_modes) == before_disabled, "disabled native review made a provider call"
            config["auxiliary"]["background_review"]["enabled"] = True
            (home / "config.yaml").write_text(yaml.safe_dump(config))
            await inbound("task: Post Stop Work", "post-stop")
            await until(lambda: job(9).get("state") == "completed", "new work after review stop did not finish")
            await until(lambda: store.rows("SELECT state FROM skill_reviews WHERE job_id=9")[0]["state"] == "completed",
                        "new review after stop did not finish")
            assert store.rows("SELECT result FROM skill_reviews WHERE job_id=9")[0]["result"] == "reviewed"
            # Stop at both startup boundaries: before parent registration and
            # after registration while admission has not returned. Neither may
            # start a review fork after shutdown or close it from a foreign thread.
            for phase in ("construction", "admission"):
                runtime = loaded.module.manager_runtime.GatewayRuntime(
                    SimpleNamespace(get_config=lambda key, default: default), runner, home, lambda: store)
                blocked, unblock = threading.Event(), threading.Event()
                original_parent = runtime.new_parent
                original_acquire = loaded.module.pool.Pool.acquire
                def held_parent(*args, **kwargs):
                    parent = original_parent(*args, **kwargs)
                    blocked.set()
                    assert unblock.wait(20), "parent construction gate timed out"
                    return parent
                def held_admission(pool, owner, *args, **kwargs):
                    if owner == "manager-skill-review-9":
                        blocked.set()
                        assert unblock.wait(20), "review admission gate timed out"
                    return original_acquire(pool, owner, *args, **kwargs)
                before_shutdown = len(review_modes)
                with patch.object(runtime, "new_parent", held_parent if phase == "construction" else original_parent), \
                     patch.object(loaded.module.pool.Pool, "acquire", held_admission if phase == "admission" else original_acquire):
                    pending_review = asyncio.create_task(runtime.review(job(9)))
                    try:
                        await until(blocked.is_set, "shutdown boundary was not reached")
                        runtime.close()
                    finally:
                        unblock.set()
                    assert await asyncio.wait_for(pending_review, 15) == "stopped"
                    assert not runtime.review_parents and len(review_modes) == before_shutdown
            await until(lambda: all(j["state"] == "sent" for j in store.rows("SELECT state FROM outbox")), "outbox did not drain")
            assert not any("Received M" in r["text"] or "I’m routing" in r["text"] for r in captured), captured
            runner._handle_message_with_agent.assert_not_awaited()
            runner._enqueue_fifo.assert_not_called()
            pool = loaded.module.pool
            await until(lambda: not any(pool.occupied(home / "runtime/resource-harness").values()), "worker leases survived cleanup")
            return {"passed": True, "real_hermes_admission_and_delegation": True,
                "provider": "deterministic fixture", "telegram_delivery": "captured adapter; no external sends",
                "early_discovery_bootstrap_before_intake": True,
                "jobs": [{"id": n, "title": job(n)["title"], "state": job(n)["state"]} for n in range(1, 10)],
                "latency_seconds": {"first": round(first_latency, 3), "while_worker_running": round(second_latency, 3), "status": round(status_latency, 3)},
                "steering_applied": True, "selected_cancellation": True, "approval_isolation": True,
                "media_delegated": True, "deduplication": True, "main_agent_calls": 0,
                "silent_intake": True, "three_word_titles": all(len(job(n)["title"].split()) <= 3 for n in range(1, 10)),
                "reused_title_and_task_lineage": True, "native_skill_review_updated_file": True,
                "skill_notice_delivered_after_result": True, "failed_patch_did_not_notify": True,
                "disabled_review_made_no_provider_call": True,
                "stop_interrupts_native_review": True, "new_work_and_review_after_stop": True,
                "shutdown_fences_review_construction_and_admission": True,
                "review_modes": review_modes,
                "remaining_leases": pool.occupied(home / "runtime/resource-harness")}
        finally:
            review_release.set()
            for gate in gates.values():
                gate.set()
            manager.unload()
            await asyncio.sleep(.3)


def main():
    source = Path(__file__).resolve().parent
    artifact = Path(sys.argv[1])
    temp = tempfile.mkdtemp(prefix="rh-manager-e2e-")
    home = Path(temp)
    try:
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_RUNTIME_DIR"] = str(home / "runtime")
        os.environ["TELEGRAM_ALLOWED_USERS"] = "*"
        shutil.copytree(source, home / "plugins/resource-harness", ignore=shutil.ignore_patterns("__pycache__", ".git"))
        import yaml
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": ["resource-harness"], "entries": {"resource-harness": {"settings": {
                "telegram_manager": True, "queue_timeout_seconds": 30}}}},
            "model": {"default": "gpt-4.1-mini", "provider": "custom", "base_url": "http://127.0.0.1:9/v1"},
            "delegation": {"max_spawn_depth": 2, "orchestrator_enabled": True, "max_iterations": 8},
            "terminal": {"backend": "local", "cwd": str(home)},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "skills": {"creation_nudge_interval": 1},
        }))
        skill = home / "skills/fixture-learning/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: fixture-learning\ndescription: Isolated manager skill-learning fixture.\n---\n"
                         "# Fixture Learning\n\nBEFORE_MANAGER_LEARNING\n")
        receipt = asyncio.run(qualify(source, home))
        artifact.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt), flush=True)
        shutil.rmtree(home)
    except BaseException:
        print("Failed E2E fixtures retained at", home, flush=True)
        raise


if __name__ == "__main__":
    main()
