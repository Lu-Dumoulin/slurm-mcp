"""Human-readable review text for every change.

A write tool takes this text back as an argument (`review_text`), so the client's permission
prompt shows what will happen: the exact command and the files, script or jobs. The server
refuses the call unless the text equals the one stored with the plan, byte for byte.
"""

from __future__ import annotations

from collections.abc import Sequence

MAX_LINES = 300


def _lines(items: Sequence[str]) -> list[str]:
    shown = list(items[:MAX_LINES])
    if len(items) > len(shown):
        shown.append(f"... and {len(items) - len(shown)} more (all are covered by the plan hash)")
    return shown


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def submit(command: str, script_path: str, sha256: str, script: str, writes_script: bool,
           warnings: Sequence[str] = ()) -> str:
    head = ["SUBMIT A JOB"] + [f"WARNING: {w}" for w in warnings]
    head += [f"command: {command}", f"script: {script_path} (sha256 {sha256[:16]}...)"]
    if writes_script:
        head.append("the script is written to that path first, then submitted")
    return "\n".join(head + ["script text:", "-----", script.rstrip("\n"), "-----"])


def jobs(action: str, command: str, rows: Sequence[dict[str, str]]) -> str:
    items = [f"  {r['job_id']}  {r['state']}  {r['name']}" for r in rows]
    return "\n".join([action.upper(), f"command: {command}", f"jobs ({len(rows)}):"] + _lines(items))


def upload(to: str, source: str, mkdir: str | None, files: Sequence[tuple[str, int, str, bool]], total: int,
           sha: str) -> str:
    """files: (relative path, size, 'create' or 'overwrite', large)"""
    items = [f"  {act:9} {rel}  {_size(size)}{'  LARGE' if large else ''}" for rel, size, act, large in files]
    out = [f"UPLOAD {len(files)} file(s), {_size(total)}", f"from: {source}", f"to: {to}"]
    if mkdir:
        out.append(f"command: {mkdir}")
    out += ["command per file: scp -p <file> <dest>/.<name>.slurm-mcp-part, then mv -f to <name>; nothing is deleted",
            f"plan sha256: {sha[:16]}...", "files:"]
    return "\n".join(out + _lines(items))


def delete(command: str, path: str, files: int, dirs: int, links: int, total: int,
           entries: Sequence[tuple[str, str, int]], sha: str) -> str:
    items = [f"  {k}  {name}  {_size(size)}" for k, name, size in entries]
    out = ["DELETE (cannot be undone)", f"command: {command}", f"path: {path}",
           f"contents: {files} file(s), {dirs} folder(s), {links} link(s), {_size(total)} in files",
           f"plan sha256: {sha[:16]}...", "entries:"]
    return "\n".join(out + _lines(items))
