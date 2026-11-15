# Bounded research on a Raspberry Pi

This opt-in Linux plugin uses Hermes's existing execution middleware and session-end hook.
It does not patch the installed Hermes checkout. The integration adapter targets Hermes 0.22.2
and browser-harness's identity-checked shutdown API; rerun the E2E after upgrading either.

The default profile has ten delegated-agent slots, ten shared model-request/search slots,
and two whole browser-session slots. File locks share admission across both threads and
Hermes processes using that profile. Separate profiles have separate budgets; the service's
memory and CPU limits cover all descendants of this gateway.

An agent takes a browser slot on its first browser call and keeps it until its turn ends.
Requested session names are normalized to one isolated session per agent turn. Two browser
owners can work concurrently; further owners wait without blocking the gateway event loop.
API/search work has a separate budget. Releasing a slot closes both its browser-harness
daemon and the managed browser, including a hybrid local sidecar. Failed cleanup retains
the slot and signals the independent watchdog; it cannot admit a replacement over a browser
that may still be alive. A browser tool error also initiates cleanup.

The harness controls Hermes tool dispatch, not arbitrary OS commands: an agent using
terminal/execute_code to start its own browser bypasses this policy. A frozen worker can
retain a slot until restart. Browser sessions do not survive turns or gateway restarts.
The API budget covers the main turn API path, not auxiliary/background provider calls.
The agent budget begins at a delegated child's first model request, not object construction.

## Research instructions

The plugin registers [research_prompt.md](../research_prompt.md) as a system prompt section.
It defaults to search first and parallel delegation for independent questions, API-based
retrieval, concise sourced findings, and prompt synthesis after asynchronous completion.
It preserves the resource limits and the delegation tool's delivery contract.

After editing that file, restart the gateway. Hermes freezes plugin prompt sections in
each session's cached system prompt, including across restarts: the change applies to
new sessions. In an existing Telegram chat, finish current work and use `/new` once to
start a session with the new policy. Restarting alone does not rewrite cached conversations.

## Configuration

Keep `delegation.max_concurrent_children: 10`, `max_spawn_depth: 1`, and
`child_timeout_seconds: 600` in `config.yaml`. The latter is an inactivity timeout.
Use `browser.backend: browser-use` with the managed local browser. The plugin rejects
legacy `browser_*` tools rather than admitting a second unaccounted browser path.

Enable `resource-harness` in `plugins.enabled`. Optional settings:

```yaml
plugins:
  entries:
    resource-harness:
      settings:
        agents: 10
        api: 10
        browsers: 2
        queue_timeout_seconds: 600
        browser_timeout_seconds: 120
```

Restart all profile processes after changing capacities. Existing lock files must stay in
place: deleting a live lock file would create a second independent admission pool.

Install this directory as `$HERMES_HOME/plugins/resource-harness`. Install the two watchdog
units under the systemd user unit directory, and the resource drop-in under
`hermes-gateway.service.d/resource-limits.conf`. The supplied units target the default profile;
adapt the home path when installing for another profile. Reload the user manager, enable
the timer, and restart the gateway once. The drop-in sets MemoryHigh=4G, MemoryMax=5G,
TasksMax=1024 and CPUQuota=300% for the four-core Pi. These are protection ceilings, not
a guarantee that every ten-agent workload will fit.

For a first installation, run `python install.py` from this directory using the installed
Hermes Python environment, with its checkout on `sys.path` and access to the systemd user
bus. The installer backs up configuration, checks the config writer's roundtrip, enables
the timer and requests a gateway restart. It refuses to replace an existing installation.
For an update, review changes and back up the installed plugin before replacing its files;
restart the gateway after the update and repeat qualification.

The watchdog checks every 45 seconds. It preserves a manually stopped gateway, gives startup
three minutes, and requires two consecutive unhealthy observations. It restarts on a fatal
platform state, failed HTTP health, stale process identity, or failed browser cleanup.
It allows at most three restarts per half-hour, spaced at least five minutes apart.
The service restart can interrupt active work; save research findings incrementally.

## Inspection and rollback

Read `gateway_state.json`, `runtime/resource-harness/watchdog.json`, and the systemd user
service/timer states. Lock-file metadata contains process IDs, internal session/turn
identities and timestamps; occupancy must be checked with the lock, not file contents.

To roll back, disable the watchdog timer, remove the plugin from `plugins.enabled`, remove
the resource drop-in, reload the user manager, and restart Hermes. Keep the configuration
backup from installation. Do not remove lock files while profile processes are running.
