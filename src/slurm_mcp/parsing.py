"""Pure parsers for squeue, sacct, scontrol and find output."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .diagnose import clean_reason
from .paths import SAFE_PATH_RE
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
    rows = parse_delimited(text, [n for n, _ in SQUEUE_FIELDS])
    for row in rows:
        row["reason"] = clean_reason(row["reason"])  # "(None)" means no reason yet
    return rows


def parse_sacct(text: str) -> list[dict[str, str]]:
    return parse_delimited(text, [f.lower() for f in SACCT_FIELDS])


def parse_sacct_paths(text: str) -> dict[str, str] | None:
    """First row of `sacct --format=WorkDir,StdOut,StdErr`, or None if there is no usable row."""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(FIELD_SEP, 2)]
        if len(parts) == 3 and (not parts[0] or parts[0].startswith("/")):
            return {"workdir": parts[0], "stdout": parts[1], "stderr": parts[2]}
    return None


# Keys are words, and one has a colon (AllocNode:Sid); a value runs up to the next key.
_KEY = r"[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)?"
_KV_RE = re.compile(rf"({_KEY})=(.*?)(?=\s+{_KEY}=|$)")


def parse_scontrol(text: str) -> dict[str, str]:
    """Parse `scontrol show job -o` (one line of Key=Value pairs)."""
    return {k: v.strip() for k, v in _KV_RE.findall(text.strip())}


_LINK_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def parse_dir_links(text: str) -> list[tuple[str, str]]:
    """Pairs of (link name, resolved absolute target) from `dir_links_cmd` output.

    Remote text is shown in the user's terminal, so anything outside a conservative
    character set is dropped.
    """
    lines = [ln for ln in text.splitlines() if ln]
    pairs = []
    for name, target in zip(lines[0::2], lines[1::2]):
        if _LINK_NAME_RE.fullmatch(name) and SAFE_PATH_RE.fullmatch(target) and ".." not in target.split("/"):
            pairs.append((name, target))
    return pairs


def parse_partition_features(text: str) -> dict[str, set[str]]:
    """`sinfo -o '%P|%f'` output to {partition: all node features seen in it}."""
    out: dict[str, set[str]] = {}
    for line in text.splitlines():
        name, sep, feats = line.partition(FIELD_SEP)
        if sep:
            out.setdefault(name.strip().rstrip("*"), set()).update(f for f in feats.strip().split(",") if f)
    return out


def parse_find(text: str) -> list[RemoteFile]:
    files = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] not in ("f", "d"):
            continue
        kind, rel, size, mtime = parts
        try:
            files.append(RemoteFile(rel, int(size), float(mtime), kind))
        except ValueError:
            continue
    return files
