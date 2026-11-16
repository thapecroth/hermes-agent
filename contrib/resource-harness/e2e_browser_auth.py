"""Real Chromium -> Lightpanda auth transfer, engine restart, and isolation.

Only a local fixture and disposable browser profiles are used. No site login
values, personal URLs or storage contents are written to the receipt.
"""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_auth import CDP, import_chromium, read_private, restore, snapshot, write_private
from pool import Pool


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


@contextlib.asynccontextmanager
async def engine(binary, kind, work):
    port = free_port()
    if kind == "chromium":
        argv = [binary, "--headless", "--no-first-run", "--no-default-browser-check",
                "--disable-dev-shm-usage", "--user-data-dir=" + str(work / "chromium"),
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port), "about:blank"]
    else:
        argv = [binary, "serve", "--host", "127.0.0.1", "--port", str(port)]
    env = dict(os.environ, LIGHTPANDA_DISABLE_TELEMETRY="true")
    log = (work / (kind + ".log")).open("ab")
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=log, env=env, start_new_session=True)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                async with CDP(endpoint, timeout=2) as connection:
                    await connection.pages()
                break
            except Exception:
                if process.returncode is not None or time.monotonic() > deadline:
                    raise RuntimeError(kind + " did not become ready") from None
                await asyncio.sleep(.1)
        yield endpoint
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        log.close()


async def navigate(connection, url):
    pages = await connection.pages()
    if not pages:
        target = await connection.call("Target.createTarget", {"url": "about:blank"})
        pages = [{"targetId": target["targetId"]}]
    session = await connection.attach(pages[0]["targetId"])
    await connection.call("Page.enable", session=session)
    await connection.call("Page.navigate", {"url": url}, session)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = await connection.evaluate("document.querySelector('#status')?.textContent || ''", session)
        if result:
            return session, result
        await asyncio.sleep(.1)
    raise TimeoutError("fixture did not load")


async def run(args):
    work = Path(tempfile.mkdtemp(prefix="hermes-auth-e2e-"))
    work.chmod(0o700)
    seed, persisted = work / "auth/seed.json", work / "auth/persisted.json"
    app = web.Application()

    async def fixture(request):
        return web.Response(text="""<!doctype html><html><body><div id="status"></div><script>
        document.querySelector('#status').textContent = localStorage.getItem('test-token') === 'synthetic-only'
          && sessionStorage.getItem('test-tab') === 'synthetic-tab' ? 'authenticated' : 'not-authenticated';
        </script></body></html>""", content_type="text/html")

    async def cookie(request):
        return web.json_response({"ok": request.cookies.get("fixture-login") == "synthetic-cookie"})

    app.router.add_get("/", fixture)
    app.router.add_get("/cookie", cookie)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    url = f"http://127.0.0.1:{port}/"
    lease = Pool(args.pool, "browser", 2).acquire("browser-auth-e2e", 20)
    result = {"passed": False, "local_only": True, "real_engines": ["chromium", "lightpanda"]}
    try:
        async with engine(args.chromium, "chromium", work) as endpoint:
            async with CDP(endpoint) as connection:
                session, _ = await navigate(connection, url)
                await connection.call("Storage.setCookies", {"cookies": [{"name": "fixture-login", "value": "synthetic-cookie",
                    "url": url, "httpOnly": True, "sameSite": "Lax"}]})
                await connection.evaluate("localStorage.setItem('test-token','synthetic-only');sessionStorage.setItem('test-tab','synthetic-tab')", session)
            await import_chromium(endpoint, seed)
        # The source browser is gone. Every target below starts with empty memory.
        async with engine(args.lightpanda, "lightpanda", work) as endpoint:
            async with CDP(endpoint) as connection:
                receipt = await restore(connection, read_private(seed))
                assert receipt["complete"], receipt
                session, status = await navigate(connection, url)
                assert status == "authenticated", "storage was not restored before site scripts"
                cookie_ok = await connection.evaluate("fetch('/cookie').then(r=>r.json()).then(x=>x.ok)", session)
                # Runtime.evaluate does not await promises in all CDP implementations.
                if cookie_ok is not True:
                    await connection.call("Runtime.evaluate", {"expression": "fetch('/cookie').then(r=>r.json()).then(x=>window.cookieOK=x.ok)", "awaitPromise": True}, session)
                    assert await connection.evaluate("window.cookieOK", session) is True, "HttpOnly cookie not sent"
                state = await snapshot(connection, read_private(seed))
                write_private(persisted, state)
                result.update(cookie_sent=True, storage_before_site_scripts=True, import_verified=receipt)
        async with engine(args.lightpanda, "lightpanda", work) as endpoint:
            async with CDP(endpoint) as connection:
                await restore(connection, read_private(persisted))
                _, status = await navigate(connection, url)
                assert status == "authenticated"
                result["restart_preserved_auth"] = True
        async with engine(args.lightpanda, "lightpanda", work) as endpoint:
            async with CDP(endpoint) as connection:
                _, status = await navigate(connection, url)
                assert status == "not-authenticated"
                assert not await connection.cookies()
                result["unseeded_session_isolated"] = True
        assert seed.stat().st_mode & 0o777 == 0o600
        assert seed.parent.stat().st_mode & 0o777 == 0o700
        result.update(private_permissions=True, passed=True)
    finally:
        lease.close()
        await runner.cleanup()
        shutil.rmtree(work)
        result["cleanup_completed"] = True
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--lightpanda", required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
