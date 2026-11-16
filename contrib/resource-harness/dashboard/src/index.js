import RFB from '@novnc/novnc';

const sdk = window.__HERMES_PLUGIN_SDK__;
const {React} = sdk;
const {useEffect, useRef, useState} = sdk.hooks;
const {Button, Input} = sdk.components;
const h = React.createElement;

class ScreenConnection {
  constructor(changed) {
    this.changed = changed;
    this.pending = new Map();
    this.sequence = 0;
  }
  async connect() {
    this.socket = new WebSocket(await sdk.buildWsUrl('/api/ws'));
    await new Promise((resolve, reject) => {
      this.socket.onopen = resolve;
      this.socket.onerror = () => reject(new Error('Could not connect. Reconnect to try again.'));
    });
    this.socket.onmessage = event => {
      for (const line of event.data.split('\n').filter(Boolean)) {
        const reply = JSON.parse(line);
        if (reply.id && this.pending.has(reply.id)) {
          const {resolve, reject, timer} = this.pending.get(reply.id);
          this.pending.delete(reply.id);
          clearTimeout(timer);
          reply.error ? reject(new Error(reply.error.message)) : resolve(reply.result);
        } else if (reply.method === 'display.lease' || reply.method === 'display.status') {
          this.changed();
        }
      }
    };
    this.socket.onclose = () => {
      for (const {reject, timer} of this.pending.values()) {
        clearTimeout(timer);
        reject(new Error('Connection lost. Reconnect to continue.'));
      }
      this.pending.clear();
      this.changed(true);
    };
  }
  call(method, params = {}) {
    if (this.socket?.readyState !== WebSocket.OPEN) return Promise.reject(new Error('Reconnect to the screen first.'));
    const id = `screen-${++this.sequence}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('The screen did not respond. Reconnect to check its state.'));
      }, 45000);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\n');
    });
  }
  close() { this.socket?.close(); }
}

function Screen() {
  const [status, setStatus] = useState(null);
  const [browser, setBrowser] = useState(null);
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [url, setUrl] = useState('');
  const [fit, setFit] = useState(true);
  const canvas = useRef(null);
  const client = useRef(null);
  const rfb = useRef(null);
  const viewer = useRef('');
  const viewerHash = useRef('');
  const leaseEpoch = useRef(-1);
  const active = useRef(true);
  const mine = useRef(false);

  const reconcile = st => {
    if ((st.lease?.epoch ?? 0) < leaseEpoch.current) return;
    leaseEpoch.current = st.lease?.epoch ?? 0;
    mine.current = !!viewerHash.current && st.lease?.holder === 'human' && st.lease.viewer_hash === viewerHash.current;
    if (rfb.current) rfb.current.viewOnly = !mine.current;
    setStatus(st);
  };
  const refresh = async () => {
    const connection = client.current;
    const st = await connection.call('display.status');
    if (!active.current || client.current !== connection) return;
    reconcile(st);
    const br = await sdk.fetchJSON('/api/plugins/resource-harness/browser');
    if (active.current && client.current === connection) setBrowser(br);
  };
  const attach = async () => {
    rfb.current?.disconnect();
    const observed = await client.current.call('display.observe', viewer.current ? {viewer_id: viewer.current} : {});
    viewer.current = observed.viewer_id;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(viewer.current));
    viewerHash.current = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('').slice(0, 12);
    reconcile(observed);
    const stream = new URL(await sdk.buildWsUrl(observed.path));
    // Native Bot Screen uses its own one-use ticket, not the generic RPC ticket.
    stream.search = '';
    stream.searchParams.set('display_ticket', observed.ticket);
    const display = new RFB(canvas.current, stream.href);
    rfb.current = display;
    display.scaleViewport = fit;
    display.resizeSession = false;
    display.viewOnly = !mine.current;
    display.addEventListener('connect', () => active.current && rfb.current === display && setConnected(true));
    display.addEventListener('disconnect', () => {
      if (active.current && rfb.current === display) setConnected(false);
    });
    display.addEventListener('securityfailure', () => setError('Screen access expired. Reconnect to continue.'));
  };
  const connect = async () => {
    mine.current = false; viewer.current = ''; viewerHash.current = ''; leaseEpoch.current = -1;
    if (rfb.current) { rfb.current.viewOnly = true; rfb.current.disconnect(); }
    setConnected(false);
    client.current?.close();
    const connection = new ScreenConnection(lost => {
      if (!active.current || client.current !== connection) return;
      if (lost) {
        mine.current = false;
        if (rfb.current) rfb.current.viewOnly = true;
        setConnected(false);
      }
      else refresh().catch(() => {});
    });
    client.current = connection;
    await connection.connect();
    await refresh();
    const st = await client.current.call('display.status');
    if (st.running) await attach();
  };
  const run = async (name, operation) => {
    setBusy(name); setError('');
    try { await operation(); }
    catch (failure) { setError(failure.message || 'The screen action failed. Reconnect to check its state.'); }
    finally { if (active.current) setBusy(''); }
  };
  useEffect(() => {
    active.current = true;
    run('Connecting', connect);
    const timer = setInterval(() => {
      if (client.current?.socket?.readyState === WebSocket.OPEN) refresh().catch(() => {});
    }, 5000);
    return () => {
      active.current = false;
      clearInterval(timer);
      // Explicit handback for a clean unmount; a network loss keeps the native fence.
      if (mine.current) client.current?.call('display.lease.release', {viewer_id: viewer.current}).catch(() => {});
      rfb.current?.disconnect();
      client.current?.close();
    };
  }, []);
  useEffect(() => { if (rfb.current) rfb.current.scaleViewport = fit; }, [fit]);

  const human = status?.lease?.holder === 'human';
  const disabled = !!busy;
  const nativeCall = async method => { await client.current.call(method); await refresh(); };
  return h('div', {className: 'rh-screen'},
    h('header', {className: 'rh-screen-header'},
      h('div', null, h('h1', null, 'Bot Screen'),
        h('p', null, 'Watch the live browser. Take over to sign in or complete a form, then hand control back.')),
      h('span', {className: 'rh-screen-state', role: 'status'},
        busy || (human ? mine.current ? 'You are in control' : 'Human control is protected' : connected ? 'Agent is in control' : status?.running ? 'Screen disconnected' : 'Screen is off'))),
    h('div', {className: 'rh-screen-toolbar'},
      !status?.running && h(Button, {disabled, onClick: () => run('Starting screen', async () => {
        await nativeCall('display.start'); await attach();
      })}, 'Start screen'),
      status?.running && !human && h(Button, {disabled: disabled || !connected, onClick: () => run('Taking over', async () => {
        await client.current.call('display.lease.acquire', {viewer_id: viewer.current, reason: 'Dashboard takeover'});
        await refresh(); if (mine.current) rfb.current?.focus();
      })}, 'Take over'),
      human && h(Button, {disabled, onClick: () => run('Handing back', async () => {
        await client.current.call('display.lease.release', mine.current ? {viewer_id: viewer.current} : {force: true});
        mine.current = false; await refresh(); if (!connected) await attach();
      })}, mine.current ? 'Hand back' : 'Hand back (force)'),
      h(Button, {disabled, variant: 'outline', onClick: () => run('Reconnecting', connect)}, 'Reconnect'),
      status?.running && h(Button, {disabled: disabled || human || browser?.running, variant: 'outline', onClick: () => run('Stopping screen', async () => {
        await nativeCall('display.stop'); rfb.current?.disconnect(); setConnected(false);
      })}, 'Stop screen'),
      h(Button, {disabled: disabled || !status?.running, variant: 'outline', onClick: () => setFit(!fit)}, fit ? 'Actual size' : 'Fit screen'),
      h(Button, {disabled: disabled || !mine.current || !connected, variant: 'outline', onClick: () => {rfb.current.sendKey(0xffe3, 'ControlLeft', true); rfb.current.sendKey(0x006c, 'KeyL'); rfb.current.sendKey(0xffe3, 'ControlLeft', false); rfb.current.focus();}}, 'Address bar')),
    error && h('div', {className: 'rh-screen-error', role: 'alert'}, error),
    status?.blocker && h('p', {role: 'status'}, status.blocker),
    h('div', {className: 'rh-screen-stage', 'data-human': human, 'aria-label': 'Live browser desktop'},
      h('div', {ref: canvas, className: 'rh-screen-canvas'}),
      !connected && h('div', {className: 'rh-screen-placeholder'},
        h('h2', null, status?.running ? 'Connect to the live screen' : 'Your browser workspace'),
        h('p', null, status?.running ? 'Reconnect to see the current page. A lost connection keeps human control protected.' : 'Start the screen, then open a website below. Pages render with their styles, JavaScript, and forms.'))),
    h('form', {className: 'rh-screen-open', onSubmit: event => {
      event.preventDefault();
      run('Opening browser', async () => {
        const result = await sdk.fetchJSON('/api/plugins/resource-harness/browser/open', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
        if (result.error) throw new Error(result.error === 'human_has_control' ? 'Hand back before opening another page.' : `Browser could not open (${result.error}).`);
        await refresh(); if (!connected) await attach();
      });
    }},
      h('label', {htmlFor: 'rh-browser-url'}, 'Website'),
      h(Input, {id: 'rh-browser-url', type: 'url', required: true, value: url, placeholder: 'https://example.com', onChange: e => setUrl(e.target.value), disabled: disabled || human}),
      h(Button, {type: 'submit', disabled: disabled || human || !url}, 'Open browser')),
    h('footer', {className: 'rh-screen-footer'},
      h('p', null, browser?.running ? 'Local Firefox · persistent session · one of two browser slots' : 'Lightpanda handles research. A rendered browser uses one of the two shared browser slots.'),
      h(Button, {variant: 'outline', disabled: disabled || human || !browser?.running, onClick: () => run('Closing browser', async () => {
        const result = await sdk.fetchJSON('/api/plugins/resource-harness/browser/close', {method: 'POST'});
        if (result.error) throw new Error(result.error); await refresh();
      })}, 'Close browser')));
}

window.__HERMES_PLUGINS__.register('resource-harness', Screen);
