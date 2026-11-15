# Qualification

Run the complete standalone suite from this directory:

```sh
python3 -m unittest discover -s tests -v
```

`e2e.py` runs under the **installed** Hermes Python environment with the installed checkout
on `sys.path`. It creates a temporary Hermes home and loads this plugin through real
plugin discovery. It drives the actual middleware and registered browser tool, ten
overlapping HTTP calls to an inspectable local fixture, and four real Chromium browser
jobs competing for two session slots. It checks that API work finishes while extra
browser jobs remain queued, then checks lock occupancy, daemon liveness and managed
browser tracking after all jobs complete. The receipt explicitly records that its API
fixture is not a real model-provider call.

```sh
python e2e.py /path/to/receipt.json
```

The live gateway canary is a separate test: submit a research batch through its authenticated
API and retain the durable run ID, model/tool events and pool occupancy peaks. This tests
real provider calls and web-search tools. Verify Telegram polling health before and after;
do not claim that API traffic alone proves Telegram message delivery.

The tests do not launch an arbitrary load or inject failures into a user's Telegram
connection. Process-crash, cancellation, capacity exhaustion, downstream exceptions,
failed cleanup and manual-stop behavior are covered with isolated deterministic cases.

## Raspberry Pi receipt, November 14, 2026

The full standalone suite passed eight tests. The isolated real-Hermes E2E passed with
ten overlapping HTTP requests, four real Chromium jobs, peak API occupancy ten, peak
browser occupancy two, peak managed browsers two, and no remaining leases or harness
daemons. Cancellation was also exercised through the real middleware, including its
fail-open exception behavior.

The systemd E2E restarted a disposable service after two injected fatal Telegram status
observations, then verified that an intentional manual stop was preserved and the fixture
process was gone. No fault was injected into the real Telegram connection.

After installation, live run `run_9664ddaf88ed49eab02694273dcd37a5` dispatched ten agents
in one background batch. Its durable completion delivery reported all ten completed in
39.47 seconds: six real web API searches and four browser jobs returning `Example Domain`.
The late-started sampler observed browser occupancy two; its agent/API peaks are partial
observations and are not used as proof of ten simultaneous provider requests. The ten-slot
concurrency proof comes from the separately controlled HTTP E2E.

After the live run, all resource pools were empty, the gateway had no automatic restarts,
all three platform states were connected, and the watchdog had no strikes or recovery
actions. Kernel cgroup files confirmed the configured memory, CPU and process limits.
This is a bounded functional qualification, not a long-duration load test or OOM test.

The fork's broader core suite was also attempted with `python -m pytest tests/ -q -n 0`
in an isolated environment containing its declared dev and ACP extras. Collection stopped
in two existing ACP tests: the adapter imports `SetSessionModelResponse`, which is absent
from the resolved `agent-client-protocol==0.12.1`. The full core suite did not pass. No core
files were changed; the installed 0.22.2 plugin boundary was qualified by the separate E2E.

## Search-first policy

After installing `research_prompt.md`, the real plugin prompt renderer and session-freezing
path returned its full 3,120 characters without truncation. All eight standalone tests still
passed. A new authenticated gateway session received an ordinary request covering CSV,
JSON, SQLite and HTTP documentation, with no instruction to delegate. Hermes loaded its
required skill, then made `delegate_task` its first research action with all four topics
in one batch. Durable completion reported four completed children and no failed children.
See the [sanitized behavioral receipt](search-first-qualification-20261114.json).

The final-answer check continued the same API session through its native `/chat` route
and received a synthesis with four official documentation links. This continuation is
explicit: the API adapter persists asynchronous delegation results without automatically
starting a wake turn; its consumer owns that next turn. No Telegram test message was sent.

The real-Hermes/Chromium E2E was rerun after the prompt update: ten HTTP requests overlapped,
four real browser jobs completed with peak browser owners and managed browsers two, and
all leases and harness daemons were gone afterward.

This checks one real routing decision; it is not a benchmark claiming a fixed latency
improvement. Existing conversations retain their frozen prompts and need `/new` to adopt
the updated policy.

## Telegram manager and resource continuation

The expanded standalone suite passed 47 tests. The actual Hermes admission/delegation
E2E passed with captured Telegram delivery: an early-discovered plugin bootstrapped its
scheduler before any message, independent jobs ran together, steering reached the live
child, selected cancellation left its sibling running, consent remained job/request/chat
scoped, media reached workers, duplicate intake did not replay jobs, and all leases cleared.
The fixture's latest receipt latencies were 137 ms initially, 28 ms with another worker
running, and 33 ms for status. They exclude Telegram's external network and are not a
production latency promise. The main conversation made zero agent calls.

A separate configured-provider canary using `gpt-6.1-sol` correctly classified new work,
steering and selected cancellation, then completed a real delegated worker. It isolated
its transcript and sent no external messages.

The real systemd E2E used a disposable unit and actual cgroup readings with a deliberately
low test threshold. Two pressure observations replaced the process, resumed saved work,
delivered both warnings to local sinks, and left the completed side effect count at one.
Restart issuance was tied to the observed replacement PID/invocation; a successful
`try-restart` no-op after a manual stop was rejected. A second phase injected the resource
failure result label into `ExecStopPost`, then verified a real `Restart=on-failure`
transition and automatic continuation. A later successful stop invalidated an outstanding
resource ticket, and a manual start did not replay it. No actual OOM was induced. Fixture
process and temporary unit cleanup were independently verified.

The browser/API E2E still measured ten overlapping HTTP calls and at most two browser
owners/managed Chromium processes. Four ordinary browser jobs and two browser-owning
coordinators with browser children completed without deadlock or remaining leases/daemons.
See the [sanitized combined receipt](manager-qualification-20261114.json).

The broader upstream suite was attempted again but did not pass: before stopping the
resource-heavy run it reported 11,180 passed, 66 failed, 32 errors and 97 skipped. The
resolved environment includes existing ACP import incompatibility and missing optional
FastAPI dependencies, alongside other upstream failures. The focused contribution checks
and installed-runtime E2Es passed; neither the full suite nor hosted CI is claimed green.

The final plugin was deployed to the Pi after an idle drain, with a private rollback
backup. Live qualification verified an advancing manager heartbeat belonging to the
new gateway PID, connected Telegram/API/Photon adapters, healthy HTTP, an active watchdog
timer, effective `OOMPolicy=kill`, 5 GiB memory maximum and 1,024 tasks, the 10/10/2 budgets,
three top-level manager jobs, delegation depth two, and empty resource pools. Browser
configuration remains local. No unsolicited Telegram test message was sent; external
Telegram message delivery remains unqualified by these captured-adapter tests.
