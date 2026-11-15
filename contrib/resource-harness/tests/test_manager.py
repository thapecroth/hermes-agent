"""Standalone manager boundary tests; no Hermes, Telegram, or model calls."""
from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import threading
import unittest
from collections import defaultdict, deque
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("resource_harness_manager_tests", ROOT / "manager.py")
manager_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager_module)
Manager = manager_module.Manager


class ControlledRuntime:
    """Workers and routing stop at explicit gates, never timing-dependent sleeps."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.started = defaultdict(asyncio.Event)
        self.release = defaultdict(asyncio.Event)
        self.classifying = defaultdict(asyncio.Event)
        self.classification_gates = {}
        self.directives = {}
        self.classifications = []
        self.executed = []
        self.sent = []
        self.send_results = deque()
        self.cancelled = set()
        self.cancel_calls = []
        self.steer_calls = []
        self.control_started = asyncio.Event()
        self.cancel_gate = None
        self.control_timeouts = 0
        self.active = 0
        self.max_active = 0

    def block_classification(self, text, directive):
        gate = asyncio.Event()
        self.classification_gates[text] = gate
        self.directives[text] = directive
        return gate

    async def classify(self, text, jobs, reply_context=""):
        self.classifications.append((text, jobs, reply_context))
        self.classifying[text].set()
        gate = self.classification_gates.get(text)
        if gate is not None:
            await gate.wait()
        directive = self.directives.get(text, {"action": "new"})
        if isinstance(directive, Exception):
            raise directive
        return directive

    async def execute(self, job):
        self.executed.append(dict(job))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started[job["id"]].set()
        try:
            await self.release[job["id"]].wait()
            if job["id"] in self.cancelled:
                return "cancelled", "Stopped at the worker boundary."
            return "completed", f"Result for {job['goal']}"
        finally:
            self.active -= 1

    def cancel(self, job_id):
        self.cancel_calls.append(job_id)
        self.loop.call_soon_threadsafe(self.control_started.set)
        if self.cancel_gate is not None and not self.cancel_gate.wait(timeout=30):
            self.control_timeouts += 1
            return False
        self.cancelled.add(job_id)
        self.loop.call_soon_threadsafe(self.release[job_id].set)
        return True

    def steer(self, job_id, text):
        self.steer_calls.append((job_id, text))
        return True

    async def send(self, route, text, delivery_id):
        self.sent.append((route, text, delivery_id))
        result = self.send_results.popleft() if self.send_results else True
        if isinstance(result, Exception):
            raise result
        return result

    def unblock(self):
        if self.cancel_gate is not None:
            self.cancel_gate.set()
        for gate in self.classification_gates.values():
            gate.set()
        for gate in self.release.values():
            gate.set()


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.managers = []
        self.runtimes = []
        self.message_number = 0
        self.manager, self.runtime = self.make_manager()

    def make_manager(self, *, max_jobs=3, root=None):
        runtime = ControlledRuntime()
        manager = Manager(root or self.root, runtime, max_jobs=max_jobs)
        self.managers.append(manager)
        self.runtimes.append(runtime)
        return manager, runtime

    async def stop_manager(self, manager):
        manager.closed = True
        tasks = set(manager.running.values()) | set(manager.routers.values())
        tasks.update(getattr(manager, "controls", {}).values())
        delivery = getattr(manager, "delivery", None)
        if delivery is not None:
            tasks.add(delivery)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            runtime.unblock()
        for manager in self.managers:
            await self.stop_manager(manager)
        self.temp.cleanup()

    async def admit(self, text, *, scope="chat-a", message_id=None, manager=None):
        self.message_number += 1
        return await (manager or self.manager).admit(
            scope, message_id or str(self.message_number),
            {"session_key": scope, "source": {"chat_id": scope}, "message_id": str(self.message_number)}, text)

    async def new_job(self, goal, *, scope="chat-a", manager=None):
        manager = manager or self.manager
        reply = await self.admit("task: " + goal, scope=scope, manager=manager)
        self.assertIn("Queued J", reply)
        return manager.store.jobs(scope)[0]["id"]

    def job(self, job_id, *, scope="chat-a", manager=None):
        return (manager or self.manager).store.get(scope, job_id)

    async def start(self, *job_ids, manager=None, runtime=None):
        manager, runtime = manager or self.manager, runtime or self.runtime
        await manager.tick()
        for job_id in job_ids:
            await asyncio.wait_for(runtime.started[job_id].wait(), timeout=1)

    async def finish(self, job_id, *, manager=None, runtime=None):
        manager, runtime = manager or self.manager, runtime or self.runtime
        runtime.release[job_id].set()
        await asyncio.wait_for(asyncio.shield(manager.running[job_id]), timeout=1)

    async def pump_until(self, predicate, *, manager=None):
        manager = manager or self.manager
        async def pump():
            while not predicate():
                await manager.tick()
                await asyncio.sleep(0)
        await asyncio.wait_for(pump(), timeout=2)

    async def test_blocked_worker_leaves_intake_status_and_other_work_available(self):
        first = await self.new_job("Search the first topic")
        await self.start(first)
        status = await asyncio.wait_for(self.admit("status"), timeout=0.5)
        self.assertIn(f"J{first} running", status)
        second = await asyncio.wait_for(self.new_job("Search an independent topic"), timeout=0.5)
        await self.start(second)
        self.assertEqual(self.runtime.active, 2)
        self.assertFalse(self.runtime.release[first].is_set())

    async def test_status_bypasses_a_blocked_classifier(self):
        first = await self.new_job("Existing task")
        text = "Research a separate topic as well"
        gate = self.runtime.block_classification(text, {"action": "new"})
        receipt = await self.admit(text)
        self.assertIn("Received M", receipt)
        await self.start(first)
        await asyncio.wait_for(self.runtime.classifying[text].wait(), timeout=1)
        status = await asyncio.wait_for(self.admit("status"), timeout=0.5)
        self.assertIn(f"J{first} running", status)
        self.assertIn("Routing pending: M", status)
        self.assertEqual(len(self.manager.store.jobs("chat-a")), 1)
        gate.set()
        await asyncio.wait_for(asyncio.shield(self.manager.routers["chat-a"]), timeout=1)
        self.assertEqual(len(self.manager.store.jobs("chat-a")), 2)

    async def test_duplicate_platform_message_never_creates_or_executes_twice(self):
        reply = await self.admit("task: One request", message_id="platform-42")
        duplicate = await self.admit("task: One request", message_id="platform-42")
        self.assertEqual(duplicate, reply)
        job_id = self.manager.store.jobs("chat-a")[0]["id"]
        await self.start(job_id)
        self.assertEqual(await self.admit("task: One request", message_id="platform-42"), reply)
        self.assertEqual(len(self.manager.store.jobs("chat-a")), 1)
        self.assertEqual([job["id"] for job in self.runtime.executed], [job_id])

    async def test_duplicate_pending_routing_is_classified_once(self):
        await self.new_job("Existing task")
        text = "Another independent request with all its constraints"
        gate = self.runtime.block_classification(text, {"action": "new", "goal": "rewritten"})
        await self.admit(text, message_id="pending-42")
        duplicate = await self.admit(text, message_id="pending-42")
        self.assertIn("already received", duplicate)
        await self.manager.tick()
        await asyncio.wait_for(self.runtime.classifying[text].wait(), timeout=1)
        gate.set()
        await asyncio.wait_for(asyncio.shield(self.manager.routers["chat-a"]), timeout=1)
        await self.admit(text, message_id="pending-42")
        self.assertEqual(len(self.runtime.classifications), 1)
        self.assertEqual(len(self.manager.store.jobs("chat-a")), 2)
        self.assertEqual(self.manager.store.jobs("chat-a")[0]["goal"], text)

    async def test_chat_cannot_steer_or_cancel_another_chats_job(self):
        own = await self.new_job("Own task")
        other = await self.new_job("Other user's task", scope="chat-b")
        reply = await self.admit(f"cancel J{other}")
        self.assertIn("No unique active job", reply)
        reply = await self.admit(f"steer J{other}: change the scope")
        self.assertIn("No unique active job", reply)
        await self.admit("cancel all")
        self.assertEqual(self.job(own)["state"], "cancelled")
        self.assertEqual(self.job(other, scope="chat-b")["state"], "queued")
        self.assertEqual(self.manager.store.rows("SELECT * FROM steers"), [])
        await self.start(other)
        self.assertEqual(self.runtime.cancel_calls, [])

    async def test_ambiguous_cancel_preserves_both_jobs(self):
        first = await self.new_job("First task")
        second = await self.new_job("Second task")
        reply = await self.admit("cancel it")
        self.assertIn("No unique active job", reply)
        self.assertEqual([self.job(j)["state"] for j in (first, second)], ["queued", "queued"])
        self.assertEqual(self.runtime.cancel_calls, [])

    async def test_cancel_waits_for_prior_pending_request_to_be_routed(self):
        old = await self.new_job("Previous task finishing now")
        await self.start(old)
        text = "Research an independent new task"
        gate = self.runtime.block_classification(text, {"action": "new"})
        await self.admit(text)
        await self.manager.tick()
        await asyncio.wait_for(self.runtime.classifying[text].wait(), timeout=1)
        receipt = await self.admit("cancel it")
        self.assertIn("Received M", receipt)
        self.assertEqual(self.job(old)["state"], "running")
        self.assertEqual(self.runtime.cancel_calls, [])
        await self.finish(old)
        gate.set()
        await asyncio.wait_for(asyncio.shield(self.manager.routers["chat-a"]), timeout=1)
        new = self.manager.store.jobs("chat-a")[0]["id"]
        self.assertNotEqual(new, old)
        await self.pump_until(lambda: self.job(new)["state"] == "cancelled")
        self.assertEqual(self.job(old)["state"], "completed")
        self.assertNotIn(old, self.runtime.cancel_calls)
        self.assertEqual(self.runtime.cancel_calls, [new])

    async def test_finished_job_is_rechecked_after_classification(self):
        job_id = await self.new_job("Task completing during routing")
        await self.start(job_id)
        text = "Use only primary sources for that task"
        gate = self.runtime.block_classification(text, {"action": "steer", "target": job_id, "message": text})
        await self.admit(text)
        await self.manager.tick()
        await asyncio.wait_for(self.runtime.classifying[text].wait(), timeout=1)
        await self.finish(job_id)
        gate.set()
        await asyncio.wait_for(asyncio.shield(self.manager.routers["chat-a"]), timeout=1)
        saved = self.manager.store.rows("SELECT * FROM inbox WHERE text=?", (text,))[0]
        self.assertIn("No unique active job", saved["reply"])
        self.assertEqual(self.manager.store.rows("SELECT * FROM steers"), [])
        self.assertEqual(self.runtime.steer_calls, [])
        self.assertEqual(self.job(job_id)["state"], "completed")

    async def test_queued_steering_is_included_when_worker_starts(self):
        self.manager.max_jobs = 1
        first = await self.new_job("Occupy the worker")
        await self.start(first)
        queued = await self.new_job("Research the queued topic")
        await self.admit(f"steer J{queued}: Limit the result to three sources")
        pending = self.manager.store.rows("SELECT * FROM steers WHERE job_id=?", (queued,))
        self.assertEqual(pending[0]["delivered"], 0)
        self.assertFalse(self.runtime.started[queued].is_set())
        await self.finish(first)
        await self.start(queued)
        captured = next(job for job in self.runtime.executed if job["id"] == queued)
        self.assertEqual([row["text"] for row in captured["initial_steers"]], ["Limit the result to three sources"])
        self.assertEqual(self.manager.store.rows("SELECT delivered FROM steers WHERE job_id=?", (queued,))[0]["delivered"], 1)
        self.assertNotIn(queued, [job_id for job_id, _ in self.runtime.steer_calls])

    async def test_cancelling_selected_job_leaves_its_sibling_running(self):
        first = await self.new_job("Cancel this task")
        sibling = await self.new_job("Keep this independent task")
        await self.start(first, sibling)
        await self.admit(f"cancel J{first}")
        await self.pump_until(lambda: self.job(first)["state"] == "cancelled")
        self.assertEqual(self.runtime.cancel_calls, [first])
        self.assertEqual(self.job(sibling)["state"], "running")
        self.assertFalse(self.runtime.release[sibling].is_set())
        await self.finish(sibling)
        self.assertEqual(self.job(sibling)["state"], "completed")

    async def test_blocked_synchronous_cancel_does_not_block_manager_intake(self):
        job_id = await self.new_job("Task with a slow interrupt fence")
        await self.start(job_id)
        self.runtime.cancel_gate = threading.Event()
        await self.admit(f"cancel J{job_id}")
        await self.manager.tick()
        await asyncio.wait_for(self.runtime.control_started.wait(), timeout=1)
        try:
            status = await asyncio.wait_for(self.admit("status"), timeout=0.5)
            self.assertIn(f"J{job_id} cancelling", status)
            await asyncio.wait_for(self.new_job("New instruction during cancellation"), timeout=0.5)
            for _ in range(3):
                await self.manager.tick()
                await asyncio.sleep(0)
            self.assertEqual(self.runtime.cancel_calls, [job_id])
            self.assertEqual(self.runtime.control_timeouts, 0)
        finally:
            self.runtime.cancel_gate.set()
        await self.pump_until(lambda: self.job(job_id)["state"] == "cancelled")

    async def test_worker_limit_queues_excess_jobs_until_capacity_frees(self):
        self.manager.max_jobs = 2
        jobs = [await self.new_job(f"Independent task {index}") for index in range(4)]
        await self.start(*jobs[:2])
        for _ in range(3):
            await self.manager.tick()
            await asyncio.sleep(0)
        self.assertEqual([job["id"] for job in self.runtime.executed], jobs[:2])
        self.assertEqual([self.job(j)["state"] for j in jobs[2:]], ["queued", "queued"])
        await self.finish(jobs[0])
        await self.start(jobs[2])
        self.assertEqual(self.runtime.max_active, 2)
        self.assertEqual(self.job(jobs[3])["state"], "queued")

    async def test_delivery_retry_reuses_outbox_identity_without_rerunning_work(self):
        job_id = await self.new_job("Produce one result")
        await self.start(job_id)
        await asyncio.wait_for(asyncio.shield(self.manager.delivery), timeout=1)
        await self.finish(job_id)
        self.runtime.send_results.extend([False, True])
        await self.manager.deliver()
        row = self.manager.store.rows("SELECT * FROM outbox")[0]
        self.assertEqual((row["state"], row["attempts"]), ("pending", 1))
        with self.manager.store.db() as db:
            db.execute("UPDATE outbox SET next_try=0 WHERE id=?", (row["id"],))
        await self.manager.deliver()
        row = self.manager.store.rows("SELECT * FROM outbox")[0]
        self.assertEqual((row["state"], row["attempts"]), ("sent", 2))
        self.assertEqual(len(self.runtime.executed), 1)
        self.assertEqual(self.job(job_id)["state"], "completed")
        self.assertEqual(self.runtime.sent[0][2], self.runtime.sent[1][2])

    async def test_delivery_exception_preserves_result_for_retry(self):
        job_id = await self.new_job("Keep the result if Telegram fails")
        await self.start(job_id)
        await asyncio.wait_for(asyncio.shield(self.manager.delivery), timeout=1)
        await self.finish(job_id)
        self.runtime.send_results.append(RuntimeError("injected transport failure"))
        with self.assertLogs(manager_module.log, level="ERROR"):
            await self.manager.deliver()
        row = self.manager.store.rows("SELECT * FROM outbox")[0]
        self.assertEqual(row["state"], "pending")
        self.assertIn("Result for", row["text"])
        self.assertEqual(self.job(job_id)["state"], "completed")
        self.assertEqual(len(self.runtime.executed), 1)

    async def test_restart_interrupts_running_work_and_only_launches_queued_work(self):
        self.manager.max_jobs = 1
        interrupted = await self.new_job("Possibly partially executed work")
        await self.start(interrupted)
        queued = await self.new_job("Safe work that never started")
        await self.stop_manager(self.manager)
        restarted, runtime = self.make_manager(max_jobs=1, root=self.root)
        old = self.job(interrupted, manager=restarted)
        self.assertEqual(old["state"], "interrupted")
        self.assertIn("Partial work may exist", old["result"])
        await self.start(queued, manager=restarted, runtime=runtime)
        self.assertEqual([job["id"] for job in runtime.executed], [queued])
        notices = restarted.store.rows("SELECT * FROM outbox")
        self.assertTrue(any(f"J{interrupted}" in row["text"] and "restart" in row["text"] for row in notices))
        self.assertEqual(len(self.runtime.executed), 1)

    async def test_failed_initial_steering_claim_rolls_back_without_stranding_job(self):
        import sqlite3
        job_id = await self.new_job("Task awaiting initial steering")
        await self.admit(f"steer J{job_id}: preserve this constraint")
        with self.manager.store.db() as db:
            db.execute("CREATE TRIGGER fixture_failure BEFORE UPDATE ON steers BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.manager.tick()
        self.assertEqual(self.job(job_id)["state"], "queued")
        self.assertNotIn(job_id, self.manager.running)
        self.assertFalse(self.runtime.executed)
        with self.manager.store.db() as db:
            db.execute("DROP TRIGGER fixture_failure")
        await self.start(job_id)
        self.assertEqual(self.runtime.executed[0]["initial_steers"][0]["text"], "preserve this constraint")

    async def test_explicit_old_job_reference_survives_recent_history_window(self):
        import json
        first = await self.new_job("Original task with its constraints")
        await self.start(first)
        await self.finish(first)
        for index in range(6):
            await self.new_job(f"Later task {index}")
        resumed = await self.new_job(f"Continue J{first} with one more example")
        route = json.loads(self.job(resumed)["route"])
        self.assertEqual(route["reference_jobs"], [first])
        self.assertEqual(self.job(resumed)["goal"], f"Continue J{first} with one more example")

    async def test_classifier_failure_keeps_original_message_and_requests_explicit_routing(self):
        await self.new_job("Existing task")
        text = "A potentially related instruction that needs classification"
        self.runtime.directives[text] = RuntimeError("injected classifier failure")
        await self.admit(text)
        with self.assertLogs(manager_module.log, level="ERROR"):
            await self.manager.tick()
            await asyncio.wait_for(asyncio.shield(self.manager.routers["chat-a"]), timeout=1)
        saved = self.manager.store.rows("SELECT * FROM inbox WHERE text=?", (text,))[0]
        self.assertEqual(saved["text"], text)
        self.assertIn("Routing is temporarily unavailable", saved["reply"])
        self.assertEqual(len(self.manager.store.jobs("chat-a")), 1)
        self.assertEqual(self.runtime.cancel_calls, [])


if __name__ == "__main__":
    unittest.main()
