"""Pure parsers for squeue, sacct, scontrol and find output."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .commands import FIELD_SEP, SACCT_FIELDS, SQUEUE_FIELDS
from .sync import RemoteFile


def parse_delimited(text: str, names: Sequence[str]) -> list[dict[str, str]]:
    """Split lines on '|'. The last field is free text, so it takes the remainder."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(FIELD_SEP, len(names) - 1)
        if len(parts) != len(names):
            continue  # malformed line: skip rather than guess
        rows.append(dict(zip(names, (p.strip() for p in parts))))
    return rows


def parse_squeue(text: str) -> list[dict[str, str]]:
    return parse_delimited(text, [n for n, _ in SQUEUE_FIELDS])


def parse_sacct(text: str) -> list[dict[str, str]]:
    return parse_delimited(text, [f.lower() for f in SACCT_FIELDS])


_KV_RE = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)")


def parse_scontrol(text: str) -> dict[str, str]:
    """Parse `scontrol show job -o` (one line of Key=Value pairs)."""
    return {k: v.strip() for k, v in _KV_RE.findall(text.strip())}


def parse_find(text: str) -> list[RemoteFile]:
    files = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        rel, size, mtime = parts
        try:
            files.append(RemoteFile(rel, int(size), float(mtime)))
        except ValueError:
            continue
    return files
