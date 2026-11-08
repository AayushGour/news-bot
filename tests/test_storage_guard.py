"""Storage guard (T348/D298). Fixture text only - never reads the real /proc."""

import pytest

from pipeline import storage
from pipeline.storage import UnsafeStorage, fs_type, parse_mountinfo, require_local_fs

MOUNTINFO = r"""
22 1 8:1 / / rw,relatime - overlay overlay rw,lowerdir=/x
30 22 0:25 / /app/state rw - ext4 /dev/vda1 rw
31 22 0:26 / /app/data rw - virtiofs mac rw
32 22 0:27 / /app/secrets ro - fakeowner fakeowner ro
33 22 0:28 / /app/odd rw - fuse.grpcfuse grpcfuse rw
34 22 0:29 / /app/with\040space rw - xfs /dev/vdb rw
35 22 0:30 / /app/state/sub rw shared:1 master:2 - btrfs /dev/vdc rw
garbage line
"""


def test_parse_skips_garbage_and_unescapes():
    mounts = dict(parse_mountinfo(MOUNTINFO))
    assert mounts["/app/with space"] == "xfs"
    assert mounts["/app/state/sub"] == "btrfs"
    assert len(mounts) == 7


@pytest.mark.parametrize("path,expected", [
    ("/app/state/app.db", "ext4"),
    ("/app/state/sub/x.db", "btrfs"),
    ("/app/data/app.db", "virtiofs"),
    ("/app/secrets/t.session", "fakeowner"),
    ("/app/odd/x", "fuse.grpcfuse"),
    ("/app/with space/x", "xfs"),
    ("/tmp/x.db", "overlay"),
    ("/app/statex/app.db", "overlay"),  # prefix must respect path boundaries
])
def test_fs_type_longest_mount_wins(path, expected):
    assert fs_type(path, MOUNTINFO) == expected


def test_fs_type_unknown_when_nothing_matches_or_unreadable(monkeypatch, tmp_path):
    assert fs_type("/x", "") == "unknown"
    monkeypatch.setattr(storage, "MOUNTINFO_PATH", tmp_path / "missing")
    assert fs_type("/x") == "unknown"


def test_overmount_later_line_wins():
    text = "1 0 0:1 / /m rw - ext4 a rw\n2 1 0:2 / /m rw - virtiofs b rw\n"
    assert fs_type("/m/f", text) == "virtiofs"


def test_guard_refuses_in_container(monkeypatch):
    monkeypatch.setattr(storage, "in_container", lambda: True)
    for bad in ("/app/data/app.db", "/tmp/app.db", "/app/secrets/s"):
        with pytest.raises(UnsafeStorage) as e:
            require_local_fs(bad, "the database", MOUNTINFO)
        assert bad in str(e.value) and "D298" in str(e.value)
    require_local_fs("/app/state/app.db", "the database", MOUNTINFO)


def test_guard_fails_closed_on_unknown(monkeypatch):
    monkeypatch.setattr(storage, "in_container", lambda: True)
    with pytest.raises(UnsafeStorage, match="unknown"):
        require_local_fs("/x/app.db", "the database", "")


def test_guard_inert_on_host(monkeypatch):
    monkeypatch.setattr(storage, "in_container", lambda: False)
    require_local_fs("/app/data/app.db", "the database", MOUNTINFO)


async def test_database_connect_refuses_in_container(tmp_path, monkeypatch):
    from pipeline.db import Database

    monkeypatch.setattr(storage, "in_container", lambda: True)
    monkeypatch.setattr(storage, "MOUNTINFO_PATH", tmp_path / "none")
    target = tmp_path / "sub" / "app.db"
    with pytest.raises(UnsafeStorage):
        await Database(target).connect()
    assert not target.parent.exists()  # refused before mkdir
