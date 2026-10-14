import os
import sys
import sysconfig
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SandboxKind(Enum):
    READ_ONLY = "read_only"

    WORKSPACE_WRITE = "workspace_write"

    # Unsandboxed execution must be explicitly named and visible, never a
    # missing-backend fallback.
    DANGER_FULL_ACCESS = "danger_full_access"

    # EXTERNAL delegates confinement to the caller's boundary outside this
    # process.
    EXTERNAL = "external"


# Protected repository metadata controls future behavior; Landlock refuses
# inexpressible carve-outs.
PROTECTED_METADATA_NAMES = (".git", ".agents", ".claude")

# Harness scratch protection is optional, unlike repository metadata. Host-side
# resolved-path guards enforce spill containment on every backend.
HARNESS_METADATA_NAME = ".cosci"

METADATA_NAMES = (*PROTECTED_METADATA_NAMES, HARNESS_METADATA_NAME)


@dataclass(frozen=True)
class SandboxPolicy:
    kind: SandboxKind
    writable_roots: tuple[Path, ...] = field(default_factory=tuple)
    network_allowed: bool = False
    readable_roots: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """macOS /var points to /private/var; OS grants need real paths.
        Relative roots would resolve against the confined process's cwd.
        """
        relative = [str(p) for p in self.writable_roots if not p.is_absolute()]
        if relative:
            raise ValueError(f"writable_roots must be absolute paths; got {relative}")
        object.__setattr__(
            self,
            "writable_roots",
            tuple(p.resolve() for p in self.writable_roots),
        )
        relative = [str(p) for p in self.readable_roots if not p.is_absolute()]
        if relative:
            raise ValueError(f"readable_roots must be absolute paths; got {relative}")
        object.__setattr__(
            self,
            "readable_roots",
            tuple(p.resolve() for p in self.readable_roots),
        )

    @property
    def confines_in_process(self) -> bool:
        return self.kind in (
            SandboxKind.READ_ONLY,
            SandboxKind.WORKSPACE_WRITE,
        )

    @property
    def allows_network(self) -> bool:
        if self.kind is SandboxKind.READ_ONLY:
            return False
        if self.kind is SandboxKind.WORKSPACE_WRITE:
            return self.network_allowed
        return True


def read_only(*roots: Path) -> SandboxPolicy:
    return SandboxPolicy(
        kind=SandboxKind.READ_ONLY,
        readable_roots=(*runtime_read_roots(), *roots),
    )


def workspace_write(*roots: Path, network_allowed: bool = False) -> SandboxPolicy:
    return SandboxPolicy(
        kind=SandboxKind.WORKSPACE_WRITE,
        writable_roots=tuple(roots),
        readable_roots=(*runtime_read_roots(), *roots),
        network_allowed=network_allowed,
    )


def runtime_read_roots() -> tuple[Path, ...]:
    """Small immutable runtime closure; service data and source trees stay out."""
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        Path("/bin"),
        Path("/usr/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/usr/lib"),
        Path(sys.executable).absolute().parent,
        Path(sys.prefix) / "lib" / version,
        Path(sysconfig.get_path("stdlib")),
        Path(sysconfig.get_path("purelib")),
        Path(sysconfig.get_path("platlib")),
        Path("/etc/ld.so.cache"),
        Path("/etc/ssl/certs"),
        Path("/etc/hosts"),
        Path("/etc/resolv.conf"),
        Path("/etc/nsswitch.conf"),
        Path("/dev/null"),
    ]
    # Shared Python builds load libpython beside, rather than within, stdlib.
    libdir = sysconfig.get_config_var("LIBDIR")
    libname = sysconfig.get_config_var("LDLIBRARY")
    if isinstance(libname, str):
        candidates.append(Path(sys.base_prefix) / "lib" / libname)
        if isinstance(libdir, str):
            candidates.append(Path(libdir) / libname)
    # A venv interpreter reads pyvenv.cfg at startup; the rest of the venv stays out.
    if sys.prefix != sys.base_prefix:
        candidates.append(Path(sys.prefix) / "pyvenv.cfg")
    skills = os.getenv("COSCIENTIST_SKILLS_DIR")
    if skills:
        candidates.append(Path(skills).expanduser())
    skills_python = os.getenv("COSCIENTIST_SKILLS_PYTHON")
    if skills_python:
        interpreter = Path(skills_python).absolute()
        venv = interpreter.parent.parent
        candidates.extend(
            (
                interpreter,
                interpreter.resolve(),
                venv / "bin",
                venv / "lib" / version,
                venv / "pyvenv.cfg",
            )
        )
    roots = tuple(dict.fromkeys(path.resolve() for path in candidates if path.exists()))
    if Path("/") in roots:
        raise ValueError("runtime read roots must not include the filesystem root")
    return roots
