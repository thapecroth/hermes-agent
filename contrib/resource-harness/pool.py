"""Small Linux admission pools shared by Hermes threads and processes."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path


class CapacityTimeout(TimeoutError):
    pass


class Lease:
    def __init__(self, stream, path: Path):
        self.stream = stream
        self.path = path
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            if not self.stream.closed:
                # Keep the file inode: unlinking a live lock creates a second pool.
                self.stream.seek(0)
                self.stream.truncate()
                self.stream.flush()
                fcntl.flock(self.stream, fcntl.LOCK_UN)
                self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Pool:
    def __init__(self, root: Path, name: str, capacity: int):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("pool capacity must be a positive integer")
        self.root = root / name
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.capacity = capacity

    def acquire(self, owner: str, timeout: float = 600, cancel=None):
        deadline = time.monotonic() + timeout
        interrupted = (cancel if callable(cancel) else cancel.is_set) if cancel is not None else lambda: False
        pause = threading.Event()
        while True:
            if interrupted():
                raise CapacityTimeout("resource wait cancelled")
            for index in range(self.capacity):
                path = self.root / f"{index}.lock"
                stream = path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.close()
                    continue
                try:
                    stream.seek(0)
                    stream.truncate()
                    json.dump({"pid": os.getpid(), "owner": owner,
                               "acquired_at": time.time()}, stream)
                    stream.flush()
                except BaseException:
                    stream.close()
                    raise
                return Lease(stream, path)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CapacityTimeout(f"{self.root.name} pool busy; retry after active work finishes")
            # Sleeps only the calling worker, never the messaging event loop.
            pause.wait(min(0.1, remaining))


def occupied(root: Path):
    """Read actual lock ownership, rather than trusting stale metadata after a crash."""
    result = {}
    for name in ("agents", "api", "browser"):
        owners = []
        for path in sorted((root / name).glob("*.lock")):
            with path.open("a+", encoding="utf-8") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.seek(0)
                    try:
                        owners.append(json.load(stream))
                    except ValueError:
                        owners.append({"owner": "admitting"})
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
        result[name] = owners
    return result
