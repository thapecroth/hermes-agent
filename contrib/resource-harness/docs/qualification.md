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
