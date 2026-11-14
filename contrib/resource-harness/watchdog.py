"""Independent gateway recovery. Runs from a systemd user timer; stdlib only."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def assess(unit, state, healthy, cleanup_failed, *, uptime, startup_grace=180):
    if unit.get("ActiveState") != "active":
        return None  # An operator stopping Hermes must keep it stopped.
    if uptime < startup_grace:
        return None
    if cleanup_failed:
        return "browser cleanup failed"
    if str(state.get("pid")) != unit.get("MainPID"):
        return "gateway state does not belong to the running process"
    for name, platform in state.get("platforms", {}).items():
        if platform.get("state") == "fatal":
            return f"{name} is fatal ({platform.get('error_code') or 'unknown'})"
    if not healthy:
        return "gateway HTTP health check failed"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--unit", default="hermes-gateway.service")
    parser.add_argument("--startup-grace-seconds", type=float, default=180)
    args = parser.parse_args()
    root = args.home / "runtime" / "resource-harness"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    response = subprocess.run(["systemctl", "--user", "show", args.unit,
        "-p", "ActiveState", "-p", "MainPID", "-p", "ActiveEnterTimestampMonotonic"],
        check=True, text=True, capture_output=True, timeout=5)
    unit = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    if unit.get("ActiveState") != "active":
        return
    try:
        state = json.loads((args.home / "gateway_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    api = state.get("platforms", {}).get("api_server", {})
    base = api.get("listener_base", "http://127.0.0.1:8642")
    url = urlsplit(base)
    healthy = False
    if url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1"):
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=5) as result:
                healthy = result.status == 200 and json.load(result).get("status") == "ok"
        except (OSError, ValueError):
            healthy = False
    uptime = time.monotonic() - int(unit.get("ActiveEnterTimestampMonotonic", "0")) / 1_000_000
    reason = assess(unit, state, healthy, root.joinpath("cleanup-failed").exists(), uptime=uptime,
                    startup_grace=args.startup_grace_seconds)
    history_path = root / "watchdog.json"
    try:
        previous = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    now = time.time()
    restarts = [t for t in previous.get("restarts", []) if now - t < 1800]
    strikes = previous.get("strikes", 0) + 1 if reason else 0
    should_restart = bool(reason and strikes >= 2 and len(restarts) < 3
                          and (not restarts or now - restarts[-1] >= 300))
    if should_restart:
        print("Restarting Hermes:", reason, flush=True)
        subprocess.run(["systemctl", "--user", "try-restart", "--no-block", args.unit],
                       check=True, timeout=5)
        restarts.append(now)
        strikes = 0
        root.joinpath("cleanup-failed").unlink(missing_ok=True)
    elif reason:
        print(f"Hermes watchdog: {reason}; strikes={strikes}, restarts/30m={len(restarts)}")
    temp = history_path.with_suffix(".tmp")
    temp.write_text(json.dumps({"checked_at": now, "reason": reason,
                               "strikes": strikes, "restarts": restarts}), encoding="utf-8")
    temp.replace(history_path)


if __name__ == "__main__":
    main()
