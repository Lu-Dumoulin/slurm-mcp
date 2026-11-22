"""Incremental download planning.

`plan_sync` is a pure function: it decides what is stale and does nothing else.
The executor lives in cluster.py. Same split as CommonUI's sync: the deciding part
is where the bugs are, so it is the part that is easy to unit-test.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

MTIME_TOLERANCE_S = 2.0  # filesystems differ in timestamp resolution


@dataclass(frozen=True)
class RemoteFile:
    rel: str
    size: int
    mtime: float


@dataclass(frozen=True)
class Planned:
    file: RemoteFile
    reason: str  # "missing" | "size" | "newer"


@dataclass(frozen=True)
class SyncPlan:
    download: tuple[Planned, ...]
    up_to_date: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(p.file.size for p in self.download)


def matches(rel: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(posixpath.basename(rel), pattern)


def plan_sync(
    remote: Iterable[RemoteFile],
    local: Mapping[str, tuple[int, float]],
    pattern: str = "*",
    tolerance: float = MTIME_TOLERANCE_S,
) -> SyncPlan:
    """`local` maps relative path to (size, mtime). Only missing or newer files are planned."""
    todo: list[Planned] = []
    same: list[str] = []
    for f in sorted(remote, key=lambda r: r.rel):
        if not matches(f.rel, pattern):
            continue
        have = local.get(f.rel)
        if have is None:
            todo.append(Planned(f, "missing"))
        elif have[0] != f.size:
            todo.append(Planned(f, "size"))
        elif f.mtime > have[1] + tolerance:
            todo.append(Planned(f, "newer"))
        else:
            same.append(f.rel)
    return SyncPlan(tuple(todo), tuple(same))


def scan_local(root: Path) -> dict[str, tuple[int, float]]:
    """Relative POSIX path -> (size, mtime) for every regular file under root."""
    state: dict[str, tuple[int, float]] = {}
    if not root.is_dir():
        return state
    for p in root.rglob("*"):
        if p.is_file() and not p.name.endswith(".part"):
            st = p.stat()
            state[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime)
    return state
