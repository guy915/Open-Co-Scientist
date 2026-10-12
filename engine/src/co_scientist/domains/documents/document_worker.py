import sys

from co_scientist.domains.documents.worker_limits import set_resource_limits

if __name__ == "__main__":
    set_resource_limits()
    from co_scientist.domains.documents.ingest import _document_worker_main

    sys.exit(_document_worker_main(sys.argv[1]))
