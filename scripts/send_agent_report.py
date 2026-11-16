#!/usr/bin/env python3
"""Validate and send a structured report to an owner-provisioned generic webhook.

Run from a Hermes checkout/environment. --dry-run validates without a secret or
network access. The receiving route owns identity, reviewer tools and delivery.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.platforms.agent_reports.contract import MAX_REPORT_BYTES, Report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--url", help="Receiver URL provisioned by the owner")
    parser.add_argument("--secret-env", default="AGENT_REPORT_SECRET", help="Environment variable holding the route secret")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        with args.report.open("rb") as stream:
            report = Report.parse(stream.read(MAX_REPORT_BYTES + 1))
        body = json.dumps(asdict(report), ensure_ascii=True, separators=(",", ":")).encode()
        if len(body) > MAX_REPORT_BYTES:
            raise ValueError("Encoded report exceeds 32768 bytes")
        if args.dry_run:
            print(body.decode())
            return 0
        if not args.url:
            parser.error("--url is required unless --dry-run is selected")
        secret = os.environ.get(args.secret_env, "")
        if not secret.strip():
            parser.error("the route secret environment variable is empty")
        # Same timestamp-bound V2 idiom as `hermes webhook test` (generic signature pin).
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(args.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Webhook-Signature-V2": signature,
            "X-Webhook-Timestamp": timestamp})
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"HTTP {response.status}")
        return 0
    except (OSError, ValueError):
        # Error text from a server/URL can contain secrets or report content.
        print("Report validation or delivery failed; check receiver configuration and retry the same report.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
