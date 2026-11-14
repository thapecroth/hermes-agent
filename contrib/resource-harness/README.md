# Hermes resource harness

An opt-in Linux plugin for research on a small host: ten API agents, two browser owners,
bounded waits, cleanup before admission, and an independent messaging-health watchdog.

See [operation and limits](docs/operations.md) and [qualification](docs/qualification.md).
Run the complete standalone test suite with `python3 -m unittest discover -s tests -v`.
`e2e.py` also needs the installed Hermes environment and Chromium.
