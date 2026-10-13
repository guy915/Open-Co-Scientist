from __future__ import annotations

import os
import subprocess
import sys

peers = int(os.getenv("LOAD_PEERS", "40"))
if not 4 <= peers <= 128:
    raise RuntimeError("LOAD_PEERS must be between 4 and 128")
for index in range(peers):
    subprocess.run(
        ["ip", "address", "add", f"172.29.251.{101 + index}/24", "dev", "eth0"], check=True
    )
os.execvp("locust", ["locust", *sys.argv[1:]])
