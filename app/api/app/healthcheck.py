"""Container-local semantic health check used by Jenkins smoke stages."""

from __future__ import annotations

import argparse
import json
from urllib.request import urlopen


def validate(body: dict, *, expected_test_mode: bool | None) -> None:
    queue = body.get("requestAuditQueue") or {}
    if body.get("status") != "ok":
        raise RuntimeError(f"API status is {body.get('status')!r}")
    if expected_test_mode is not None and body.get("testMode") is not expected_test_mode:
        raise RuntimeError("testMode does not match the deployment environment")
    if queue.get("enabled") is not True or queue.get("available") is not True:
        raise RuntimeError("request audit Redis producer is unavailable")
    if queue.get("workerHealthy") is not True:
        raise RuntimeError("request audit worker heartbeat is unavailable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:3000/api/v1/health")
    parser.add_argument("--expected-test-mode", choices=("true", "false"))
    args = parser.parse_args()
    expected = (
        None
        if args.expected_test_mode is None
        else args.expected_test_mode == "true"
    )
    with urlopen(args.url, timeout=5) as response:
        body = json.load(response)
    validate(body, expected_test_mode=expected)
    print(json.dumps(body, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
