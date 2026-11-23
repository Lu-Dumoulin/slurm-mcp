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


def _split_top_level(text: str) -> list[str]:
    """Split a GRES string on commas that are not inside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [p for p in parts + [cur] if p]


def _number_first(text: str) -> tuple[int, str]:
    """Sort key for values like 12+ and 64: by the leading number."""
    m = re.match(r"\d+", text)
    return (int(m.group()) if m else 0, text)


def parse_partitions(text: str) -> list[dict]:
    """`sinfo -o '%P|%l|%D|%c|%m|%G'` rows merged per partition (one row per kind of node)."""
    merged: dict[str, dict] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(FIELD_SEP)]
        if len(parts) != 6:
            continue
        name, limit, nodes, cpus, mem, gres = parts
        entry = merged.setdefault(name.rstrip("*"), {
            "partition": name.rstrip("*"), "default": name.endswith("*"), "time_limit": limit,
            "nodes": 0, "cpus_per_node": set(), "memory_mb": set(), "gres": []})
        entry["nodes"] += int(nodes) if nodes.isdigit() else 0
        entry["cpus_per_node"].add(cpus)
        entry["memory_mb"].add(mem)
        for g in _split_top_level(gres):
            g = re.sub(r"\(S:[^)]*\)", "", g)  # drop the socket annotation
            if g not in ("(null)", "") and g not in entry["gres"]:
                entry["gres"].append(g)
    out = []
    for e in merged.values():
        e["cpus_per_node"] = sorted(e["cpus_per_node"], key=_number_first)
        e["memory_mb"] = sorted(e["memory_mb"], key=_number_first)
        out.append(e)
    return out


def parse_slurm_limits(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Selected `Key = value` lines of `scontrol show config`."""
    found = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in keys:
            found[k.strip()] = v.strip()
    return found


_UNIT = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "PiB": 1024**5}


def _size_bytes(text: str) -> int | None:
    """`530.46 GiB` to bytes; None for `unlimited` or anything else."""
    parts = text.split()
    if len(parts) == 2 and parts[1] in _UNIT:
        try:
            return int(float(parts[0]) * _UNIT[parts[1]])
        except ValueError:
            return None
    return None


def _count(text: str) -> int | None:
    t = text.replace(",", "").strip()
    return int(t) if t.isdigit() else None


def parse_beegfs_quota(text: str) -> list[dict] | None:
    """The table printed by Baobab's beegfs-get-quota-home-scratch.sh, one entry per storage row.
    Returns None when the output does not look like that table, so the caller can show it as is."""
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) == 7 and cells[0] in ("home", "scratch") and cells[2].isdigit():
            rows.append({
                "storage": cells[0], "name": cells[1], "id": int(cells[2]),
                "size_used": cells[3], "size_hard": cells[4], "files_used": cells[5], "files_hard": cells[6],
                "size_used_bytes": _size_bytes(cells[3]), "size_hard_bytes": _size_bytes(cells[4]),
                "files_used_count": _count(cells[5]), "files_hard_count": _count(cells[6]),
                "size_unlimited": cells[4].lower() == "unlimited", "files_unlimited": cells[6].lower() == "unlimited",
            })
    return rows or None


def parse_df(text: str) -> dict | None:
    """Last row of `df -P -k` as byte counts."""
    lines = [ln.split() for ln in text.strip().splitlines()]
    if len(lines) < 2 or len(lines[-1]) < 6:
        return None
    fs, total, used, avail, pct, mount = lines[-1][0], lines[-1][1], lines[-1][2], lines[-1][3], lines[-1][4], " ".join(lines[-1][5:])
    if not (total.isdigit() and used.isdigit() and avail.isdigit()):
        return None
    return {"filesystem": fs, "total_bytes": int(total) * 1024, "used_bytes": int(used) * 1024,
            "available_bytes": int(avail) * 1024, "use_percent": pct, "mounted_on": mount}


def parse_du(text: str) -> int | None:
    """Bytes from `du -s -k` output (first column)."""
    first = text.strip().split("\t", 1)[0].split(" ", 1)[0]
    return int(first) * 1024 if first.isdigit() else None


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
