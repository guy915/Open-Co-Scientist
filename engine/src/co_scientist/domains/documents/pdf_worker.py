from __future__ import annotations

import sys

from co_scientist.domains.documents.worker_limits import set_resource_limits

if __name__ == "__main__":
    set_resource_limits()
    from co_scientist.domains.documents.ingest import _pdf_worker_main

    sys.exit(_pdf_worker_main())
