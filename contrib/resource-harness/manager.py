"""Durable Telegram inbox, job scheduler and outbox; no Hermes imports needed for tests."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "cancelling")


def task_title(value=None, goal=""):
    """A display label, never an identity or an authorization key."""
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", value if isinstance(value, str) else "", re.UNICODE)
    if not words:
        ignored = {"a", "an", "the", "please", "can", "could", "would", "you", "i", "me", "my",
                   "help", "with", "to", "for", "of", "and", "it", "that", "this", "task"}
        words = [word for word in re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", goal, re.UNICODE)
                 if word.casefold() not in ignored]
    return " ".join(word[:30] for word in words[:3]) or "New Task"


def label(job):
    return task_title(job.get("title"), job.get("goal", ""))


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        self.path = root / "manager.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, message_id TEXT NOT NULL,
                    route TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    reply TEXT, UNIQUE(scope, message_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, route TEXT NOT NULL,
                    goal TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    result TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS steers (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, text TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, route TEXT NOT NULL, text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS recoveries (
                    id TEXT PRIMARY KEY, source_pid INTEGER NOT NULL, created REAL NOT NULL,
                    reason TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recovery_jobs (
                    recovery_id TEXT NOT NULL, job_id INTEGER NOT NULL, attempt INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_reviews (
                    job_id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', result TEXT);
                CREATE TABLE IF NOT EXISTS skill_review_budget (
                    scope TEXT PRIMARY KEY, iterations INTEGER NOT NULL DEFAULT 0,
                    since_job INTEGER NOT NULL DEFAULT 0);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "attempt" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
            if "recovery_context" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN recovery_context TEXT NOT NULL DEFAULT ''")
            if "title" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            if "task_key" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN task_key TEXT NOT NULL DEFAULT ''")
            outbox_columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
            if "job_id" not in outbox_columns:
                db.execute("ALTER TABLE outbox ADD COLUMN job_id INTEGER")
            if "after_id" not in outbox_columns:
                db.execute("ALTER TABLE outbox ADD COLUMN after_id INTEGER")
            for row in db.execute("SELECT id,goal,title,task_key FROM jobs WHERE title='' OR task_key=''").fetchall():
                db.execute("UPDATE jobs SET title=?,task_key=? WHERE id=?",
                           (task_title(row["title"], row["goal"]), row["task_key"] or uuid.uuid4().hex, row["id"]))
            recovery_columns = {r[1] for r in db.execute("PRAGMA table_info(recoveries)")}
            if "target_pid" not in recovery_columns:
                db.execute("ALTER TABLE recoveries ADD COLUMN target_pid INTEGER NOT NULL DEFAULT 0")
            if "target_invocation" not in recovery_columns:
                db.execute("ALTER TABLE recoveries ADD COLUMN target_invocation TEXT NOT NULL DEFAULT ''")
            if "source_unit" not in recovery_columns:
                db.execute("ALTER TABLE recoveries ADD COLUMN source_unit TEXT NOT NULL DEFAULT ''")
            if "source_restarts" not in recovery_columns:
                db.execute("ALTER TABLE recoveries ADD COLUMN source_restarts INTEGER NOT NULL DEFAULT -1")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=0.1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def receive(self, scope, message_id, route, text):
        with self.db() as db:
            cursor = db.execute("INSERT OR IGNORE INTO inbox(scope,message_id,route,text) VALUES(?,?,?,?)",
                                (scope, str(message_id), json.dumps(route), text))
            row = dict(db.execute("SELECT * FROM inbox WHERE scope=? AND message_id=?",
                                  (scope, str(message_id))).fetchone())
            return row, bool(cursor.rowcount)

    def jobs(self, scope, *, active=False):
        return self.rows("SELECT * FROM jobs WHERE scope=? " +
                         ("AND state IN ('queued','running','cancelling') " if active else "") +
                         ("ORDER BY id DESC" if active else "ORDER BY id DESC LIMIT 30"), (scope,))

    def get(self, scope, job_id):
        rows = self.rows("SELECT * FROM jobs WHERE scope=? AND id=?", (scope, job_id))
        return rows[0] if rows else None

    def named(self, scope, title, *, active=False):
        return self.rows("SELECT * FROM jobs WHERE scope=? AND title=? COLLATE NOCASE " +
                         ("AND state IN ('queued','running','cancelling') " if active else "") +
                         "ORDER BY id DESC", (scope, title.strip()))

    def notice(self, job, text):
        with self.db() as db:
            preceding = db.execute("SELECT id FROM outbox WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["id"],)).fetchone()
            db.execute("INSERT INTO outbox(route,text,job_id,after_id) VALUES(?,?,?,?)",
                       (job["route"], f"{label(job)}\n{text}", job["id"], preceding["id"] if preceding else None))

    def skill_review_due(self, scope, job_id, iterations, interval):
        """Carry the configured skill cadence across short, independent workers."""
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO skill_review_budget(scope) VALUES(?)", (scope,))
            row = db.execute("SELECT * FROM skill_review_budget WHERE scope=?", (scope,)).fetchone()
            total = row["iterations"] + iterations
            due = total >= interval
            db.execute("UPDATE skill_review_budget SET iterations=?,since_job=? WHERE scope=?",
                       (total % interval if due else total, job_id if due else row["since_job"], scope))
            return row["since_job"] if due else None

    def finish_message(self, event, reply, *, notify=False, create=None, title=None, task_key=None, control=None):
        """The routing decision and its visible reply commit with the job/control effect."""
        with self.db() as db:
            current = db.execute("SELECT state FROM inbox WHERE id=?", (event["id"],)).fetchone()
            if current["state"] == "done":
                return
            if create is not None:
                now = time.time()
                title = task_title(title, create)
                db.execute("INSERT INTO jobs(scope,route,goal,created,updated,title,task_key) VALUES(?,?,?,?,?,?,?)",
                           (event["scope"], event["route"], create, now, now, title, task_key or uuid.uuid4().hex))
                reply = f"New session: {title}."
            if control:
                action, job_id, text = control
                for target in job_id if isinstance(job_id, list) else [job_id]:
                    if action == "cancel":
                        db.execute("UPDATE jobs SET state=CASE state WHEN 'queued' THEN 'cancelled' "
                                   "ELSE 'cancelling' END, updated=? WHERE id=? AND scope=? "
                                   "AND state IN ('queued','running','cancelling')",
                                   (time.time(), target, event["scope"]))
                    else:
                        # Preserve steering accepted before the child has registered its live identity.
                        db.execute("INSERT INTO steers(job_id,text) VALUES(?,?)", (target, text))
            db.execute("UPDATE inbox SET state='done',reply=? WHERE id=?", (reply, event["id"]))
            if notify:
                db.execute("INSERT INTO outbox(route,text) VALUES(?,?)", (event["route"], reply))

    def prepare_recovery(self, reason, source_pid, *, source_unit="", source_restarts=-1):
        ticket = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO recoveries(id,source_pid,created,reason,state,source_unit,source_restarts) "
                       "VALUES(?,?,?,?, 'prepared',?,?)",
                       (ticket, source_pid, time.time(), reason, source_unit, source_restarts))
            db.execute("INSERT INTO recovery_jobs SELECT ?,id,attempt FROM jobs WHERE state='running'", (ticket,))
        return ticket

    def recovery_state(self, ticket, state, *, target_pid=0, target_invocation=""):
        with self.db() as db:
            db.execute("UPDATE recoveries SET state=?,target_pid=?,target_invocation=? "
                       "WHERE id=? AND state IN ('prepared','issued')", (state, target_pid, target_invocation, ticket))

    def restarting(self):
        return bool(self.rows("SELECT id FROM recoveries WHERE state IN ('prepared','issued') "
                              "AND source_pid=? AND created>?", (os.getpid(), time.time() - 120)))

    def recover(self):
        """Only a fresh, issued watchdog ticket authorizes automatic continuation."""
        with self.db() as db:
            # A review might already have written a skill before a crash. Do not
            # silently replay uncertain shared-file changes on restart.
            db.execute("UPDATE skill_reviews SET state='interrupted' WHERE state='running'")
            tickets = db.execute("SELECT * FROM recoveries WHERE state='issued' AND source_pid!=? AND created>? "
                                 "AND target_pid IN (0,?) AND (target_invocation='' OR target_invocation=?)",
                                 (os.getpid(), time.time() - 600, os.getpid(), os.environ.get("INVOCATION_ID", ""))).fetchall()
            eligible = []
            for ticket in tickets:
                if ticket["source_unit"]:
                    try:
                        result = subprocess.run(["systemctl", "--user", "show", ticket["source_unit"],
                                                 "-p", "NRestarts", "-p", "MainPID", "-p", "InvocationID"],
                                                check=True, text=True, capture_output=True, timeout=5)
                        unit = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                    except (OSError, subprocess.SubprocessError):
                        log.exception("Cannot verify systemd resource recovery; withholding continuation")
                        unit = {}
                    if (int(unit.get("NRestarts", "0")) != ticket["source_restarts"] + 1
                            or unit.get("MainPID") != str(os.getpid())
                            or unit.get("InvocationID") != os.environ.get("INVOCATION_ID")):
                        db.execute("UPDATE recoveries SET state='failed' WHERE id=?", (ticket["id"],))
                        continue
                eligible.append(ticket)
            tickets = eligible
            for row in db.execute("SELECT * FROM jobs WHERE state IN ('running','cancelling')").fetchall():
                ticket = next((t for t in tickets if db.execute(
                    "SELECT 1 FROM recovery_jobs WHERE recovery_id=? AND job_id=? AND attempt=?",
                    (t["id"], row["id"], row["attempt"])).fetchone()), None)
                state = "cancelled" if row["state"] == "cancelling" else "interrupted"
                result = "Interrupted by a gateway restart. Partial work may exist; ask to resume explicitly."
                if state == "cancelled":
                    result = "Cancellation preserved across the gateway restart."
                elif ticket and row["attempt"] < 2:
                    state = "queued"
                    result = f"Hermes restarted: {ticket['reason']}. Resuming automatically with reduced parallelism."
                    context = (f"RESOURCE RECOVERY attempt {row['attempt'] + 1}: {ticket['reason']}. "
                               "The previous request exceeded resource or health limits. Use smaller search batches "
                               "(at most two research children), prefer APIs, and release browsers promptly. "
                               "Continue the original goal using the saved transcript. Verify existing files and "
                               "external effects before doing anything again; do not repeat a completed send, purchase, "
                               "deployment or mutation. If its outcome cannot be checked, ask the user. "
                               f"Previous transcript parent: rh-manager-parent-{row['id']}-a{row['attempt']}.")
                    db.execute("UPDATE jobs SET attempt=attempt+1,recovery_context=? WHERE id=?", (context, row["id"]))
                    db.execute("UPDATE steers SET delivered=0 WHERE job_id=?", (row["id"],))
                elif ticket:
                    result = "Automatic recovery limit reached after two resumes. Partial work is saved; ask to resume explicitly."
                db.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE id=?",
                           (state, result, time.time(), row["id"]))
                db.execute("INSERT INTO outbox(route,text) VALUES(?,?)",
                           (row["route"], f"{label(dict(row))}: {result}"))
            for ticket in tickets:
                db.execute("UPDATE recoveries SET state='consumed' WHERE id=?", (ticket["id"],))
            db.execute("UPDATE recoveries SET state='expired' WHERE state IN ('prepared','issued') AND created<?",
                       (time.time() - 600,))

    def finish_job(self, job, state, result):
        with self.db() as db:
            row = db.execute("SELECT state,attempt FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if row["state"] not in ("running", "cancelling") or row["attempt"] != job.get("attempt", 0):
                return
            pending = db.execute("SELECT text FROM steers WHERE job_id=? AND delivered=0 ORDER BY id",
                                 (job["id"],)).fetchall()
            if pending:
                result += "\n\nSteering arrived too late to deliver: " + "; ".join(r["text"] for r in pending)
            db.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE id=?",
                       (state, result, time.time(), job["id"]))
            db.execute("INSERT INTO outbox(route,text,job_id) VALUES(?,?,?)",
                       (job["route"], f"{label(job)} {state}\n{result}", job["id"]))
            if state == "completed":
                db.execute("INSERT OR IGNORE INTO skill_reviews(job_id) VALUES(?)", (job["id"],))


def explicit(text):
    """Only unambiguous controls bypass the model; ordinary work is never keyword-cancelled."""
    value = text.strip()
    if re.fullmatch(r"/?(?:jobs|status|list jobs|what(?:'s| is) running)[?.!]?", value, re.I):
        return {"action": "status"}
    match = re.fullmatch(r"/?(?:cancel|stop)\s+(all|(?:job\s*)?#?j?\d+|it|that|this)(?:\s+(?:jobs?|tasks?))?[.!]?", value, re.I)
    if match:
        target = match[1].lower()
        return {"action": "cancel", "target": "all" if target == "all" else
                (int(re.sub(r"\D", "", target)) if re.search(r"\d", target) else None)}
    match = re.fullmatch(r"/?(?:steer|update)\s+(?:job\s*)?#?j?(\d+)\s*[: ]\s*(.+)", value, re.I | re.S)
    if match:
        return {"action": "steer", "target": int(match[1]), "message": match[2]}
    match = re.fullmatch(r"/?(?:task|new task|another task)\s*:\s*(.+)", value, re.I | re.S)
    if match:
        return {"action": "new", "goal": match[1]}
    match = re.fullmatch(r"/?(?:steer|update)\s+([^:\n]+):\s*(.+)", value, re.I | re.S)
    if match and len(match[1].split()) <= 3:
        return {"action": "steer", "target": match[1].strip(), "message": match[2]}
    match = re.fullmatch(r"/?(?:cancel|stop|continue|resume)\s+([^:\n]+?)[.!]?", value, re.I)
    if match and len(match[1].split()) <= 3:
        action = "continue" if value.split()[0].lower().lstrip("/") in ("continue", "resume") else "cancel"
        target = match[1].strip()
        identity = re.fullmatch(r"[Jj](\d+)", target)
        return {"action": action, "target": int(identity[1]) if identity else target}
    return None


class Manager:
    def __init__(self, root, runtime, *, max_jobs=3, max_queued=50, recover=True):
        self.store = Store(root)
        self.runtime = runtime
        self.max_jobs = max(1, int(max_jobs))
        self.max_queued = max(1, int(max_queued))
        self.running = {}
        self.routers = {}
        self.controls = {}
        self.cancel_sent = set()
        self.reviewing = None
        self.closed = False
        self.recovery_pending = not recover
        if recover:
            self.store.recover()

    async def admit(self, scope, message_id, route, text):
        event, fresh = self.store.receive(scope, message_id, route, text)
        if not fresh:
            # Platform replays must not duplicate a routing receipt or create chatter.
            return ""
        directive = explicit(text)
        earlier = self.store.rows("SELECT id FROM inbox WHERE scope=? AND state='pending' AND id<? LIMIT 1",
                                  (scope, event["id"]))
        if directive and (not earlier or directive["action"] == "status"):
            await self.route(event, directive, notify=False)
            return self.store.rows("SELECT reply FROM inbox WHERE id=?", (event["id"],))[0]["reply"]
        return ""

    def targets(self, scope, target, *, active=False):
        if type(target) is int:
            job = self.store.get(scope, target)
            return [job] if job and (not active or job["state"] in ACTIVE) else []
        if isinstance(target, str):
            return self.store.named(scope, target, active=active)
        return []

    async def route(self, event, directive=None, *, notify=True):
        directive = directive or explicit(event["text"])
        if directive is None:
            try:
                directive = await self.runtime.classify(event["text"], self.store.jobs(event["scope"]),
                                                        json.loads(event["route"]).get("reply_context", ""))
                if not isinstance(directive, dict):
                    raise ValueError("manager directive must be an object")
                if directive.get("message") is not None and not isinstance(directive["message"], str):
                    raise ValueError("manager message must be text")
            except Exception:
                log.exception("Manager classification failed; preserving request for explicit routing")
                directive = {"action": "clarify", "message": "Routing is temporarily unavailable. "
                             "Use ‘task: …’, ‘cancel <title>’, ‘steer <title>: …’, or ‘status’. Your message is saved."}
        # Inference may have taken seconds; control decisions must use current job state.
        active = self.store.jobs(event["scope"], active=True)
        action = directive.get("action")
        reference = None
        if action == "continue":
            matches = self.targets(event["scope"], directive.get("target"))
            live = [job for job in matches if job["state"] in ACTIVE]
            if len(live) == 1:
                directive = {"action": "steer", "target": live[0]["id"], "message": event["text"]}
                action = "steer"
            elif not live and matches and len({job["task_key"] for job in matches}) == 1:
                reference = matches[0]
                action = "new"
            else:
                self.store.finish_message(event, "No unique task matches that title. Describe the task you mean.", notify=notify)
                return
        if action == "new":
            if len(active) >= self.max_queued + self.max_jobs:
                self.store.finish_message(event, "The job queue is full. Cancel a queued job or retry after one finishes.", notify=notify)
                return
            # Keep the full user's request. The classifier may not rewrite away constraints.
            goal = directive.get("goal") if explicit(event["text"]) else None
            refs = [int(value) for value in re.findall(r"\b[Jj](\d+)\b", event["text"])]
            if type(directive.get("target")) is int:
                refs.append(directive["target"])
            if reference:
                refs.append(reference["id"])
            refs = [value for value in dict.fromkeys(refs) if self.store.get(event["scope"], value)][:3]
            if not reference and type(directive.get("target")) is int and not explicit(event["text"]):
                candidate = self.store.get(event["scope"], directive["target"])
                if candidate and candidate["state"] not in ACTIVE:
                    reference = candidate
            if refs:
                route = json.loads(event["route"])
                route["reference_jobs"] = refs
                event = dict(event, route=json.dumps(route))
            self.store.finish_message(event, "", notify=notify, create=goal or event["text"],
                                      title=reference["title"] if reference else directive.get("title"),
                                      task_key=reference["task_key"] if reference else None)
            return
        if action == "status":
            jobs = self.store.jobs(event["scope"])
            reply = "\n".join(f"{label(j)} {j['state']}: {j['goal'][:100]}" for j in jobs[:15]) or "No jobs yet."
            self.store.finish_message(event, reply, notify=notify)
            return
        if action not in ("cancel", "steer"):
            self.store.finish_message(event, directive.get("message") or
                                      "Which task should I update? Give its title or describe it, or say ‘task: …’.", notify=notify)
            return
        target = directive.get("target")
        if target == "all" and action == "cancel":
            targets = active
        elif target is None:
            targets = active if len(active) == 1 else []
        else:
            targets = self.targets(event["scope"], target, active=True)
        if not targets or (len(targets) > 1 and target != "all"):
            self.store.finish_message(event, "No unique active job matches. Use ‘status’ and describe the task you mean.", notify=notify)
            return
        # A bulk cancellation is one durable decision, not a partially repeated inbox operation.
        if len(targets) > 1:
            reply = "Cancellation requested for " + ", ".join(label(j) for j in targets) + "."
            self.store.finish_message(event, reply, notify=notify,
                                      control=("cancel", [j["id"] for j in targets], ""))
        else:
            job = targets[0]
            message = directive.get("message") or event["text"]
            reply = (f"{label(job)} cancelled before starting." if action == "cancel" and job["state"] == "queued" else
                     f"Cancellation requested for {label(job)}; I’ll confirm when it stops." if action == "cancel" else
                     f"Existing session: {label(job)}. Instruction saved.")
            self.store.finish_message(event, reply, notify=notify, control=(action, job["id"], message))

    async def _work(self, job):
        try:
            state, result = await self.runtime.execute(job)
        except asyncio.CancelledError:
            # The thread may still be stopping. Leave the durable job in running state
            # so restart recovery never mistakes cancelling an asyncio wrapper for a join.
            raise
        except Exception as exc:
            log.exception("Manager job J%s failed", job["id"])
            state, result = "failed", f"Worker failed ({type(exc).__name__}). The request and transcript are retained."
        # A busy DB may delay persistence, but must never cause the worker to execute again.
        while True:
            try:
                self.store.finish_job(job, state, result)
                return
            except sqlite3.OperationalError:
                log.exception("Manager result persistence busy; retaining completed worker result")
                await asyncio.sleep(0.5)

    async def _route_saved(self, event):
        try:
            await self.route(event)
        except Exception:
            log.exception("Manager routing failed for M%s", event["id"])
            self.store.finish_message(event, "Routing failed. Your message is saved; use ‘task: …’ or a task title to retry.", notify=True)

    async def _review(self, job):
        try:
            result = await self.runtime.review(job)
            state = "completed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Manager skill review failed for J%s", job["id"])
            state, result = "failed", type(exc).__name__
        while True:
            try:
                with self.store.db() as db:
                    db.execute("UPDATE skill_reviews SET state=?,result=? WHERE job_id=? AND state='running'",
                               (state, result, job["id"]))
                return
            except sqlite3.OperationalError:
                await asyncio.sleep(.5)

    async def _control(self, job):
        # Interrupts can wait for a compression commit fence. They must never run on
        # the gateway loop. One control task per live job bounds thread fan-out.
        try:
            if job["state"] == "cancelling":
                if await asyncio.to_thread(self.runtime.cancel, job["id"]):
                    self.cancel_sent.add(job["id"])
                return
            for steer in self.store.rows("SELECT * FROM steers WHERE job_id=? AND delivered=0 ORDER BY id", (job["id"],)):
                if not await asyncio.to_thread(self.runtime.steer, job["id"], steer["text"]):
                    break
                with self.store.db() as db:
                    db.execute("UPDATE steers SET delivered=1 WHERE id=?", (steer["id"],))
        except Exception:
            log.exception("Manager control failed for J%s; retrying", job["id"])
            await asyncio.sleep(1)

    @staticmethod
    def _settle(task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Manager background operation failed")

    async def tick(self):
        if self.reviewing and self.reviewing.done():
            self._settle(self.reviewing)
            self.reviewing = None
        if time.time() - getattr(self, "last_heartbeat", 0) >= 10:
            pulse = self.store.path.parent / "manager-status.json"
            temporary = pulse.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": os.getpid(), "updated_at": time.time(),
                                            "running_jobs": list(self.running), "routing_chats": len(self.routers)}))
            temporary.replace(pulse)
            self.last_heartbeat = time.time()
        for job_id, task in list(self.running.items()):
            if task.done():
                self._settle(task)
                self.running.pop(job_id)
                self.cancel_sent.discard(job_id)
        for scope, task in list(self.routers.items()):
            if task.done():
                self._settle(task)
                self.routers.pop(scope)
        for job_id, task in list(self.controls.items()):
            if task.done():
                self._settle(task)
                self.controls.pop(job_id)
        for event in self.store.rows("SELECT * FROM inbox WHERE state='pending' ORDER BY id"):
            if event["scope"] not in self.routers:
                self.routers[event["scope"]] = asyncio.create_task(self._route_saved(event))
        for job in self.store.rows("SELECT * FROM jobs WHERE state IN ('running','cancelling')"):
            if job["id"] not in self.controls and job["id"] not in self.cancel_sent:
                self.controls[job["id"]] = asyncio.create_task(self._control(job))
        for job in self.store.rows("SELECT * FROM jobs WHERE state='queued' ORDER BY id"):
            recovery_running = any(j["attempt"] for j in self.store.rows("SELECT attempt FROM jobs WHERE state='running'"))
            limit = 1 if job["attempt"] or recovery_running else self.max_jobs
            if self.store.restarting() or len(self.running) >= limit:
                break
            with self.store.db() as db:
                changed = db.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='queued'",
                                     (time.time(), job["id"])).rowcount
                if changed:
                    # Claim and initial steering are atomic: a busy follow-up transaction
                    # must never strand a running row without its worker task.
                    initial = [dict(r) for r in db.execute("SELECT * FROM steers WHERE job_id=? AND delivered=0 ORDER BY id", (job["id"],))]
                    for steer in initial:
                        db.execute("UPDATE steers SET delivered=1 WHERE id=?", (steer["id"],))
            if changed:
                job["initial_steers"] = initial
                self.running[job["id"]] = asyncio.create_task(self._work(job))
        # Results are already durable. Review one job at a time, after foreground
        # work and routing, so learning never holds the main chat's turn open.
        if (self.reviewing is None and hasattr(self.runtime, "review") and not self.running and not self.routers
                and not self.store.restarting()):
            reviews = self.store.rows("SELECT j.* FROM jobs j JOIN skill_reviews r ON r.job_id=j.id "
                                      "WHERE r.state='pending' ORDER BY j.id LIMIT 1")
            if reviews:
                job = reviews[0]
                with self.store.db() as db:
                    claimed = db.execute("UPDATE skill_reviews SET state='running' WHERE job_id=? AND state='pending'",
                                         (job["id"],)).rowcount
                if claimed:
                    self.reviewing = asyncio.create_task(self._review(job))
        if not hasattr(self, "delivery") or self.delivery.done():
            if hasattr(self, "delivery"):
                self._settle(self.delivery)
            self.delivery = asyncio.create_task(self.deliver())

    async def deliver(self):
        for item in self.store.rows("SELECT o.* FROM outbox o WHERE o.state='pending' AND o.next_try<=? "
                                    "AND (o.after_id IS NULL OR EXISTS(SELECT 1 FROM outbox p WHERE p.id=o.after_id AND p.state='sent')) "
                                    "ORDER BY o.id LIMIT 5", (time.time(),)):
            try:
                sent = await asyncio.wait_for(self.runtime.send(json.loads(item["route"]), item["text"],
                                                                f"rh-manager-{item['id']}"), timeout=25)
            except Exception:
                log.exception("Manager outbox delivery failed")
                sent = False
            with self.store.db() as db:
                db.execute("UPDATE outbox SET state=?, attempts=attempts+1,next_try=? WHERE id=?",
                           ("sent" if sent else "pending", time.time() + 30, item["id"]))

    async def serve(self):
        try:
            if self.recovery_pending:
                # systemctl's accepted restart can boot faster than the independent
                # watchdog commits issuance. A prepared ticket alone is never consent.
                deadline = time.monotonic() + 120
                while self.store.rows("SELECT id FROM recoveries WHERE state='prepared' AND source_pid!=? AND created>?",
                                      (os.getpid(), time.time() - 120)) and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                while True:
                    try:
                        self.store.recover()
                        break
                    except sqlite3.OperationalError:
                        log.exception("Manager recovery busy; retrying before launching work")
                        await asyncio.sleep(.5)
                self.recovery_pending = False
            while not self.closed:
                try:
                    await self.tick()
                except Exception:
                    log.exception("Manager scheduler failed; retrying without replaying running jobs")
                    await asyncio.sleep(1)
                await asyncio.sleep(0.25)
        finally:
            self.closed = True
            tasks = [*self.running.values(), *self.routers.values(), *self.controls.values()]
            if hasattr(self, "delivery"):
                tasks.append(self.delivery)
            if self.reviewing:
                tasks.append(self.reviewing)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
