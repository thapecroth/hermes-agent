"""Private, local CDP authentication snapshots for isolated browser sessions.

Cookie values and web storage never enter a tool result. Chromium is only read
when importing; its windows, profile and password database are left untouched.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.parse import urlsplit

import aiohttp


class ProtocolError(RuntimeError):
    pass


def loopback_endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "ws"} or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parts.port or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("Authentication transfer requires a local loopback CDP endpoint")
    return value


class CDP:
    """One bounded local connection. Commands are intentionally serialized."""
    def __init__(self, endpoint: str, timeout=15):
        self.endpoint = loopback_endpoint(endpoint)
        self.timeout = timeout
        self.sequence = 0

    async def __aenter__(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            endpoint = self.endpoint
            if endpoint.startswith("http:"):
                async with self.http.get(endpoint.rstrip("/") + "/json/version", allow_redirects=False) as response:
                    response.raise_for_status()
                    endpoint = loopback_endpoint((await response.json())["webSocketDebuggerUrl"])
                    if urlsplit(endpoint).port != urlsplit(self.endpoint).port:
                        raise ValueError("CDP discovery changed the local port")
            self.ws = await self.http.ws_connect(endpoint, max_msg_size=16 * 1024 * 1024)
            return self
        except BaseException:
            await self.http.close()
            raise

    async def __aexit__(self, *_):
        await self.http.close()

    async def call(self, method, params=None, session=None):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        async with asyncio.timeout(self.timeout):
            await self.ws.send_json(message)
            while True:
                reply = await self.ws.receive_json()
                if reply.get("id") != self.sequence:
                    continue
                if "error" in reply:
                    # Parameters/replies can contain credentials. Never include them.
                    raise ProtocolError(f"{method} failed (code {reply['error'].get('code', 'unknown')})")
                return reply.get("result", {})

    async def pages(self):
        return [t for t in (await self.call("Target.getTargets")).get("targetInfos", []) if t.get("type") == "page"]

    async def attach(self, target):
        return (await self.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]

    async def evaluate(self, expression, session):
        result = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
        if result.get("exceptionDetails"):
            raise ProtocolError("Browser storage evaluation failed")
        return result.get("result", {}).get("value")

    async def cookies(self):
        try:
            return (await self.call("Storage.getCookies")).get("cookies", [])
        except ProtocolError:
            return (await self.call("Network.getAllCookies")).get("cookies", [])


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError("Authentication directory must belong to this user")
    path.chmod(0o700)


def write_private(path: Path, payload):
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_private(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16 * 1024 * 1024:
            raise ValueError("Authentication snapshot must be private and bounded")
        data = json.load(stream)
    if data.get("version") != 1 or not isinstance(data.get("cookies"), list) or not isinstance(data.get("origins"), list):
        raise ValueError("Invalid authentication snapshot")
    return data


STORAGE_EXPRESSION = """(() => {
  if (!/^https?:$/.test(location.protocol)) return null;
  const entries = s => Object.keys(s).filter(name => !name.startsWith('__hermes_auth_import_'))
    .map(name => ({name, value:s.getItem(name)}));
  return {origin:location.origin, localStorage:entries(localStorage), sessionStorage:entries(sessionStorage)};
})()"""


async def snapshot(connection: CDP, previous=None):
    origins = {o["origin"]: o for o in (previous or {}).get("origins", [])}
    unreadable = 0
    for page in await connection.pages():
        session = await connection.attach(page["targetId"])
        try:
            storage = await connection.evaluate(STORAGE_EXPRESSION, session)
            if storage:
                origins[storage["origin"]] = storage
        except (ProtocolError, TimeoutError):
            unreadable += 1
        finally:
            await connection.call("Target.detachFromTarget", {"sessionId": session})
    return {"version": 1, "saved_at": time.time(), "cookies": await connection.cookies(),
            "origins": list(origins.values()), "unreadable_pages": unreadable}


def cookie_parameters(cookie, *, lightpanda=True):
    if lightpanda and ("partitionKey" in cookie or cookie.get("partitionKeyOpaque")):
        return None  # LP 1.0.4 ignores partitionKey. Never flatten a partitioned cookie.
    if cookie.get("expires", -1) > 0 and cookie["expires"] <= time.time():
        return None
    keys = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")
    result = {key: cookie[key] for key in keys if key in cookie}
    if not cookie.get("session", False) and cookie.get("expires", -1) > 0:
        result["expires"] = cookie["expires"]
    if not lightpanda:
        for key in ("partitionKey", "priority", "sourceScheme", "sourcePort"):
            if key in cookie:
                result[key] = cookie[key]
    return result


def storage_script(origins):
    # Seed only missing keys: refreshed in-session tokens must win over the seed.
    return """(() => { const all = %s; const row = all.find(x => x.origin === location.origin);
      if (!row) return;
      for (const [kind, store] of [['localStorage',localStorage],['sessionStorage',sessionStorage]]) {
        for (const item of (row[kind] || [])) if (store.getItem(item.name) === null) store.setItem(item.name,item.value);
      }
    })()""" % json.dumps(origins, ensure_ascii=True)


async def restore(connection: CDP, state, *, lightpanda=True, inject_storage=True):
    cookies = [p for c in state["cookies"] if (p := cookie_parameters(c, lightpanda=lightpanda)) is not None]
    pages = await connection.pages()
    if not pages:
        target = await connection.call("Target.createTarget", {"url": "about:blank"})
        pages = [{"targetId": target["targetId"]}]
    # LP 1.0.4 creates its connection-local context on the first target.
    if cookies:
        await connection.call("Storage.setCookies", {"cookies": cookies})
    script = storage_script(state["origins"])
    for page in pages if inject_storage else []:
        session = await connection.attach(page["targetId"])
        try:
            await connection.call("Page.addScriptToEvaluateOnNewDocument", {"source": script}, session)
            await connection.evaluate(script, session)
        finally:
            await connection.call("Target.detachFromTarget", {"sessionId": session})
    observed = await connection.cookies()
    def identity(c):
        return c.get("name"), c.get("domain", ""), c.get("path", "/"), json.dumps(c.get("partitionKey"), sort_keys=True)
    actual = {identity(c): c for c in observed}
    verified = sum(all(actual.get(identity(c), {}).get(k) == c.get(k)
                       for k in ("value", "secure", "httpOnly", "sameSite") if k in c) for c in cookies)
    return {"requested_cookies": len(cookies), "verified_cookies": verified,
            "skipped_partitioned": sum("partitionKey" in c or bool(c.get("partitionKeyOpaque")) for c in state["cookies"]) if lightpanda else 0,
            "storage_origins": len(state["origins"]), "complete": verified == len(cookies)}


def state_path(home: Path, identity: str):
    return home / "browser-auth" / "sessions" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


@contextmanager
def state_lock(path):
    private_directory(path.parent)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def require_revision(previous, revision):
    if previous and revision != previous.get("saved_at"):
        raise ProtocolError("Authentication snapshot changed in another owner; refusing stale save")


def owner_marker(home, owner):
    return Path(home) / "runtime/resource-harness/auth" / (hashlib.sha256(owner.encode()).hexdigest()[:32] + ".ready")


class ExecutorCDP(CDP):
    """Use the Browser Use daemon's actual connection, especially for Lightpanda.

    LP 1.0.4 isolates contexts PER WEBSOCKET. A new observer connection cannot
    import into or export from the agent's page, even on the very same port.
    """
    def __init__(self, call):
        self.native_call = call

    async def call(self, method, params=None, session=None):
        return self.native_call(method, session_id=session, **(params or {}))


def executor_auth(cdp, *, home, identity, owner, action):
    """Called inside browser_exec; returns counts only, never credentials."""
    home = Path(home)
    path = state_path(home, identity)
    marker = owner_marker(home, owner)
    private_directory(marker.parent)
    with state_lock(path):
        async def run():
            connection = ExecutorCDP(cdp)
            if action == "restore":
                if marker.exists():
                    return {"already_restored": True}
                source = path if path.exists() else home / "browser-auth/chromium-seed.json"
                if not source.exists():
                    return {"seed_available": False}
                state = read_private(source)
                receipt = await restore(connection, state, inject_storage=False)
                if not receipt["complete"]:
                    raise ProtocolError("Authentication cookie import could not be verified")
                if not path.exists():
                    write_private(path, state)
                write_private(marker, {"revision": state.get("saved_at"), "consumed": []})
                return receipt
            if action != "save":
                raise ValueError("Unknown authentication action")
            previous = read_private(path) if path.exists() else None
            tracking = json.loads(marker.read_text()) if marker.exists() else {}
            require_revision(previous, tracking.get("revision"))
            state = await snapshot(connection, previous)
            write_private(path, state)
            tracking["revision"] = state["saved_at"]
            write_private(marker, tracking)
            return {"saved": True, "cookies": len(state["cookies"]), "storage_origins": len(state["origins"])}
        return asyncio.run(run())


def forget_owner(home, owner):
    marker = owner_marker(home, owner)
    marker.unlink(missing_ok=True)


def wrap_code(code, *, home, identity, owner):
    """Wrap code without indenting it (future imports and tool globals survive)."""
    values = {"home": str(home), "identity": identity, "owner": owner}
    module_dir = str(Path(__file__).resolve().parent)
    return ("import sys as _rh_sys\n"
            f"_rh_sys.path.insert(0, {module_dir!r})\n"
            "from browser_auth import executor_session as _rh_session\n"
            f"with _rh_session(globals(), **{values!r}):\n"
            f"    exec(compile({code!r}, '<browser_exec>', 'exec'), globals(), globals())\n")


class executor_session:
    """Seed each origin once, on the owning Browser Use connection.

    Intercept navigation at the CDP boundary, including new_tab's internal
    calls. Save before leaving/closing pages so logout cannot resurrect tokens.
    Injection is removed after navigation, rather than surviving future logouts.
    """
    def __init__(self, namespace, **values):
        self.namespace, self.values = namespace, values
        self.home = Path(values["home"])
        self.path = state_path(self.home, values["identity"])
        self.marker = owner_marker(self.home, values["owner"])

    def __enter__(self):
        import browser_harness.helpers as helpers
        self.helpers, self.original = helpers, helpers.cdp
        executor_auth(self.original, **self.values, action="restore")
        self.tracking = json.loads(self.marker.read_text()) if self.marker.exists() else {"consumed": []}
        helpers.cdp = self.namespace["cdp"] = self.call
        return self

    def call(self, method, session_id=None, **params):
        source = None
        if method in {"Page.navigate", "Target.closeTarget"}:
            executor_auth(self.original, **self.values, action="save")
        if method == "Page.navigate" and self.path.exists():
            url = urlsplit(params.get("url", ""))
            origin = f"{url.scheme}://{url.netloc}"
            if url.scheme in {"http", "https"} and origin not in self.tracking["consumed"]:
                state = read_private(self.path)
                rows = [r for r in state["origins"] if r["origin"] == origin]
                if rows:
                    source = self.original("Page.addScriptToEvaluateOnNewDocument", session_id=session_id,
                                           source=storage_script(rows)).get("identifier")
        try:
            result = self.original(method, session_id=session_id, **params)
            if source:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    probe = self.original("Runtime.evaluate", session_id=session_id,
                        expression="JSON.stringify({origin:location.origin,ready:document.readyState})", returnByValue=True)
                    value = json.loads(probe.get("result", {}).get("value") or "{}")
                    if value.get("origin") == origin and value.get("ready") in {"interactive", "complete"}:
                        self.tracking["consumed"].append(origin)
                        tracking = json.loads(self.marker.read_text())
                        tracking["consumed"] = self.tracking["consumed"]
                        write_private(self.marker, tracking)
                        break
                    time.sleep(.05)
                else:
                    raise ProtocolError("Authentication storage navigation did not complete")
            return result
        finally:
            if source:
                self.original("Page.removeScriptToEvaluateOnNewDocument", session_id=session_id, identifier=source)

    def __exit__(self, error_type, error, traceback):
        self.helpers.cdp = self.namespace["cdp"] = self.original
        try:
            executor_auth(self.original, **self.values, action="save")
            probe = self.original("Runtime.evaluate", expression="""JSON.stringify({url:location.href,
              challenge:/(just a moment|attention required|access denied|verify you are human|security check)/i.test(document.title)
                || !!document.querySelector('iframe[src*="captcha"],iframe[src*="challenges.cloudflare.com"]')})""", returnByValue=True)
            value = json.loads(probe.get("result", {}).get("value") or "{}")
            if value.get("challenge") and urlsplit(value.get("url", "")).scheme in {"http", "https"}:
                write_private(self.marker.with_suffix(".challenge"), {"url": value["url"]})
        except Exception:
            if error is None:
                raise
            # Preserve the user's original failure. No credential values escape.
        return False


async def import_chromium(endpoint, destination):
    async with CDP(endpoint, timeout=5) as connection:
        state = await snapshot(connection)
    write_private(destination, state)
    return {"saved": True, "cookies": len(state["cookies"]), "storage_origins": len(state["origins"]),
            "unreadable_pages": state["unreadable_pages"], "local_only": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("import-chromium", "restore", "save"):
        command = commands.add_parser(action)
        command.add_argument("--cdp", required=True)
        command.add_argument("--state", required=True, type=Path)
        if action == "restore":
            command.add_argument("--chromium", action="store_true")
    args = parser.parse_args()

    async def run():
        if args.action == "import-chromium":
            return await import_chromium(args.cdp, args.state)
        async with CDP(args.cdp) as connection:
            if args.action == "restore":
                return await restore(connection, read_private(args.state), lightpanda=not args.chromium)
            previous = read_private(args.state) if args.state.exists() else None
            state = await snapshot(connection, previous)
            write_private(args.state, state)
            return {"saved": True, "cookies": len(state["cookies"]), "storage_origins": len(state["origins"])}
    try:
        print(json.dumps(asyncio.run(run())))
    except (ProtocolError, OSError, ValueError, TimeoutError, aiohttp.ClientError) as exc:
        # Some transport exceptions include URLs. Expose only the type.
        raise SystemExit("Local authentication operation failed: " + type(exc).__name__) from None


if __name__ == "__main__":
    main()
