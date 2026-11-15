# Codon pilot on Raspberry Pi

This is an opt-in experiment, separate from the production Hermes service. See
[the measured report and blocker ledger](../../docs/codon-pilot.md).

`hermes.codon` is a native launcher **embedding CPython**. Hermes, its plugins,
async scheduling and Python dependencies execute inside CPython. It does not
compile the Hermes stack to native code. Native compilation attempts are kept
separately, with no `--auto-python` fallback.

## Reproduce

Use the installed Hermes dependency environment for runtime checks. The scripts
do not read credentials into receipts, construct Telegram adapters, or change
the production service. Real-provider checks consume the configured provider.
Run builds, tests and profiles sequentially on the Pi.

1. Obtain Codon v0.20.6's official ARM64 manylinux SDK and verify its release
   digest. Its original allocator requires a newer ARM instruction set than the
   Cortex-A72. Run `rebuild_pi_compiler.py --sdk SDK --out BUILD_RECEIPTS` on the
   Pi to produce `SDK/bin/codon-a72`; this retains the supplied libraries and
   builds the CLI without mimalloc. Allow roughly 2 GiB of compiler memory.
2. Copy this directory to a separate Pi lab directory. Run:

   ```sh
   python3 qualify.py --codon SDK/bin/codon-a72 --source HERMES_SOURCE \
     --out QUALIFICATION --python-library PYTHON_ROOT/lib/libpython3.14.so \
     --python-home PYTHON_ROOT
   python3 native_port.py --codon SDK/bin/codon-a72 \
     --source HERMES_SOURCE --view QUALIFICATION/native-source --out NATIVE_PORT
   ```

   The first command stages `.codon` copies and captures independent feature
   probes, the actual native entrypoint failure, a hybrid build and CLI checks.
   The second generates native errno constants from the target's C headers and
   attempts additional actual Hermes source units. `--resume` on `qualify.py`
   requires matching source/toolchain fingerprints and executable content.
3. Keep `runtime_compat.py` beside the generated `hermes-codon` executable. It
   serializes cold catalog builds and invalidation while preserving user-home,
   overlay and plugin semantics. It installs before worker/plugin discovery.
4. Run the launcher with:

   ```sh
   export CODON_PYTHON=PYTHON_ROOT/lib/libpython3.14.so
   export PYTHONHOME=PYTHON_ROOT
   export HERMES_CODON_SOURCE=HERMES_SOURCE
   export HERMES_DISABLE_LAZY_INSTALLS=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
   QUALIFICATION/hermes-codon --version
   QUALIFICATION/hermes-codon --run-script LAB/profile_workload.py PROFILE_OUTPUT
   ```

   Use absolute paths. `--run-script` runs the actual installed Hermes core in
   an isolated test script; it does not start the production gateway. The
   configured resource-harness plugin must already be installed.
5. Capture parent-process Python wall stacks using py-spy 0.4.3 with
   `record --rate 19 --idle --threads --format speedscope --output STACKS -- COMMAND`.
   Reduce sampling further if it reports lag. This includes waiting threads,
   not browser subprocess CPU. Render with a
   separately obtained and digest-verified official FlameGraph `flamegraph.pl`:

   ```sh
   python3 render_profile.py --stacks STACKS --receipt PROFILE_OUTPUT/profile.json \
     --flamegraph flamegraph.pl --out REPORT
   ```

## Focused checks

`test_evidence.py` uses the standard library and checks stale-evidence rejection
and interval-union accounting. `test_runtime_compat.py` uses the **actual installed**
`agent.i18n` implementation. Invoke the latter through the installed dependency
bootstrap (or from an equivalently activated Hermes environment).

`HERMES_TEST_UNPATCHED=1 ... test_runtime_compat.py LocaleTests.test_concurrent_cold_load_parses_once`
demonstrates the original three-parse failure; the normal run verifies the fix.
`benchmark_locale.py OUTPUT.json` measures real cold three-worker catalog loads;
set `HERMES_BENCH_SINGLEFLIGHT=1` for the corrected path in a fresh process.

`profile_workload.py` runs two search jobs and one real local-browser job in
parallel, twice in one process. It retains content-free spans and semantic
booleans, correlates successful tools to individual delegated jobs, blocks
messaging, and verifies worker/lease/browser cleanup. The second wave uses new
sessions; it does not measure reused-conversation prompt caching.

Keep raw profiler stacks and failed-run workdirs in the lab for diagnosis.
Only sanitized evidence and rendered graphs belong in version control.
