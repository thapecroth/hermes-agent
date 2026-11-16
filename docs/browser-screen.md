# Local rendered browser and authentication handoff

The resource harness keeps Lightpanda as the research engine and adds a rendered
Firefox lane using `invisible-playwright==0.45.0`, `invisible-core==55.50.0`,
engine `firefox-55`. This is the last verified native Linux ARM64 combination.
It runs locally on the Pi. No Vercel, Browserbase, or hosted browser is involved.
It uses Playwright's Firefox protocol; it cannot attach to the CDP backend.

The dashboard plugin adds **Bot Screen** at `/bot-screen`, using the installed
Hermes Bot Desktop and noVNC 1.7.0. This streams the actual Xfce/Firefox screen:
CSS, JavaScript, forms, dialogs and human interaction render in the same session.
Open a website, choose **Take over**, complete the interaction, then **Hand back**.
The agent continues through `interactive_browser`, which offers inspect, fill,
click, keyboard input, persistence and close. Nothing is submitted automatically
by opening a page or handing control back.

## Boundaries and lifetime

- Ten API/agent slots remain unchanged. The rendered worker consumes one of the
  same **two** browser slots, for its whole lifetime. It is exclusive to one job.
- A supervisor holds admission independently of the execution worker. If the
  worker dies, the supervisor reaps identified Firefox descendants before
  releasing its slot and removing ownership files. PID and creation time guard
  against PID reuse. Immediate reopen waits for prior cleanup under a separate
  lifetime lock. A startup timeout signals the supervisor and reports its PID if
  cleanup is still pending; it never kills the cleanup authority to free a slot.
- Native Bot Desktop's on-disk human lease fences agent input and captures.
  A changed lease epoch also discards an in-flight action's result. Lost network
  connections preserve the native human lease. **Hand back (force)** recovers it.
- The desktop is intentionally placed on the gateway. Lightpanda stays headless
  and isolated; headed mode is not enabled globally. The visible browser has a
  private persistent Firefox profile per job identity. A closed browser frees its
  slot; an idle browser closes after 30 minutes unless a human holds control.
- Browser processes launched manually outside the harness, including the user's
  pre-existing personal Chromium, are outside its two-owner resource accounting.
  Use the dashboard's browser controls rather than the desktop's Chromium dock
  launcher for bounded interactive work.
- The dashboard retains Hermes authentication and its loopback listener. Use an
  SSH tunnel to access it; this extension opens no new network listener. The local
  worker uses a private Unix socket, not CDP on a public port.

## Authentication

`browser_auth.py import-chromium --cdp http://127.0.0.1:9222 --state
<HERMES_HOME>/browser-auth/chromium-seed.json` reads existing Chromium cookies
(including HttpOnly) and local/session storage from open web pages through CDP.
It does not navigate those tabs or open the password database. State directories
are mode 0700, snapshots 0600. Values never appear in harness receipts or Git.

Lightpanda 1.0.4 isolates contexts per WebSocket connection. Production import and
export therefore run through the **existing Browser Use connection**, inside
`browser_exec`. A second CDP connection cannot seed the working session. Helpers'
navigation and tab-close calls snapshot state before leaving an origin. Storage
injection runs once per origin, before page scripts, then is removed. Logout and
later exceptions still persist. A revision check rejects stale concurrent saves.

Firefox uses its own persistent profile. Initial import uses cookies and a
temporary storage script restricted to pending top-level origins; subsequent
launches retain Firefox state. HTTP redirects import the destination's state
without consuming an unvisited source origin. A temporary session marker prevents
an immediate token refresh/reload from reapplying the old snapshot. Import markers
are excluded from saved auth. Saving a visited origin consumes its pending import,
so newer human changes and logout are not replaced on a later agent navigation.
An updated research snapshot supersedes the prior Firefox import. Both engines
use the same snapshot lock and revision check, so a stale engine cannot overwrite
a newer logout. Explicit `interactive_browser open` checkpoints and releases the
job's existing Lightpanda session before opening Firefox, just like challenge fallback.
The legacy wrapper's `storage_state()` has no localStorage support, so it is not
used for authentication transfer. Partitioned cookies are skipped rather than
flattened into another scope. The source Chromium profile remains untouched.

This transfers supported authentication, not the complete Chromium profile.
IndexedDB, passkeys, password-manager entries, device-bound tokens, closed tabs'
web storage and arbitrary live JavaScript/form state are not copied. Some sites
require a new login after changing engines. Browser-backed tests use synthetic
credentials; personal account login success requires checking the actual site.

## Challenge fallback

After a Browser Use call, the harness checks known challenge titles and CAPTCHA
frames. On detection it saves auth, closes Lightpanda, opens the URL in local
Firefox, and returns `rendered_fallback` with the next action. Further headless
calls from that job are directed to `interactive_browser`. The detector is a
heuristic and cannot identify every challenge. A CAPTCHA may still need the
human to solve it through Bot Screen. No guaranteed challenge bypass is claimed.

The transition starts a new page in another engine. It carries the URL and
supported saved auth, and does not preserve an unsaved form or in-memory DOM.

## Build, deployment and checks

From `contrib/resource-harness/dashboard`, run `npm ci` then `npm run build`.
The classic loader imports a bundled local ES module because noVNC uses top-level
await. No CDN is used; noVNC licenses ship with the assets.

Install the exact Python pins in an isolated environment and fetch the matching
sealed engine with `python -m invisible_playwright fetch`. Pass its **explicit
binary path** to `install_screen.py`: Bot Desktop's XDG environment can otherwise
select another cache and download the engine again at runtime. The wrapper still
verifies the engine seal when a binary path is supplied. Run the installer under
the activated Hermes environment with `--site-packages` and `--binary`, then
restart the gateway and dashboard after checking active work. Existing config and
plugin files are backed up privately. Export Chromium auth only on the host.
Ship the complete qualified directory, including generated JavaScript and CSS,
and compare deployed SHA256 hashes with the local checkout's manifest after
installation. The qualification receipt records those hashes.

Run the standalone suite using the repository venv:
`python -m unittest discover -s contrib/resource-harness/tests -v`.
`e2e_browser_auth.py` qualifies real Chromium to Lightpanda transfer directly.
`e2e_screen.py OUT SITE_PACKAGES` under the installed Hermes environment exercises
real dispatch, Browser Use new tabs, logout plus exception/restart, automatic
Lightpanda-to-Firefox fallback, JS forms, native takeover exclusion, admission,
persistent auth and worker-crash cleanup. Set `HERMES_INVISIBLE_BINARY` to the
pinned Firefox executable. Tests use disposable homes and local fixtures.

`e2e_dashboard_fixture.py` serves a temporary synthetic form on Pi loopback port
19120. Forward that port and dashboard port 9119 to the local machine, then run
`e2e_dashboard.mjs` with `HERMES_PLAYWRIGHT_MODULE` pointing to an existing local
Playwright `index.mjs`, and `HERMES_SCREEN_AUTH_FILE` pointing to private JSON with
dashboard `username` and `password` or an authorized `cookies` array.
`HERMES_DASHBOARD_URL` defaults to local port
19119; `HERMES_SCREEN_ARTIFACTS` selects the receipt/screenshot directory. It checks
actual noVNC typing and form submission, human fencing, handback, reconnect,
foreign-viewer eviction, fit controls, mobile overflow and browser close. Stop
the fixture and tunnel and delete the private credential file after the run.

## Qualification on November 15, 2026

The focused standalone suite passes 73 tests. The real Pi engine receipt and
dashboard receipt, plus cropped desktop/mobile screenshots, are linked in
[`browser-screen-20261115.json`](../contrib/resource-harness/docs/browser-screen-20261115.json).
The engine checks include real Browser Use auth restoration, redirects, immediate
token refresh/reload, logout, close/reopen, worker crashes, job isolation, human
fencing and the two-browser cap. Dashboard qualification types through noVNC and
submits a synthetic JavaScript form, then checks reconnect, foreign-viewer
eviction, handback, fit controls and shutdown. All auth and forms in those tests
are local synthetic fixtures.

Desktop was inspected at 1440x900 and mobile at 390x844. Mobile fit mode makes the
remote desktop small; Actual size permits panning. The page scrolls vertically to
the browser controls, with no page-level horizontal overflow. Mobile form typing
has not been qualified. The source detector scanned two files with regex rules
only; it did not evaluate rendered contrast or element sizing.

The required full fork-core suite is not green: 11,819 passed, 68 failed, 46
errors and 97 skipped. Comparing test identities against the earlier baseline
(11,818 passed, 69 failed, 46 errors) found no newly failing names; one earlier
failure did not recur. This is baseline comparison, not evidence that the unrelated
failure was fixed. Runtime E2E targets the Pi's installed Hermes APIs, which are
newer than this fork's core.
