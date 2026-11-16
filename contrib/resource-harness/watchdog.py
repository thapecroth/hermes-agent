"""Independent gateway recovery. Runs from a systemd user timer; stdlib only."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def resource_usage(unit, cgroup_root=Path('/sys/fs/cgroup')):
    """Read the service's limits and usage, not unrelated host workloads."""
    group = unit.get("ControlGroup", "")
    if not group.startswith("/") or ".." in Path(group).parts:
        return {}
    root = cgroup_root / group.lstrip("/")
    result = {}
    for name in ("memory.current", "memory.max", "pids.current", "pids.max"):
        try:
            value = (root / name).read_text().strip()
            result[name] = int(value) if value != "max" else None
        except (OSError, ValueError):
            result[name] = None
    return result


def assess(unit, state, healthy, cleanup_failed, *, uptime, startup_grace=180, resources=None, threshold=.9):
    if unit.get("ActiveState") != "active":
        return None  # An operator stopping Hermes must keep it stopped.
    if uptime < startup_grace:
        return None
    for kind in ("memory", "pids"):
        usage = (resources or {}).get(kind + ".current")
        limit = (resources or {}).get(kind + ".max")
        if usage is not None and limit and usage >= limit * threshold:
            return f"resource pressure: {kind} at {usage}/{limit} (at least {threshold:.0%})"
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


def restart_and_confirm(unit_name, previous, *, timeout=105):
    """A successful try-restart submission can be a no-op after a manual stop."""
    subprocess.run(["systemctl", "--user", "try-restart", "--no-block", unit_name], check=True, timeout=5)
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(["systemctl", "--user", "show", unit_name, "-p", "ActiveState",
                                 "-p", "MainPID", "-p", "InvocationID", "-p", "Job"],
                                check=True, text=True, capture_output=True, timeout=5)
        current = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        pid = int(current.get("MainPID") or 0)
        if (current.get("ActiveState") == "active" and pid and str(pid) != previous.get("MainPID")
                and current.get("InvocationID") and current["InvocationID"] != previous.get("InvocationID")):
            return current
        if current.get("ActiveState") in ("inactive", "failed") and not current.get("Job"):
            raise RuntimeError("Restart did not occur; service was stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("Restart was not confirmed; automatic continuation is withheld")
        time.sleep(.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--unit", default="hermes-gateway.service")
    parser.add_argument("--startup-grace-seconds", type=float, default=180)
    parser.add_argument("--record-exit", action="store_true")
    parser.add_argument("--resource-threshold", type=float, default=.9)
    args = parser.parse_args()
    if not 0 < args.resource_threshold <= 1:
        parser.error("resource threshold must be greater than zero and at most one")
    root = args.home / "runtime" / "resource-harness"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.record_exit:
        # ExecStopPost sees systemd's result before Restart=always launches a new
        # process. A manual stop has result=success and must not authorize replay.
        if os.environ.get("SERVICE_RESULT") in ("oom-kill", "watchdog", "resources"):
            from manager import Store
            store = Store(root)
            try:
                old = json.loads((args.home / "gateway_state.json").read_text())
            except (OSError, ValueError):
                old = {}
            result = subprocess.run(["systemctl", "--user", "show", args.unit, "-p", "NRestarts"],
                                    check=True, text=True, capture_output=True, timeout=5)
            unit = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            ticket = store.prepare_recovery("systemd resource failure: " + os.environ["SERVICE_RESULT"],
                                           int(old.get("pid") or 0), source_unit=args.unit,
                                           source_restarts=int(unit["NRestarts"]))
            store.recovery_state(ticket, "issued")
        elif os.environ.get("SERVICE_RESULT") == "success" and (root / "manager.sqlite3").exists():
            from manager import Store
            # A manual stop must not leave an old resource ticket for a later start.
            # An intentional watchdog restart is still only prepared at this point.
            with Store(root).db() as db:
                db.execute("UPDATE recoveries SET state='failed' WHERE state='issued'")
        return
    response = subprocess.run(["systemctl", "--user", "show", args.unit,
        "-p", "ActiveState", "-p", "MainPID", "-p", "ActiveEnterTimestampMonotonic", "-p", "ControlGroup", "-p", "InvocationID"],
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
                    startup_grace=args.startup_grace_seconds, resources=resource_usage(unit), threshold=args.resource_threshold)
    try:
        pulse = json.loads(root.joinpath("manager-status.json").read_text())
        if (not reason and uptime >= args.startup_grace_seconds and str(pulse.get("pid")) == unit.get("MainPID")
                and time.time() - pulse.get("updated_at", 0) > 120):
            reason = "Telegram manager scheduler heartbeat is stale"
    except (OSError, ValueError, TypeError):
        pass
    history_path = root / "watchdog.json"
    try:
        previous = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    now = time.time()
    restarts = [t for t in previous.get("restarts", []) if now - t < 1800]
    same_run = previous.get("invocation") == unit.get("InvocationID")
    strikes = (previous.get("strikes", 0) if same_run else 0) + 1 if reason else 0
    should_restart = bool(reason and strikes >= 2 and len(restarts) < 3
                          and (not restarts or now - restarts[-1] >= 300))
    if should_restart:
        print("Restarting Hermes:", reason, flush=True)
        from manager import Store
        store = Store(root)
        ticket = store.prepare_recovery(reason, int(unit.get("MainPID") or 0))
        try:
            replacement = restart_and_confirm(args.unit, unit)
        except Exception:
            store.recovery_state(ticket, "failed")
            raise
        store.recovery_state(ticket, "issued", target_pid=int(replacement["MainPID"]),
                             target_invocation=replacement["InvocationID"])
        restarts.append(now)
        strikes = 0
        root.joinpath("cleanup-failed").unlink(missing_ok=True)
    elif reason:
        print(f"Hermes watchdog: {reason}; strikes={strikes}, restarts/30m={len(restarts)}")
    temp = history_path.with_suffix(".tmp")
    temp.write_text(json.dumps({"checked_at": now, "reason": reason,
                               "strikes": strikes, "restarts": restarts,
                               "invocation": unit.get("InvocationID"), "resources": resource_usage(unit)}), encoding="utf-8")
    temp.replace(history_path)


if __name__ == "__main__":
    main()
