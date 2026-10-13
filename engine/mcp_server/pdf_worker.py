from __future__ import annotations

import resource
import sys

from mcp_server.pdf_parser import MAX_PDF_RESPONSE_BYTES, _worker_main


def _set_resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    # Darwin exposes RLIMIT_AS but rejects finite values; production is Linux.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PDF_RESPONSE_BYTES, MAX_PDF_RESPONSE_BYTES))


if __name__ == "__main__":
    _set_resource_limits()
    try:
        max_chars = int(sys.argv[1])
    except (IndexError, ValueError):
        sys.exit(2)
    sys.exit(_worker_main(max_chars))
