import hashlib
import io
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path


TOOLS = {
    "gitleaks": (
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        "gitleaks",
    ),
    "actionlint": (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        "actionlint",
    ),
    "bun": (
        "https://github.com/oven-sh/bun/releases/download/bun-v1.3.14/bun-linux-x64.zip",
        "951ee2aee855f08595aeec6225226a298d3fea83a3dcd6465c09cbccdf7e848f",
        "bun-linux-x64/bun",
    ),
}


def extract_verified(
    archive: bytes, checksum: str, binary: str, destination: Path
) -> None:
    if hashlib.sha256(archive).hexdigest() != checksum:
        raise ValueError(f"{binary}: archive checksum mismatch")
    if zipfile.is_zipfile(io.BytesIO(archive)):
        with zipfile.ZipFile(io.BytesIO(archive)) as contents:
            member = contents.getinfo(binary)
            mode = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir() or mode not in (0, stat.S_IFREG):
                raise ValueError(f"{binary}: expected a regular file")
            executable = contents.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as contents:
            member = contents.getmember(binary)
            if not member.isfile():
                raise ValueError(f"{binary}: expected a regular file")
            source = contents.extractfile(member)
            if source is None:
                raise ValueError(f"{binary}: archive lacks executable")
            executable = source.read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(executable)
    destination.chmod(0o755)


def install(binary: str, directory: Path) -> Path:
    url, checksum, member = TOOLS[binary]
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    destination = directory / binary
    extract_verified(archive, checksum, member, destination)
    print(f"Installed checksum-verified {binary} at {destination}")
    return destination


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]))
