from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any


def summarize(root: Path) -> dict[str, Any]:
    rows = list(csv.DictReader((root / "requests_stats.csv").open()))
    samples = [json.loads(line) for line in (root / "telemetry.jsonl").read_text().splitlines()]
    healthy = [sample for sample in samples if "error" not in sample]
    if not healthy:
        raise RuntimeError("No API telemetry samples; capacity is unmeasured")
    requests = [
        row for row in rows if row["Name"] not in {"Aggregated", "fixture", "viewer fixture"}
    ]
    failures = sum(int(row["Failure Count"]) for row in requests)
    total = sum(int(row["Request Count"]) for row in requests)
    return {
        "source_revision": (root / "source-revision.txt").read_text().strip(),
        "api_image": (root / "api-image.txt").read_text().strip(),
        "generator_image": (root / "generator-image.txt").read_text().strip(),
        "harness_sha256": (root / "harness-sha256.txt").read_text().strip(),
        "requests": total,
        "unexpected_errors": failures,
        "unexpected_error_rate": failures / max(total, 1),
        "counts": json.loads((root / "counts.json").read_text()),
        "peak_active_sse": max(sample["active_sse"] for sample in healthy),
        "peak_rss_mib": max(sample["memory_kib"]["VmHWM"] for sample in healthy) / 1024,
        "peak_fds": max(sample["fds"] for sample in healthy),
        "telemetry_errors": len(samples) - len(healthy),
        "measurement_metrics": healthy[-1],
        "final_metrics": json.loads((root / "final-metrics.json").read_text()),
        "latencies_ms": {
            row["Name"]: {p: float(row[p]) for p in ("50%", "95%", "99%")} for row in requests
        },
        "exit_statuses": {
            name: (root / name).read_text().strip()
            for name in ("master-exit.txt", "sampler-exit.txt", "worker-exits.txt")
        },
    }


if __name__ == "__main__":
    result = summarize(Path(sys.argv[1]))
    (Path(sys.argv[1]) / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if (
        result["unexpected_errors"]
        or result["telemetry_errors"]
        or any(
            value != "0"
            for values in result["exit_statuses"].values()
            for value in values.splitlines()
        )
    ):
        sys.exit(1)
