# Local browser performance on the Raspberry Pi

The resource harness supports the installed Hermes 0.22.2 Browser Use adapter with
Lightpanda 1.0.4. Hermes launches one local Lightpanda CDP server per browser owner;
the Browser Use driver connects directly to it. This route does not use the
agent-browser CLI or a Vercel service. Telemetry is disabled in the launcher.

## Measured results, November 15, 2026

The [sanitized qualification receipt](../contrib/resource-harness/docs/performance-20261115.json)
contains every paired trial, locale comparisons, manager/browser E2Es and the
real-provider canary's stage timings.

Three pairs alternated Chromium and Lightpanda in fresh processes. Each used two
concurrent, isolated owners, identical real `browser_exec` dispatch, a local HTTP
fixture, and the same locale/cleanup adapter. The fixture checks title, JavaScript,
dynamic fetch, form input, cookies, localStorage isolation and session reuse.
Each trial had its own cgroup with 1.5 GiB memory and two CPUs of quota. The Pi
also hosted other workloads; alternating order reduces but cannot eliminate host
load variation. These are browser-path measurements, excluding external provider/network
latency. Memory is a steady PSS snapshot taken after the timed work, not peak PSS.

| Median across three pairs | Chromium | Lightpanda |
| --- | ---: | ---: |
| First useful browser result | 14.63 s | 5.62 s |
| All browser results | 18.28 s | 7.50 s |
| Browser workload including cleanup | 21.66 s | 8.66 s |
| Controller-observed process wall time | 33.22 s | 19.81 s |
| Browser and driver PSS | 774.47 MiB | 99.85 MiB |
| Cgroup CPU during measured trial | 43.38 s | 20.38 s |

The tested browser work completed 2.44 times faster, using 87% less browser/driver
PSS and 53% less measured-trial CPU time. The CPU window starts at worker entry
and ends before receipt writing; it excludes interpreter/bootstrap imports.
Workload-plus-cleanup and CPU totals include the post-work PSS sampling overhead.
This is not an entire research-request
speedup or evidence of a Codon compilation benefit.

A separate four-owner Lightpanda run passed all semantic checks and independently
verified cleanup: 179 MiB browser/driver PSS, 11.14 seconds to all results, and
13.10 seconds including cleanup. The same process then passed cancellation of an
existing owner, a real navigation timeout, and successful session replacement.
The four-owner trial includes additional fault work in its total CPU/wall metrics;
those totals must not be compared with the latency-only two-owner trials.

Four sessions are demonstrated for this fixture. Production retains the requested
two-owner limit, ten API/agent slots and three manager jobs. A higher production
limit needs representative site/load qualification and a corresponding prompt
limit update; memory ratios alone are not a capacity guarantee.

## Session lifecycle and recovery

Each owner gets one isolated session for its turn. Use `new_tab(url)` once and
`goto_url(url)` for subsequent pages. Reuse preserves that owner's state while
preventing sharing between owners. End a browser-owning child promptly when done.
Coordinators release their browser before delegating to children.

Cleanup records authenticated driver PIDs and validated engine PIDs, including
descendants and process-start identities, before core teardown can remove its
metadata. Identities survive cleanup retries. Normal backend shutdown runs first;
remaining verified descendants are terminated and, if needed, killed. Capacity
returns only after independent process-death verification. Any teardown error
quarantines the lease and triggers the existing watchdog recovery marker. Errors
in one cleanup step do not skip the remaining driver/engine teardown steps.

Queued/reused admission checks cancellation. In-flight browser execution remains
bounded by its configured timeout; an interrupt does not guarantee immediate
termination of a blocked core CLI call. The isolated 10-second navigation timeout
test completed error handling and cleanup in 17.21 seconds. The Telegram manager
continues to handle new instructions independently of that worker.

## Locale startup work

The plugin now enables the previously qualified locale singleflight adapter by
default. All catalog loads, language discovery, overlay access and invalidation
share an RLock. Concurrent cold workers parse the same bundled catalog once;
reset cannot be overtaken by an older in-flight catalog/overlay build. Profile,
overlay and plugin-pack semantics are preserved. The Codon pilot imports this
canonical adapter rather than carrying another copy. Set the plugin setting
`locale_singleflight: false` only for an isolated baseline or rollback.

Three alternating fresh-process pairs measured a median cold locale load of
7.63 seconds before and 2.06 seconds after, with the same 3,433 catalog entries.
Bundled parse calls fell from three to one. The measured boundary is three cold
workers building a locale catalog, not all process startup work.

## Real research canary and remaining waits

The same `gpt-6.1-sol` provider ran three parallel manager jobs: two official
documentation searches and one browser title task, followed by another wave of
new sessions in the warm process. Both configurations passed all six job outcomes,
expected per-job tool semantics and cleanup with zero external messages.

| One live-provider pair | Chromium, old locale behavior | Lightpanda, locale singleflight |
| --- | ---: | ---: |
| First three jobs | 32.34 s | 21.57 s |
| Three new sessions in warm process | 13.57 s | 7.94 s |

This combines the browser and locale changes and includes variable provider and
search latency/cache states. It is a successful canary, not an isolated or repeated
estimate of whole-request speedup. In the optimized warm wave, provider transport
covered 6.46 seconds of the 7.94-second wave; browser execution covered 2.56 seconds.
These interval unions overlap and must not be added. Remaining model/network waits
will not disappear by compiling more Python.

## Pinned ARM64 installation

The official 1.0.4 ARM64 binary requires GLIBC_2.38, newer than this Pi's host
runtime. The installer verifies pinned SHA256 digests for Lightpanda and Debian's
ARM64 libc 2.41-12+deb13u4, extracts libc to a private prefix, and invokes its
loader only for Lightpanda. It does not upgrade host libraries or set a global
`LD_LIBRARY_PATH`. The loader name contains `lightpanda` for Hermes orphan reaping.

Run under the installed Hermes Python:

```sh
python contrib/resource-harness/install_lightpanda.py --prefix /absolute/private/prefix
```

Expose `PREFIX/bin/lightpanda` on the gateway PATH or at
`$HERMES_HOME/bin/lightpanda`, ahead of any other Lightpanda installation, then
configure:

```yaml
browser:
  backend: browser-use
  engine: lightpanda
  cloud_provider: local
  cdp_url: ""
  use_real_profile: false
```

Clear shared `BU_CDP_WS`, `BU_CDP_URL`, `BROWSER_CDP_URL` and
`AGENT_BROWSER_PROFILE` environment overrides as well. Keep `use_real_profile`
disabled. A saved CDP endpoint takes precedence over the engine selection and can
silently keep the old shared browser in use. The harness rejects these overrides
and shared profiles because they bypass isolated engines and capacity accounting.
On this Pi, a
systemd user-service drop-in also prioritizes `$HERMES_HOME/bin` over an existing
`/usr/local/bin/lightpanda`. Verify the actual serving process and owner-specific
engine identities, rather than relying on configuration alone.

Use the existing `browser_exec` restriction. Built-in native browser tools have a
Chrome fallback which could start another engine without another harness lease;
the Browser Use Lightpanda route returns a compatibility error instead. Lightpanda
has no graphical screenshot renderer. Site-specific compatibility is not implied
by a local fixture pass.

The separate direct `agent-browser 0.26.0 --engine lightpanda` test timed out on
open and close with the same private Lightpanda binary. That CLI combination is
not qualified; the production route uses the tested Browser Use/CDP integration.

## Reproduction

Activate the repository venv before local Python commands. Installed-stack tests
must use the installed Hermes dependency environment and its checkout on sys.path.
`--bootstrap` names a small script that activates those dependencies and executes
the next script argument. Set `XDG_RUNTIME_DIR` to the actual systemd user runtime.
The benchmark creates temporary resource-limited user units using `systemctl` so
it also works on this Pi without a session D-Bus. Every unit is stopped and its
cgroup independently checked; failed cleanup retains its handle.

```sh
python contrib/resource-harness/benchmark_browser.py --suite --bootstrap /absolute/bootstrap.py \
  --sessions 2 --trials 3 --out /absolute/evidence/paired
python contrib/resource-harness/benchmark_browser.py --contained --bootstrap /absolute/bootstrap.py \
  --engine lightpanda --sessions 4 --failure-checks --out /absolute/evidence/four.json
HERMES_E2E_BROWSER_ENGINE=lightpanda python contrib/resource-harness/e2e.py /absolute/evidence/e2e.json
python contrib/codon-pilot/test_runtime_compat.py -v
```

`profile_provider.py` is an opt-in real-provider qualification. It resolves the
current provider in memory, creates an isolated profile and session database, and
runs the fixed public-documentation workload through actual manager workers.
It constructs no messaging adapter. Run it in `run_contained()` with a sufficient
deadline. Compare equivalent models, prompts and cache states; a single provider
pair is a canary, not a controlled general performance claim.

## Deployment and validation status

The Pi's production profile was upgraded after the existing manager work drained.
The plugin and configuration were backed up before each of three Hermes restarts.
The first serving check exposed a saved CDP override, which was cleared before
the second restart. The third installed guards against shared persistent profiles.
The final PID reached Telegram connected state with a fresh
manager heartbeat. The Pi was not rebooted. The private launcher is selected at
`$HERMES_HOME/bin/lightpanda`; browser/API/agent/manager limits remain 2/10/10/3.

The final live-gateway canary completed ten real-provider children: six searches
and four successful browser title calls. The observer correlated all four browser
owners with the pinned private Lightpanda runtime and their authenticated drivers.
Observed peaks were ten agents, ten API calls, two browser owners and two
Lightpanda engines. All caller leases and captured engine/driver processes were
gone after completion, with zero external message sends. The sanitized receipt
records the durable run ID and the serving PID.

Validation includes 57 standalone harness tests, four pilot evidence tests, five
installed-core locale regressions, both engines' real dispatch/queue/delegation
E2Es, manager E2E, three paired browser trials, the four-session fault trial and
the real-provider comparison. The old fork's required full core suite was also
run: 11,818 passed, 69 failed, 97 skipped, 46 errors. Existing hosted CI was already
failing before this change; a matched baseline comparison of every failure was
not performed. Neither
the local core suite nor hosted CI should be described as green.
