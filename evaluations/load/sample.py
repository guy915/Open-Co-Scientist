from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with args.output.open("w") as output:
        for _ in range(args.seconds):
            start = time.monotonic()
            try:
                with opener.open("http://api:8008/__load__/metrics", timeout=10) as r:
                    row = json.load(r)
                row["timestamp"] = time.time()
                output.write(json.dumps(row) + "\n")
                output.flush()
            except OSError as error:
                output.write(json.dumps({"error": str(error), "timestamp": time.time()}) + "\n")
            time.sleep(max(0, 1 - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
