#!/usr/bin/env python3
"""Compare cold vs warm Wowhead search latency with file cache enabled."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from wowhead_cli.wowhead_client import WowheadClient


def _run(query: str, *, cache_dir: Path) -> tuple[float, float]:
    os.environ["WARCRAFT_CACHE_DIR"] = str(cache_dir)
    client = WowheadClient(cache_enabled=True)
    try:
        start = time.perf_counter()
        client.search_suggestions(query)
        cold_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        client.search_suggestions(query)
        warm_ms = (time.perf_counter() - start) * 1000
    finally:
        client.close()
    return cold_ms, warm_ms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="thunderfury", help="Wowhead search query.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of plain text.")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="wowhead-bench-") as tmp:
        cold_ms, warm_ms = _run(args.query, cache_dir=Path(tmp))
    payload = {
        "query": args.query,
        "cold_ms": round(cold_ms, 1),
        "warm_ms": round(warm_ms, 1),
        "speedup_ratio": round(cold_ms / warm_ms, 2) if warm_ms > 0 else None,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"query={args.query} cold_ms={payload['cold_ms']} warm_ms={payload['warm_ms']} speedup={payload['speedup_ratio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
