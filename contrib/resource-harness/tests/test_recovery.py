"""Recovery authorization, stale-attempt fencing, and service resource boundaries."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manager_module = load_module("resource_harness_recovery_manager_tests", "manager.py")
watchdog = load_module("resource_harness_recovery_watchdog_tests", "watchdog.py")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = manager_module.Store(self.root)
        self.message_number = 0

    def create_job(self, *, state="running", attempt=0, goal="Original request", store=None):
        store = store or self.store
        self.message_number += 1
        event, fresh = store.receive("chat-a", str(self.message_number),
                                     {"session_key": "chat-a", "source": {"chat_id": "chat-a"}}, goal)
        self.assertTrue(fresh)
        store.finish_message(event, "", create=goal)
        job = store.jobs("chat-a")[0]
        with store.db() as db:
            db.execute("UPDATE jobs SET state=?,attempt=?,result=? WHERE id=?",
                       (state, attempt, "Saved result" if state == "completed" else None, job["id"]))
        return store.get("chat-a", job["id"])

    def issue(self, *, source_pid=100, store=None):
        store = store or self.store
        ticket = store.prepare_recovery("memory pressure", source_pid)
        store.recovery_state(ticket, "issued")
        return ticket

    def get(self, job):
        return self.store.get("chat-a", job["id"])

    def test_fresh_issued_ticket_from_previous_process_authorizes_resume(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            job = self.create_job(goal="Preserve this exact task and its constraints")
            ticket = self.issue(source_pid=100)
            self.store.recover()
        recovered = self.get(job)
        self.assertEqual((recovered["state"], recovered["attempt"]), ("queued", 1))
        self.assertEqual(recovered["goal"], job["goal"])
        self.assertIn("at most two research children", recovered["recovery_context"])
        self.assertIn(f"rh-manager-parent-{job['id']}-a0", recovered["recovery_context"])
        self.assertIn("Verify existing files", recovered["recovery_context"])
        self.assertEqual(self.store.rows("SELECT state FROM recoveries WHERE id=?", (ticket,))[0]["state"], "consumed")
        notices = self.store.rows("SELECT * FROM outbox")
        self.assertEqual(len(notices), 1)
        self.assertIn("Resuming automatically", notices[0]["text"])

    def test_prepared_failed_same_process_and_expired_tickets_do_not_authorize_resume(self):
        for case in ("prepared", "failed", "same-process", "expired"):
            with self.subTest(case=case):
                store = manager_module.Store(self.root / case)
                with patch.object(manager_module.os, "getpid", return_value=200), \
                        patch.object(manager_module.time, "time", return_value=10_000):
                    job = self.create_job(store=store)
                    ticket = store.prepare_recovery("memory pressure", 200 if case == "same-process" else 100)
                    if case != "prepared":
                        store.recovery_state(ticket, "failed" if case == "failed" else "issued")
                now = 10_601 if case == "expired" else 10_001
                with patch.object(manager_module.os, "getpid", return_value=200), \
                        patch.object(manager_module.time, "time", return_value=now):
                    store.recover()
                recovered = store.get("chat-a", job["id"])
                self.assertEqual((recovered["state"], recovered["attempt"]), ("interrupted", 0))
                self.assertEqual(recovered["recovery_context"], "")
                self.assertIn("ask to resume explicitly", recovered["result"])
                if case == "expired":
                    self.assertEqual(store.rows("SELECT state FROM recoveries WHERE id=?", (ticket,))[0]["state"], "expired")

    def test_consumed_ticket_cannot_authorize_repeated_execution(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            job = self.create_job()
            ticket = self.issue()
            self.store.recover()
            self.assertEqual(self.get(job)["attempt"], 1)
            with self.store.db() as db:
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            self.store.recovery_state(ticket, "issued")
            self.store.recover()
        recovered = self.get(job)
        self.assertEqual((recovered["state"], recovered["attempt"]), ("interrupted", 1))
        self.assertEqual(self.store.rows("SELECT state FROM recoveries WHERE id=?", (ticket,))[0]["state"], "consumed")
        self.assertEqual(len(self.store.rows("SELECT * FROM outbox")), 2)

    def test_confirmed_restart_ticket_is_bound_to_replacement_process_and_invocation(self):
        for pid, invocation, expected in ((200, "new-run", "queued"), (300, "new-run", "interrupted"),
                                          (200, "later-manual-run", "interrupted")):
            with self.subTest(pid=pid, invocation=invocation):
                store = manager_module.Store(self.root / f"{pid}-{invocation}")
                job = self.create_job(store=store)
                ticket = store.prepare_recovery("memory pressure", 100)
                store.recovery_state(ticket, "issued", target_pid=200, target_invocation="new-run")
                with patch.object(manager_module.os, "getpid", return_value=pid), \
                        patch.dict(manager_module.os.environ, {"INVOCATION_ID": invocation}):
                    store.recover()
                self.assertEqual(store.get("chat-a", job["id"])["state"], expected)

    def test_resource_exit_ticket_requires_the_next_systemd_automatic_restart(self):
        for count, expected in ((0, "interrupted"), (1, "queued"), (2, "interrupted")):
            with self.subTest(automatic_restarts=count):
                store = manager_module.Store(self.root / f"restart-{count}")
                job = self.create_job(store=store)
                ticket = store.prepare_recovery("systemd resource failure: oom-kill", 100,
                                                source_unit="fixture.service", source_restarts=0)
                store.recovery_state(ticket, "issued")
                response = SimpleNamespace(stdout=f"NRestarts={count}\nMainPID=200\nInvocationID=new\n")
                with patch.object(manager_module.os, "getpid", return_value=200), \
                        patch.dict(manager_module.os.environ, {"INVOCATION_ID": "new"}), \
                        patch.object(manager_module.subprocess, "run", return_value=response):
                    store.recover()
                self.assertEqual(store.get("chat-a", job["id"])["state"], expected)

    def test_recovery_preserves_cancellation_and_leaves_completed_work_untouched(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            running = self.create_job(goal="Resume this running work")
            cancelling = self.create_job(goal="Cancellation accepted after snapshot")
            completed = self.create_job(goal="Completed after snapshot")
            cancelled = self.create_job(state="cancelled", goal="Already cancelled")
            queued = self.create_job(state="queued", goal="Never launched")
            self.issue()
            with self.store.db() as db:
                db.execute("UPDATE jobs SET state='cancelling' WHERE id=?", (cancelling["id"],))
                db.execute("UPDATE jobs SET state='completed',result='Do not replace this result' WHERE id=?", (completed["id"],))
            self.store.recover()
        self.assertEqual(self.get(running)["state"], "queued")
        stopped = self.get(cancelling)
        self.assertEqual((stopped["state"], stopped["attempt"]), ("cancelled", 0))
        self.assertIn("Cancellation preserved", stopped["result"])
        self.assertEqual(self.get(completed)["result"], "Do not replace this result")
        self.assertEqual(self.get(completed)["state"], "completed")
        self.assertEqual(self.get(cancelled)["state"], "cancelled")
        self.assertEqual((self.get(queued)["state"], self.get(queued)["attempt"]), ("queued", 0))
        notices = self.store.rows("SELECT text FROM outbox")
        self.assertEqual(len(notices), 2)
        self.assertFalse(any(f"J{completed['id']}:" in row["text"] for row in notices))

    def test_second_resume_is_allowed_but_a_third_is_not(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            job = self.create_job(attempt=1)
            self.issue()
            self.store.recover()
            second = self.get(job)
            self.assertEqual((second["state"], second["attempt"]), ("queued", 2))
            self.assertIn("RESOURCE RECOVERY attempt 2", second["recovery_context"])
            with self.store.db() as db:
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            self.issue()
            self.store.recover()
        final = self.get(job)
        self.assertEqual((final["state"], final["attempt"]), ("interrupted", 2))
        self.assertIn("recovery limit reached", final["result"])

    def test_ticket_does_not_authorize_an_attempt_it_did_not_snapshot(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            original = self.create_job()
            later = self.create_job(state="queued", goal="Launched after ticket snapshot")
            self.issue()
            with self.store.db() as db:
                db.execute("UPDATE jobs SET attempt=1 WHERE id=?", (original["id"],))
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (later["id"],))
            self.store.recover()
        self.assertEqual((self.get(original)["state"], self.get(original)["attempt"]), ("interrupted", 1))
        self.assertEqual((self.get(later)["state"], self.get(later)["attempt"]), ("interrupted", 0))

    def test_late_prior_attempt_finish_cannot_overwrite_resumed_work(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            old_attempt = self.create_job()
            self.issue()
            self.store.recover()
        with self.store.db() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (old_attempt["id"],))
        current = self.get(old_attempt)
        before = len(self.store.rows("SELECT * FROM outbox"))
        self.store.finish_job(old_attempt, "completed", "Stale worker must not win")
        self.assertEqual((self.get(old_attempt)["state"], self.get(old_attempt)["attempt"]), ("running", 1))
        self.assertNotIn("Stale worker", self.get(old_attempt)["result"])
        self.assertEqual(len(self.store.rows("SELECT * FROM outbox")), before)
        self.store.finish_job(current, "completed", "Current attempt result")
        self.assertEqual(self.get(old_attempt)["result"], "Current attempt result")
        self.assertEqual(self.get(old_attempt)["state"], "completed")
        self.assertEqual(len(self.store.rows("SELECT * FROM outbox")), before + 1)

    def test_all_steering_is_restored_in_original_order_for_recovery(self):
        with patch.object(manager_module.os, "getpid", return_value=200):
            job = self.create_job()
            instructions = ["Use primary sources", "Focus on ARM systems", "Return three choices"]
            for index, text in enumerate(instructions):
                event, _ = self.store.receive("chat-a", "steer-" + str(index), {"session_key": "chat-a"}, text)
                self.store.finish_message(event, "Steering accepted", control=("steer", job["id"], text))
            with self.store.db() as db:
                db.execute("UPDATE steers SET delivered=1 WHERE job_id=? AND id<(SELECT MAX(id) FROM steers)", (job["id"],))
            before = self.store.rows("SELECT * FROM steers WHERE job_id=? ORDER BY id", (job["id"],))
            self.assertEqual([row["delivered"] for row in before], [1, 1, 0])
            self.issue()
            self.store.recover()
        restored = self.store.rows("SELECT * FROM steers WHERE job_id=? ORDER BY id", (job["id"],))
        self.assertEqual([row["text"] for row in restored], instructions)
        self.assertEqual([row["delivered"] for row in restored], [0, 0, 0])
        self.assertEqual(self.get(job)["goal"], job["goal"])


class ResourcePressureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unit = {"ControlGroup": "/user.slice/hermes.scope", "ActiveState": "active", "MainPID": "200"}
        self.state = {"pid": 200, "platforms": {"telegram": {"state": "connected"}}}

    def write_resource_files(self, values):
        group = self.root / "user.slice/hermes.scope"
        group.mkdir(parents=True, exist_ok=True)
        for name, value in values.items():
            (group / name).write_text(str(value) + "\n", encoding="utf-8")

    def assess(self, resources, *, unit=None, uptime=1000):
        return watchdog.assess(unit or self.unit, self.state, True, False, uptime=uptime, resources=resources)

    def test_resource_usage_reads_the_services_cgroup_limits(self):
        expected = {"memory.current": 900, "memory.max": 1000, "pids.current": 11, "pids.max": 128}
        self.write_resource_files(expected)
        self.assertEqual(watchdog.resource_usage(self.unit, cgroup_root=self.root), expected)

    def test_unlimited_missing_and_malformed_values_are_not_pressure(self):
        self.write_resource_files({"memory.current": 999, "memory.max": "max", "pids.current": "invalid"})
        resources = watchdog.resource_usage(self.unit, cgroup_root=self.root)
        self.assertEqual(resources, {"memory.current": 999, "memory.max": None, "pids.current": None, "pids.max": None})
        self.assertIsNone(self.assess(resources))

    def test_invalid_cgroup_paths_do_not_escape_the_cgroup_root(self):
        for group in ("relative/path", "/user.slice/../outside", ""):
            with self.subTest(group=group):
                self.assertEqual(watchdog.resource_usage({"ControlGroup": group}, cgroup_root=self.root), {})

    def test_memory_and_pid_pressure_trigger_at_exactly_ninety_percent(self):
        for kind in ("memory", "pids"):
            for usage, pressure in ((89, False), (90, True), (91, True)):
                with self.subTest(kind=kind, usage=usage):
                    resources = {"memory.current": 0, "memory.max": 100, "pids.current": 0, "pids.max": 100}
                    resources[kind + ".current"] = usage
                    reason = self.assess(resources)
                    if pressure:
                        self.assertIn("resource pressure: " + kind, reason)
                        self.assertIn("at least 90%", reason)
                    else:
                        self.assertIsNone(reason)

    def test_healthy_high_cpu_alone_does_not_restart_the_gateway(self):
        resources = {"memory.current": 89, "memory.max": 100, "pids.current": 89, "pids.max": 100,
                     "cpu.percent": 1000, "cpu.usage_usec": 99_999_999}
        self.assertIsNone(self.assess(resources))

    def test_resource_pressure_respects_manual_stop_and_startup_grace(self):
        resources = {"memory.current": 100, "memory.max": 100, "pids.current": 100, "pids.max": 100}
        self.assertIsNone(self.assess(resources, unit={**self.unit, "ActiveState": "inactive"}))
        self.assertIsNone(self.assess(resources, uptime=179))
        self.assertIn("resource pressure", self.assess(resources, uptime=180))

    def test_restart_noop_after_manual_stop_does_not_authorize_continuation(self):
        stopped = SimpleNamespace(stdout="ActiveState=inactive\nMainPID=0\nInvocationID=\nJob=\n")
        with patch.object(watchdog.subprocess, "run", side_effect=[SimpleNamespace(), stopped]):
            with self.assertRaisesRegex(RuntimeError, "service was stopped"):
                watchdog.restart_and_confirm("fixture.service", {"MainPID": "100", "InvocationID": "old"})

    def test_restart_requires_actual_new_pid_and_invocation(self):
        before = {"MainPID": "100", "InvocationID": "old"}
        replies = [SimpleNamespace(), SimpleNamespace(stdout="ActiveState=active\nMainPID=100\nInvocationID=old\nJob=7\n"),
                   SimpleNamespace(stdout="ActiveState=active\nMainPID=200\nInvocationID=new\nJob=\n")]
        with patch.object(watchdog.subprocess, "run", side_effect=replies), patch.object(watchdog.time, "sleep"):
            result = watchdog.restart_and_confirm("fixture.service", before)
        self.assertEqual((result["MainPID"], result["InvocationID"]), ("200", "new"))

    def test_unconfirmed_restart_times_out_without_authorizing_continuation(self):
        old = SimpleNamespace(stdout="ActiveState=active\nMainPID=100\nInvocationID=old\nJob=\n")
        with patch.object(watchdog.subprocess, "run", side_effect=[SimpleNamespace(), old]):
            with self.assertRaises(TimeoutError):
                watchdog.restart_and_confirm("fixture.service", {"MainPID": "100", "InvocationID": "old"}, timeout=0)


if __name__ == "__main__":
    unittest.main()
