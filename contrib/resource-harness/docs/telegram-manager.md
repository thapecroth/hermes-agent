# Telegram manager and automatic recovery

The opt-in manager targets the primary profile's Telegram direct messages on Hermes
0.22.2. Enable `telegram_manager: true` in the resource-harness plugin settings.
Keep `delegation.max_spawn_depth: 2` and `orchestrator_enabled: true`: the manager
creates one background coordinator per request, and that coordinator can delegate
independent research to leaf agents. Other platforms and multiplex profiles retain
the ordinary gateway behavior.

The main chat saves each admitted message and releases its active-turn slot immediately.
The scheduler also bootstraps when plugin discovery precedes gateway initialization;
recovery and pending notifications do not need another incoming Telegram message.
New work starts in a delegated worker. A small routing model distinguishes another
independent task from steering, cancellation, or status; ambiguous controls ask for a
job number. Deterministic controls and status avoid model latency. Three top-level jobs
run together by default, leaving capacity for research children inside the shared
10-agent, 10-API/search-call, and two-browser budgets. Excess jobs queue durably; the
per-chat queue is bounded to 50 waiting jobs plus active capacity. Attachment paths,
images, and replied-to message text are passed to workers too.

Examples:

- Send an ordinary request: it becomes `J1`, `J2`, and so on.
- `task: compare these alternatives` or `/task compare these alternatives`: explicitly start another job.
- `steer J1: focus on local options` or `/job_steer J1: focus on local options`: steer a selected job.
- `cancel J2` or `/cancel J2`: cancel only that job. Confirmation follows actual worker termination.
- `cancel all`, `/cancel all`, or native `/stop`: request cancellation of this chat's active jobs.
- `status` or `/jobs`: get saved job states while workers or the classifier are busy.
- `continue J1`: start a new job with J1's saved goal/result attached as context.

Workers have separate consent queues. Approval prompts name both the job and the
specific request: `/job_approve J1 <request ID>` grants that operation once;
`/job_deny J1 <request ID> [reason]` denies it. A request from another chat cannot
resolve the approval. Ordinary `/approve` and `/deny` retain the core chat behavior.
Finishing a normal gateway turn cannot withdraw another job's approval requests.
Never use an unqualified natural-language acknowledgement as execution consent.

The SQLite inbox, jobs, steering, restart tickets, and notification outbox live in
`$HERMES_HOME/runtime/resource-harness/manager.sqlite3` (directory 0700, DB 0600).
Repeated platform message IDs do not execute a task twice. Routing instructions stay
ordered within a chat, while status can bypass slow routing. Results and their outbox
entry commit together; failed sends retry independently. A crash after Telegram accepted
a send but before local acknowledgement can duplicate a notification. It never replays
that notification's underlying task. Plugin slash dispatch lacks the platform message
ID on this runtime, so slash-created tasks do not have the ordinary inbox's platform
replay protection. Prefer ordinary `task: ...` messages when that matters.

## Resource recovery

The independent systemd watchdog runs every 45 seconds. After the three-minute startup
grace, two unhealthy samples trigger a restart. It reads this gateway's actual cgroup:
90% of `memory.max` or `pids.max` is pressure, even if HTTP health still passes. A fatal
adapter, failed health check, stale process identity, or failed browser cleanup also
qualifies. Busy CPU alone is expected work and does not trigger a restart. The service
has 4 GiB memory high, 5 GiB memory maximum, 1,024 tasks, and a 300% CPU quota.

Before requesting a restart, the watchdog transactionally snapshots running job attempt
IDs in a recovery ticket and pauses new launches. Only a fresh **issued** ticket from a
different process authorizes continuation. The replacement waits briefly for a restart
issuance that races startup, then consumes the ticket, increments attempt counters,
queues affected jobs and user notices in one transaction. A failed/prepared/expired
restart request does not authorize replay. Systemd's `ExecStopPost` also records a ticket
for an observed `oom-kill`, watchdog, or resources failure before `Restart=always` boots
the replacement. For a watchdog-requested restart, issuance requires an observed new PID
and systemd invocation; the ticket is bound to that replacement. A successful no-op
`try-restart` after a manual stop cannot authorize replay. Confirmation has a 105-second
ceiling; an unconfirmed restart leaves work interrupted instead of replaying it.
A successful manual stop/restart does not create such a ticket.
Resource-exit tickets additionally require the next systemd automatic restart counter
and the current PID/invocation to match; an explicit start cannot consume them. A
successful stop invalidates outstanding issued tickets while preserving a watchdog's
prepared restart request.

Recovered jobs run one at a time, with their original goal, steering, and bounded saved
transcript progress. The worker is explicitly told the previous attempt exceeded resource
or health limits, to use smaller search batches, and to inspect existing effects before
continuing. A fresh coordinator cannot restore an in-flight call or unsaved browser state.
If a previous external action's outcome cannot be checked, it must ask the user before
repeating it. This is continuation from saved evidence, not an exactly-once guarantee for
external systems. Cancelling jobs stay cancelled and completed jobs stay completed.
Late results from an older attempt cannot overwrite the replacement attempt.

Each job gets at most two automatic resumes. The watchdog also permits at most three
restarts per half-hour, separated by five minutes. Exhausted jobs become interrupted
with a saved explanation; explicit resumption is required. Without an eligible ticket,
a gateway restart marks running work interrupted instead of replaying it.

## Qualification

Run the standalone suite with the repository's Python environment:

```sh
python -m unittest discover -s contrib/resource-harness/tests -v
```

Run `e2e_manager.py <receipt.json>` using the installed Hermes Python and checkout.
It exercises real gateway admission/plugin discovery, real AIAgent/delegation, steering,
cancellation, job-specific consent, media intake, deduplication, and resource cleanup.
Provider responses are deterministic fixtures and Telegram sends are captured locally.
`e2e_watchdog.py <receipt.json>` uses a disposable real systemd service and a deliberately
low test pressure threshold to prove restart, warning delivery, continuation, and an
unrepeated side effect without exhausting memory or restarting the actual gateway.
`e2e.py` separately qualifies actual HTTP concurrency and local Chromium lifecycle.

After deployment, verify the real service's startup manager task, connected adapters,
HTTP health, watchdog timer, effective cgroup limits, and pool cleanup. These captured
Telegram tests do not prove delivery through Telegram's external network.
