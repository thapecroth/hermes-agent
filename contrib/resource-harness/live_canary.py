"""Submit once and inspect a durable live run; keep credentials in memory only."""
import argparse
import json
import time
import urllib.request
import threading
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "poll", "stop"))
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--watch-seconds", type=float, default=0)
    args = parser.parse_args()
    from hermes_constants import get_hermes_home
    from hermes_cli.config import read_raw_config
    config = read_raw_config()
    key = config["platforms"]["api_server"]["extra"]["key"]
    home = get_hermes_home()
    state = json.loads(home.joinpath("gateway_state.json").read_text())
    base = state["platforms"]["api_server"]["listener_base"].rstrip("/")
    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    if args.action == "start":
        if args.receipt.exists():
            raise RuntimeError("receipt already exists; poll its run instead of submitting again")
        assert state.get("gateway_state") == "running"
        assert state["platforms"]["telegram"]["state"] == "connected"
        tasks = []
        for index in range(6):
            tasks.append({"goal": f"Resource canary API task {index}. Use web_search exactly once to search "
                f"for official Python documentation on {('lists','dictionaries','sets','tuples','strings','iterators')[index]}. "
                "Return one official documentation URL and its topic, maximum 40 words. Do not use a browser, "
                "terminal, execute_code, memory, or send messages."})
        for index in range(4):
            tasks.append({"goal": f"Resource canary browser task {index}. Use browser_exec to open "
                "https://example.com/, wait_for_load(), then print the document title. Return that title only. "
                "Use one browser session. Do not use terminal, execute_code, memory, or send messages."})
        prompt = ("This is a bounded resource-harness qualification run, not a personal research request. "
                  "Do not use personal context, memories, or send messages to any external channel. "
                  "Call delegate_task exactly once with the following ten tasks using supported schema fields. "
                  "Submit all ten in one batch; do not execute their work yourself. "
                  "If the tool dispatches in background mode, report its delegation ID accurately; "
                  "qualification will inspect the durable completion delivery separately.\n" + json.dumps(tasks))
        response = request("/v1/runs", {"input": prompt,
            "instructions": "Use the requested resource qualification procedure. No external messages."})
        print("Admission response keys:", sorted(response))
        run_id = response.get("run_id") or response.get("id")
        if not run_id:
            raise RuntimeError("admission did not return a durable run identity")
        args.receipt.write_text(json.dumps({"run_id": run_id, "submitted_at": time.time(),
            "requested_children": 10, "api_search_tasks": 6, "browser_tasks": 4,
            "admission": response}, indent=2) + "\n")
        print("LIVE_RUN", run_id)
    else:
        receipt = json.loads(args.receipt.read_text())
        run_id = receipt["run_id"]
        if args.watch_seconds:
            import importlib.util
            spec = importlib.util.spec_from_file_location("rh_live_pool", Path(__file__).with_name("pool.py"))
            pool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pool)
            deadline = time.monotonic() + args.watch_seconds
            peaks = receipt.setdefault("observed_peak", {"agents": 0, "api": 0, "browser": 0})
            pause = threading.Event()
            while time.monotonic() < deadline:
                occupancy = pool.occupied(home / "runtime/resource-harness")
                for name, owners in occupancy.items():
                    peaks[name] = max(peaks[name], len(owners))
                pause.wait(0.2)
            print("OBSERVED_PEAK", peaks)
        response = request(f"/v1/runs/{run_id}" + ("/stop" if args.action == "stop" else ""),
                           {} if args.action == "stop" else None)
        receipt["latest"] = response
        receipt["checked_at"] = time.time()
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        print("LIVE_RUN", run_id, "keys", sorted(response))
        print("STATUS", response.get("status"), "ERROR", response.get("error"))
        if args.action == "poll" and response.get("status") == "completed":
            messages = request(f"/api/sessions/{response['session_id']}/messages")
            receipt["session_messages"] = messages
            args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
            print("MESSAGES_SHAPE", sorted(messages) if isinstance(messages, dict) else type(messages).__name__)
            rows = messages.get("data", []) if isinstance(messages, dict) else messages
            batches = [row.get("content", "") for row in rows if isinstance(row, dict)
                       and str(row.get("content", "")).startswith("[ASYNC DELEGATION BATCH COMPLETE")]
            if batches:
                batch = batches[-1]
                completed = len(re.findall(r"\(status=completed, api_calls=", batch))
                browser_results = len(re.findall(r"---\nExample Domain\n", batch))
                receipt["qualification"] = {"completed_children": completed,
                    "verified_browser_titles": browser_results,
                    "passed": completed == 10 and browser_results == 4,
                    "real_provider": True}
                args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
                print("QUALIFICATION", receipt["qualification"])
            else:
                print("QUALIFICATION: waiting for durable background completion delivery")
        for field in ("output", "response", "result"):
            if field in response:
                print(field, json.dumps(response[field])[:5000])


if __name__ == "__main__":
    main()
