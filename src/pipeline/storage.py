"""Storage guard (T348, D298): a live SQLite file must sit on a local Linux fs.

SQLite's WAL mode needs POSIX byte-range locks and a shared-memory ``-shm``
mapping, both kernel objects. Inside a Docker Desktop container a bind mount is
virtiofs/fuse from macOS - neither crosses that boundary, which is how the live
DB was corrupted once (T338). A named volume is ext4 on the VM's own disk and
is safe. So in a container we refuse, fail closed, unless the file is on a
filesystem known to be local. On the host (macOS) nothing is checked.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .config import in_container

log = logging.getLogger(__name__)

MOUNTINFO_PATH = Path("/proc/self/mountinfo")
#: Filesystems whose locks and mmap are kernel-local on the Docker Desktop VM.
LOCAL_FS = frozenset({"ext4", "xfs", "btrfs"})
UNKNOWN_FS = "unknown"
_MOUNTINFO_SEPARATOR = " - "
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
_MOUNTPOINT_FIELD = 4


class UnsafeStorage(RuntimeError):
    """The path is not on a filesystem a live SQLite file may use (D298)."""


def _unescape(field: str) -> str:
    """mountinfo writes space/tab/newline/backslash in a path as ``\\040`` etc."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """(mount point, fs type) per line, in file order. Malformed lines are skipped."""
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        head, sep, tail = line.partition(_MOUNTINFO_SEPARATOR)
        fields = head.split()
        if not sep or len(fields) <= _MOUNTPOINT_FIELD or not tail.split():
            continue
        mounts.append((_unescape(fields[_MOUNTPOINT_FIELD]), tail.split()[0]))
    return mounts


def _contains(mount: str, path: str) -> bool:
    return mount == "/" or path == mount or path.startswith(mount.rstrip("/") + "/")


def fs_type(path: str | Path, mountinfo: str | None = None) -> str:
    """Filesystem type of ``path`` (need not exist), or ``"unknown"``.

    The longest mount point containing the path wins; for an over-mount the
    later line is the one in effect.
    """
    if mountinfo is None:
        try:
            mountinfo = MOUNTINFO_PATH.read_text(encoding="utf-8")
        except OSError:
            return UNKNOWN_FS
    target = os.path.realpath(path)
    best_len, best = -1, UNKNOWN_FS
    for mount, fstype in parse_mountinfo(mountinfo):
        if _contains(mount, target) and len(mount) >= best_len:
            best_len, best = len(mount), fstype
    return best


def require_local_fs(path: str | Path, what: str, mountinfo: str | None = None) -> None:
    """In a container, raise ``UnsafeStorage`` unless ``path`` is on LOCAL_FS."""
    if not in_container():
        return
    fstype = fs_type(path, mountinfo)
    if fstype not in LOCAL_FS:
        raise UnsafeStorage(
            f"refusing to open {what} {path}: it is on a '{fstype}' filesystem, and a live "
            f"SQLite file must be on a local one ({', '.join(sorted(LOCAL_FS))}) inside a "
            "container (D298). Put it on the pipeline-state named volume."
        )
