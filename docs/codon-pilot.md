# Codon compilation and Hermes performance on Raspberry Pi

Qualification date: November 14, 2026, America/Los_Angeles. Target: four-core
Cortex-A72, Linux ARM64, installed Hermes 0.22.2, Python 3.14.7, Codon 0.20.6.

The Codon compiler now works on this Pi, and a Codon-built hybrid launcher runs
the installed Hermes CLI. The launcher embeds CPython: Hermes and its existing
dependencies still execute as Python. This does not establish whole-stack native
compilation or a Codon performance improvement.

The experiments, fixes and reproducible commands live in
[`contrib/codon-pilot`](../contrib/codon-pilot/README.md). The production manager,
parallel-search harness and watchdog remain separate from this experiment.

## Measured bottleneck and correction

The initial representative profile completed two waves of three parallel jobs:
official Python CSV search, official SQLite transaction search, and a real local
browser reading example.com's title. First-wave elapsed time was 30.41 seconds,
with 16.59 seconds of parent-process CPU. New jobs in the warm process took
11.58 seconds and 3.52 CPU seconds. Browser subprocess CPU is excluded.

Cold `announce_api_call` paths spent 4.90 aggregate thread CPU seconds on a
localized spinner's thinking-verb lookup. The wall stacks traced the cost through
`KawaiiSpinner._catalog_thinking_verbs → t → _load_catalog → _load_bundled →
parse_locale_file → ruamel YAML safe_load`. Simultaneous cache misses parsed the
same 251,968-byte English catalog independently. Warm jobs largely eliminated
this cost. A provider retry also affected the first wave.

`runtime_compat.py` puts catalog/language/overlay builds and cache invalidation
behind one reentrant lock. It keeps the existing caches, parser, user-home keys,
overlay precedence and plugin invalidation. No catalog dictionary is reused
across different profiles. Installation is opt-in through the hybrid launcher,
before worker or plugin creation; installed core files are not patched.

An isolated real-catalog comparison in fresh CPython processes measured:

| Three simultaneous cold loads | Before | With singleflight |
| --- | ---: | ---: |
| Bundled YAML parses | 3 | 1 |
| Elapsed seconds | 4.943 | 1.897 |
| Process CPU seconds | 4.848 | 1.816 |
| Catalog entries | 3,433 | 3,433 |
| All catalogs equal | yes | yes |

The unpatched regression reproduced three parses; the corrected concurrency,
reset, direct-surface, profile-overlay and plugin tests passed. These measurements
show duplicate work removed in this workload. They are not a Codon speedup or a
prediction of end-to-end provider latency.

## Native blocker ledger

Every native build uses the repaired compiler with `-release -mcpu=cortex-a72`,
without `--auto-python`. Fixes are applied only to copied sources.

| Blocker | Work performed | Native status / remaining requirement |
| --- | --- | --- |
| Official compiler crashes with SIGILL | Traced `casal` to executable mimalloc initialization. Rebuilt CLI against matching SDK without that allocator, using ARMv8-A and the SDK's old C++ string ABI. Native version, hello and compilation work. | Fixed on Cortex-A72. Preserve matching compiler/runtime versions. Full source-build equivalent: `CODON_USE_MIMALLOC=OFF`. |
| Python module layout | Created plain `.codon` copies and one `CODON_PATH` directory; symlinks canonicalized back to `.py` and failed. | Build staging fixed. It does not make the Python language compatible. |
| Deferred annotation import | Removed `from __future__ import annotations` in staged copies while preserving line count. | Native import obstacle addressed; Codon annotations are static. |
| Missing `errno` | Generated finite native constants from the target's `<errno.h>` and compiled/run assertions. | Native shim for the constants actually needed, not a complete errno port. |
| Missing-attribute `getattr` default | Actual native errno probe rejected `getattr(errno, 'WSAEWOULDBLOCK', 10035)`. | Specialize the known Linux fallback to 10035. Arbitrary runtime attributes still need typed dispatch or Python. |
| Runtime-selected fields | Dynamic `setattr/getattr` probe failed; explicitly declared-field equivalent compiled and returned 7. | Fixed-pattern native rewrite demonstrated. Hermes's arbitrary heterogeneous state still requires a typed redesign or Python. |
| Runtime-added methods | Dynamic class registration failed; declared-method equivalent compiled and returned `local-ok`. | Fixed-pattern native rewrite demonstrated; arbitrary plugins need a typed extension protocol or Python. |
| Dynamic imports | Native importlib probe failed; explicit Python-import interoperability returned 7. Actual CLI advances from errno to missing `importlib.abc`. | Existing import hooks and Python plugins execute inside CPython. Native import/discovery lifecycle remains a port. |
| Python import-hook inheritance | Bootstrap subclasses `importlib.abc.MetaPathFinder` and `Loader`; runtime Python objects are not native Codon class definitions. | Keep the complete installer in CPython, or replace the native transport/import architecture. Import spelling alone cannot fix it. |
| Multiline class-base headers | Actual `run_agent` and `hermes_state` source units initially failed at their class declarations. Flattened their headers in staged copies, preserving bases and line count. | Syntax obstacle fixed; the agent then fails at `importlib.abc`, and the database at missing `atexit`. |
| Missing logging/network/stdlib APIs | Actual locale source fails at `logging`, database at `atexit`; SDK also lacks Python socket/selectors/importlib APIs needed by bootstrap. | Hybrid preserves them. A native implementation must preserve dual-stack connection racing, diagnostics and lifecycle semantics. |
| Environment and update bootstrap | Inspected actual getenv-race protection, at-fork behavior, source-update recovery and dependency activation. | Preserved through existing CPython bootstrap. Replacing it with a no-op would lose behavior. |
| Task-local context | Native `contextvars` probe failed. Embedded-Python propagation through `to_thread` passed. | CPython boundary fixes compatibility. Native task-local propagation must retain profile/credential isolation. |
| Worker executors | Native `asyncio.to_thread` and `run_in_executor` probes failed. | CPython path retained. Native worker-pool scheduling and context transfer remain a port. |
| Timeouts and exceptional gathering | Native `wait_for` and `gather(return_exceptions=True)` probes failed; embedded-Python cancellation, timeout, exception gathering and finally cleanup passed. | CPython path retained. Native equivalents require lifecycle tests before substitution. |
| SQLite, provider SDKs, Python extension wheels | Existing packages load through the hybrid launcher; actual runtime E2E exercises persistence, provider transport and browser tools. | They remain CPython dependencies. Native replacements need typed APIs and differential tests. |
| Embedded interpreter executable, argv, signals and exit codes | Repaired subprocess Python selection, NUL-delimited argv transfer, SIGINT handling, `SystemExit` handling and output flushing. CLI version returns 0; invalid option returns 2. | Hybrid launcher fixed. Python code remains interpreted. |
| Evidence integrity and parallel timing | Fixed resume reuse after source overwrite; fingerprint source/toolchain and executable. Stage summaries use interval unions. | Corrected; overlapping spans are not added to elapsed time. |

The unresolved native requirements above are substantial rewrites, including
Hermes's dynamic plugin and state contracts. They have operational compatibility
through CPython; they have not been turned into native Hermes implementations.
The whole-stack native goal remains unmet. The shim and syntax fixes advance
real compilation, but do not resolve the remaining library and runtime ports.

## E2E and flame graphs

The representative runtime checks use the configured real model and actual
`GatewayRuntime` delegation, not simulated tool calls. Each wave launches three
independent jobs. Hooks correlate every result with its job ancestry and require
successful tool payloads, official documentation URLs and the actual browser
title. No messaging adapter is created; messaging tools and runtime delivery are
blocked. Cleanup checks live executor threads, parent agents, leases and browser
daemons independently before deleting isolated transcripts.

The [qualification evidence](../contrib/codon-pilot/evidence/20261114/README.md)
includes the initial [baseline flame graph](../contrib/codon-pilot/evidence/20261114/baseline/wall-flamegraph.svg),
the [hybrid flame graph](../contrib/codon-pilot/evidence/20261114/hybrid/wall-flamegraph.svg),
and [hybrid stage interval unions](../contrib/codon-pilot/evidence/20261114/hybrid/stage-summary.json).
Open an SVG in a browser for hover/search/zoom. Graph widths represent
**sampled thread wall time, including waits**;
they are not CPU percentages. A single interval can appear in several threads.
Use the stage-summary interval unions for wall-time attribution and explicitly
measured thread/process CPU for CPU cost. Provider and tool intervals can also
overlap, so their totals must not be added into a response-time budget.

The stricter CPython rerun passed all six individual tool checks, as did the
hybrid launcher. The hybrid's first three jobs took 36.90 seconds and the second
three took 18.32 seconds; all workers, owned leases and browser daemons exited.
Its parent-process peak RSS was 252,228 KiB. The corresponding strict CPython
rerun took 53.49 and 21.93 seconds. **These are not comparable speed estimates:**
the 49 Hz CPython sampler reported lag, the hybrid used 19 Hz, provider latency
varied, and unrelated host work was active. Both captures had zero stack errors.
Timing starts inside the workload, after interpreter/dependency bootstrap, and
excludes browser subprocess CPU.

The existing actual-Hermes manager E2E also passed inside the compiled launcher:
independent jobs, steering, selective cancellation, approval isolation, media and
deduplication. Main-agent turns stayed at zero; captured Telegram delivery made
no external sends. The manager accepted a second instruction in 0.045 seconds
while a worker was running, and returned status in 0.073 seconds. Its provider
is a deterministic fixture; the separate research E2E uses the real provider.

Eight focused tests passed (three evidence-accounting checks and five against
the Pi's actual locale implementation). The required full fork-core suite also
ran: **11,819 passed, 68 failed, 46 errors, 97 skipped**, in 142.40 seconds.
Failures include ACP dependency/API mismatches and missing optional FastAPI in
unchanged core paths. Neither the full upstream suite nor hosted CI is claimed
green.

In the hybrid warm wave, provider transport occupied 9.82 seconds of wall-time
union and the browser tool 10.89 seconds. Search-tool union was 0.062 seconds
(process-warm tool/provider caches may contribute). Agent/API/browser admission
unions were each at most 0.103 seconds. These overlapping stages explain why
remote work and browser execution matter more than native Python compilation in
this measured warm path. The 8.83-second startup-import/discovery span is another
potential optimization target, primarily relevant to newly started processes.

The later warm path is dominated by provider transport and actual tool execution
rather than pool admission. Three jobs do not establish ten-agent saturation or
a long-duration soak. Changing a compiler cannot remove remote API latency.

## Primary references

- [Codon v0.20.6](https://github.com/exaloop/codon/releases/tag/v0.20.6)
- [Matching CLI source](https://github.com/exaloop/codon/blob/v0.20.6/codon/app/main.cpp)
- [Matching build configuration](https://github.com/exaloop/codon/blob/v0.20.6/CMakeLists.txt)
- [Mimalloc v3.6.1 architecture defaults](https://github.com/microsoft/mimalloc/blob/v3.6.1/CMakeLists.txt)
- [Codon Python interoperability](https://docs.exaloop.io/integrations/python/python-from-codon/)
- [Matching native asyncio library](https://github.com/exaloop/codon/blob/v0.20.6/stdlib/asyncio.codon)

The native failures and measured results are experimental evidence from this
specific installed stack, not an exhaustive compatibility statement about future
Codon or Hermes releases. The production service continues using its qualified
Python launcher.
