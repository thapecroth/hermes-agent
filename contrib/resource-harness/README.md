# Hermes resource harness

An opt-in Linux plugin for research on a small host: ten API agents, two browser owners,
bounded waits, cleanup before admission, an always-available Telegram manager, and an
independent resource/health watchdog with bounded automatic continuation.

See [operation and limits](docs/operations.md) and [qualification](docs/qualification.md).
Run the complete standalone test suite with `python3 -m unittest discover -s tests -v`.
`e2e.py` also needs the installed Hermes environment and its configured local engine.
See [Lightpanda installation and performance qualification](../../docs/browser-performance.md)
for the pinned ARM64 runtime, session reuse, cleanup checks, and controlled comparisons.

See [Telegram jobs and resource recovery](docs/telegram-manager.md).

See [Bot Screen and local rendered fallback](../../docs/browser-screen.md) for
persistent auth, live forms, human takeover and the pinned ARM64 Firefox adapter.
