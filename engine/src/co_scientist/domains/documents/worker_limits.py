import sys


def set_resource_limits() -> None:
    # Applied before parser imports, inherited by OCR children. Public parsing
    # fails closed on platforms without these worker limits.
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (115, 115))
    # Darwin exposes RLIMIT_AS but rejects finite values; production is Linux.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024**2, 8 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
