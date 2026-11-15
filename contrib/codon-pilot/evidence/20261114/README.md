# Pi Codon evidence — November 14, 2026

Read [the report](../../../../docs/codon-pilot.md) for scope, caveats, blockers and
reproduction commands. All measurements come from the actual installed Pi stack.
The production service still uses its Python launcher.

| Evidence | Scope |
| --- | --- |
| [qualification.json](qualification.json) | Fourteen independent feature probes, native entrypoint diagnostics, compiled hybrid CLI and exit behavior. Six positive controls pass; eight native features fail. `whole_stack_native` stays false. Final resume reused fourteen builds and reexecuted successful binaries. |
| [native-port.json](native-port.json) | Target C-header-derived native errno shim passes. Actual CLI/agent/database/locale builds still fail. Header normalization advances agent and database compilation to missing importlib.abc / atexit. |
| [initial-attempt.json](initial-attempt.json) | Earlier native missing-attribute getattr-default failure, retained rather than replaced by a success. |
| [compiler-sigill.json](compiler-sigill.json), [compiler-rebuild.json](compiler-rebuild.json) | Original illegal instruction and successful allocator-free CLI rebuild. |
| [locale-before.json](locale-before.json), [locale-after.json](locale-after.json) | Three real cold catalog loads: three parses become one; equal catalog entries. CPython comparison, not Codon speed evidence. |
| [baseline/profile.json](baseline/profile.json), [baseline graph](baseline/wall-flamegraph.svg) | Original 49 Hz all-thread wall profile; actual searches/browser and cleanup, before stronger per-job URL checks were added. |
| [baseline-strict/profile.json](baseline-strict/profile.json), [strict baseline graph](baseline-strict/wall-flamegraph.svg) | Updated per-job semantic checks all pass. 49 Hz sampler reported lag, so timing is not a clean comparison. |
| [hybrid/profile.json](hybrid/profile.json), [hybrid graph](hybrid/wall-flamegraph.svg) | Compiled launcher with embedded CPython, locale singleflight; real provider, six assigned tool checks, independent worker/lease/browser cleanup, zero message sends. 19 Hz sampler, 15,607 samples, zero errors. |
| [hybrid-manager.json](hybrid-manager.json) | Actual Hermes admission/delegation, steering, selected cancellation, approval isolation, media, dedupe; deterministic provider and captured Telegram adapter. |
| [flamegraph-provenance.json](flamegraph-provenance.json) | Official renderer URL and SHA-256. |
| [manifest.json](manifest.json) | SHA-256 of captured receipts and graph artifacts. |
| [verification.json](verification.json) | Focused checks, E2E scope, native limits, profiler metadata, production status and full upstream-suite result. |

Focused checks additionally passed: three local evidence-accounting tests and
five tests against the installed Pi's actual locale code. An unpatched test first
reproduced three catalog parses instead of one. No full-native Hermes success is
claimed. Both research E2Es passed all per-job checks, but their elapsed times
are confounded by sampler rate/lag, provider variation and unrelated host load.
The full fork-core suite was run before publication: 11,819 passed, 68 failed,
46 errors, 97 skipped, 200 warnings in 142.40 seconds. ACP dependency/API mismatch
and missing optional FastAPI are among the failures; full-suite success is not
claimed. This pilot does not modify those core paths.

Each graph is interactive SVG. Width is sampled **thread wall time including
waits**, not CPU utilization. Stage summaries use unions within a stage; even
those stage unions overlap each other. Do not add them into total latency.

Raw speedscope files and folded stacks remain in the task's local
`hermes-profiling-20261114` directory and Pi lab. Receipts retain booleans, timing
and internal session identifiers; prompts, API credentials and transcripts are
not published.
