// Run locally through an SSH tunnel. HERMES_SCREEN_AUTH_FILE is private JSON
// {username,password} or {cookies:[...]}; values never enter test output.
// HERMES_PLAYWRIGHT_MODULE points to an existing Playwright installation.
import assert from 'node:assert/strict';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';

const {chromium} = await import(pathToFileURL(process.env.HERMES_PLAYWRIGHT_MODULE).href);
const auth = JSON.parse(await readFile(process.env.HERMES_SCREEN_AUTH_FILE, 'utf8'));
const base = process.env.HERMES_DASHBOARD_URL || 'http://127.0.0.1:19119';
const fixture = 'http://127.0.0.1:19120';
const output = process.env.HERMES_SCREEN_ARTIFACTS || '/tmp/hermes-screen-dashboard';
await mkdir(output, {recursive: true});
const receipt = {passed: false, fixture_only: true};
const browser = await chromium.launch({headless: true, channel: process.env.HERMES_SCREEN_BROWSER_CHANNEL || undefined});
const context = await browser.newContext({viewport: {width: 1440, height: 900}});
if (auth.cookies) await context.addCookies(auth.cookies.map(cookie => ({url: base, ...cookie})));
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));

async function waitFor(check, message, timeout = 60000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(message);
}
async function idle(p = page) {
  await waitFor(async () => await p.getByRole('button', {name: 'Reconnect', exact: true}).isEnabled(), 'Screen action did not settle');
}
async function click(name, p = page) {
  await p.getByRole('button', {name, exact: true}).click();
  await idle(p);
  assert.equal(await p.locator('.rh-screen-error').count(), 0, 'Dashboard reported an action error');
}
async function fixturePost(path) {
  const response = await fetch(fixture + path, {method: 'POST'});
  assert.equal(response.status, 200);
  return response.json();
}
async function connected(p = page) {
  await waitFor(async () => await p.getByRole('button', {name: 'Take over', exact: true}).isEnabled(), 'noVNC did not connect');
}
async function screenshot(name) {
  // The dashboard sidebar may contain unrelated conversations. Capture only
  // this feature; viewport sizing is verified separately below.
  await page.locator('.rh-screen').screenshot({path: `${output}/${name}.png`});
}

try {
  await page.goto(base + '/bot-screen');
  if (await page.locator('input[name=password]').count()) {
    await page.locator('input[name=username]').fill(auth.username);
    await page.locator('input[name=password]').fill(auth.password);
    await page.locator('button[type=submit]').click();
  }
  await page.locator('.rh-screen').waitFor({timeout: 60000});
  await idle();
  await screenshot('desktop-before');
  if (await page.getByRole('button', {name: 'Start screen', exact: true}).count()) await click('Start screen');
  await connected();
  assert((await fixturePost('/prepare')).success, 'Could not prepare credential-free QA browser');
  await page.getByLabel('Website', {exact: true}).fill(fixture);
  await click('Open browser');
  await connected();
  assert((await fixturePost('/focus')).success);
  await click('Take over');
  assert.equal(await page.getByRole('button', {name: 'Address bar', exact: true}).isEnabled(), true);
  assert.equal((await fixturePost('/fence')).error, 'human_has_control');
  receipt.human_fence = true;
  await page.keyboard.type('Dashboard visitor', {delay: 70});
  await page.keyboard.press('Tab');
  await page.keyboard.press('Enter');
  await waitFor(async () => (await (await fetch(fixture + '/result')).json()).submissions.includes('Dashboard visitor'), 'Human noVNC input did not submit the form');
  receipt.live_form_input_and_javascript = true;
  await click('Address bar');
  // Wait for the remote browser's chrome to receive Ctrl+L, then type at a
  // human pace. A websocket write alone does not acknowledge remote focus.
  await page.waitForTimeout(250);
  const addressPath = `/address-bar-${Date.now()}`;
  await page.keyboard.type(fixture + addressPath, {delay: 70});
  await page.keyboard.press('Enter');
  await waitFor(async () => (await (await fetch(fixture + '/result')).json()).visits.includes(addressPath), 'Address bar did not navigate to the complete local URL');
  await click('Hand back');
  await connected();
  receipt.address_bar_and_handback = true;
  await click('Actual size');
  await click('Fit screen');
  receipt.fit_cycle = true;
  await click('Take over');
  await click('Reconnect');
  await waitFor(async () => await page.getByRole('button', {name: 'Hand back (force)', exact: true}).count(), 'Reconnect retained stale ownership');
  assert.equal(await page.getByRole('button', {name: 'Address bar', exact: true}).isDisabled(), true);
  assert.equal((await fixturePost('/fence')).error, 'human_has_control');
  await click('Hand back (force)');
  await connected();
  receipt.reconnect_preserves_fence_and_requires_force = true;
  await click('Take over');
  const other = await context.newPage();
  await other.goto(base + '/bot-screen');
  await other.locator('.rh-screen').waitFor();
  await idle(other);
  await click('Hand back (force)', other);
  await connected();
  assert.equal(await page.getByRole('button', {name: 'Address bar', exact: true}).isDisabled(), true);
  receipt.foreign_viewer_eviction_disables_input = true;
  await other.close();
  await screenshot('desktop');
  const desktopBox = await page.locator('.rh-screen').boundingBox();
  assert(desktopBox.width <= 1440);
  await page.setViewportSize({width: 390, height: 844});
  await screenshot('mobile');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  assert.equal(overflow, false, 'Page overflows on mobile');
  receipt.mobile_no_horizontal_overflow = true;
  await page.setViewportSize({width: 1440, height: 900});
  await click('Close browser');
  await waitFor(async () => (await context.request.get(base + '/api/plugins/resource-harness/browser')).json().then(s => !s.running), 'Browser did not stop');
  receipt.close_and_release = true;
  await click('Stop screen');
  await page.getByRole('button', {name: 'Start screen', exact: true}).waitFor();
  receipt.stop_screen = true;
  assert.deepEqual(errors, [], 'Browser JavaScript errors');
  receipt.passed = true;
} finally {
  await context.close();
  await browser.close();
  assert.equal(browser.isConnected(), false);
  receipt.local_browser_disconnected = true;
  await writeFile(`${output}/result.json`, JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify(receipt));
}
