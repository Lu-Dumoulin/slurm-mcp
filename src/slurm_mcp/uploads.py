"""Local side of code upload: which folders may be uploaded, which files leave this computer,
and how a local tree compares with the cluster.

Uploading is the one place where files move from your disk to the cluster, so the rules
are strict: the local path must be inside an allowed folder, symlinks are never followed,
secrets and build clutter are excluded by name, and only files whose names pass the same
character check as remote paths are sent. There is no size limit, but files above
`large_file_mb` need an explicit extra confirmation (initial conditions can be large).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SlurmMcpError
from .paths import SENSITIVE_NAMES, is_safe_relative
from .sync import MTIME_TOLERANCE_S

# Names that are never uploaded. Extra patterns can be added in the config (upload_exclude).
# `data` folders are deliberately not listed: they may hold initial conditions.
DEFAULT_EXCLUDE = (
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", "node_modules", ".ipynb_checkpoints",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode", ".DS_Store", "out", "output", "outputs", "results",
    "*.pyc", "*.pyo", "*.egg-info",
) + SENSITIVE_NAMES

# Folders that are never accepted as "the project the agent is working in".
# Names are compared in lower case, so they also match on Windows and on case-insensitive macOS.
_SYSTEM_STRICT = {"bin", "sbin", "etc", "usr", "dev", "proc", "sys", "boot", "system", "library", "applications", "root",
                  "windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information"}
_SYSTEM_SHALLOW = {"var", "private", "tmp", "opt", "lib", "run"}   # only the folder itself or one level below
_HOME_STANDARD = {"desktop", "documents", "downloads", "library", "movies", "music", "pictures", "public",
                  "videos", "onedrive", "appdata"}


@dataclass(frozen=True)
class LocalFile:
    rel: str          # POSIX path relative to the uploaded folder
    path: str         # absolute local path
    size: int
    mtime: float
    mtime_ns: int


@dataclass(frozen=True)
class LocalScan:
    files: tuple[LocalFile, ...]
    excluded: int
    symlinks: int
    unsafe_names: int


@dataclass(frozen=True)
class UploadRoot:
    path: Path
    rule: str   # why this folder is allowed, shown in the dry run


def project_dir_problem(path: Path, home: Path) -> str | None:
    """Why `path` is too broad to be treated as the agent's project folder, or None if it is fine."""
    if not path.is_absolute() or not path.is_dir():
        return "not an existing absolute folder"
    return broad_folder_problem(path, home)


def broad_folder_problem(path: Path, home: Path) -> str | None:
    """Why a folder is too broad to upload from (the folder need not exist)."""
    if path == type(path)(path.anchor):
        return "it is the filesystem root (or a drive root)"
    if path == home or path in home.parents:
        return "it is your home folder or above"
    if home not in path.parents and len(path.parts) >= 2:
        top, depth = path.parts[1].lower(), len(path.parts) - 1
        if top in _SYSTEM_STRICT or (top in _SYSTEM_SHALLOW and depth <= 2):
            return "it is a system folder"
    if path.parent == home and path.name.lower() in _HOME_STANDARD:
        return f"{path.name} is a general folder, not a project"
    return None


def effective_roots(
    configured: Iterable[Path],
    *,
    from_cwd: bool = True,
    cwd: Path | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[list[UploadRoot], list[str]]:
    """Allowed local folders and notes about candidates that were refused.

    The default is the folder the server was started in: Claude Code starts its MCP servers in
    the project folder, while Claude Desktop starts them in `/`, which is refused here, so
    there the config's `local_roots` apply. A client may also name the project folder in
    CLAUDE_PROJECT_DIR.
    """
    home = (home or Path.home()).resolve()
    roots: list[UploadRoot] = []
    notes: list[str] = []
    if from_cwd:
        candidates = []
        env_dir = (env if env is not None else os.environ).get("CLAUDE_PROJECT_DIR")
        if env_dir:
            candidates.append((Path(env_dir).expanduser(), "the project folder named in CLAUDE_PROJECT_DIR"))
        candidates.append(((cwd or Path.cwd()), "the folder the server was started in (the agent's working folder)"))
        for cand, rule in candidates:
            try:
                real = cand.resolve()
            except OSError:
                continue
            problem = project_dir_problem(real, home)
            if problem is None:
                if all(real != r.path for r in roots):
                    roots.append(UploadRoot(real, rule))
            else:
                notes.append(f"{real} not used as the upload folder: {problem}")
    for r in configured:
        if all(r != x.path for x in roots):
            roots.append(UploadRoot(r, "listed in local_roots in the config"))
    return roots, notes


def is_link_or_junction(path: Path) -> bool:
    """A symbolic link, or on Windows a junction or other reparse point. Python 3.11 does not see junctions as links."""
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)          # Python 3.12 and later
    if junction is not None and junction():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)    # only exists on Windows
    except OSError:
        return False
    return bool(attributes & 0x400)                          # FILE_ATTRIBUTE_REPARSE_POINT


CRLF_CHECKED = (".sh", ".bash", ".slurm", ".sbatch", ".jl", ".py", ".csv")


def script_bytes(text: str) -> bytes:
    """The exact bytes of a generated script: UTF-8 with LF line endings on every system."""
    return text.encode("utf-8")


def crlf_files(files: Iterable[LocalFile]) -> list[str]:
    """Relative paths of script, code and table files that have Windows line endings (CRLF)."""
    found = []
    for f in files:
        if f.rel.lower().endswith(CRLF_CHECKED):
            try:
                with open(f.path, "rb") as fh:
                    if b"\r\n" in fh.read(65536):
                        found.append(f.rel)
            except OSError:
                continue
    return found


def is_excluded(name: str, rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(rel, p) for p in patterns)


def resolve_local_path(local_path: str | None, roots: tuple[UploadRoot, ...]) -> tuple[Path, UploadRoot]:
    """The real path of `local_path` (default: the one allowed folder) and the rule that allows it."""
    if not roots:
        raise SlurmMcpError(
            "uploading is not set up: there is no allowed local folder. Start the agent in your project "
            "folder, or add local_roots to the config"
        )
    if not local_path:
        if len(roots) != 1:
            raise SlurmMcpError("several local folders are allowed; give local_path: " + ", ".join(str(r.path) for r in roots))
        return roots[0].path, roots[0]
    raw = Path(str(local_path)).expanduser()
    if not raw.is_absolute():
        raise SlurmMcpError(f"local path must be absolute: {local_path!r}")
    try:
        real = raw.resolve(strict=True)
    except OSError:
        raise SlurmMcpError(f"local path does not exist: {local_path}") from None
    for r in roots:
        if real == r.path or r.path in real.parents:
            return real, r
    raise SlurmMcpError(f"local path is outside the allowed local folders: {local_path}")


def scan_local(real: Path, exclude: tuple[str, ...], max_files: int) -> LocalScan:
    """Regular files under `real` (or `real` itself), without following symlinks."""
    files: list[LocalFile] = []
    excluded = symlinks = unsafe = 0

    def add(path: Path, rel: str) -> None:
        nonlocal unsafe
        if not is_safe_relative(rel):
            unsafe += 1
            return
        st = path.stat()
        files.append(LocalFile(rel, str(path), st.st_size, st.st_mtime, st.st_mtime_ns))
        if len(files) > max_files:
            raise SlurmMcpError(f"more than {max_files} files to consider; upload a smaller folder or raise max_upload_files")

    if real.is_file():
        if is_excluded(real.name, real.name, exclude):
            raise SlurmMcpError(f"{real.name} matches an excluded name (secrets and build files are never uploaded)")
        add(real, real.name)
        return LocalScan(tuple(files), 0, 0, unsafe)

    for dirpath, dirnames, filenames in os.walk(real, topdown=True, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            rel = (here / d).relative_to(real).as_posix()
            if is_link_or_junction(here / d):
                symlinks += 1
            elif is_excluded(d, rel, exclude):
                excluded += 1
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            p = here / f
            rel = p.relative_to(real).as_posix()
            if is_link_or_junction(p):
                symlinks += 1
            elif is_excluded(f, rel, exclude):
                excluded += 1
            elif p.is_file():
                add(p, rel)
    return LocalScan(tuple(files), excluded, symlinks, unsafe)


def plan_hash(local_root: str, remote_dir: str, items: list[tuple[str, int, int, str]]) -> str:
    """Fingerprint of an upload plan: source, destination and every file with size, mtime and reason."""
    blob = json.dumps([local_root, remote_dir, sorted(items)], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# --- comparing a local tree with the cluster (CommonUI's check compares sizes) --------------------

@dataclass(frozen=True)
class Comparison:
    matching: tuple[str, ...] = ()
    only_local: tuple[str, ...] = ()
    only_remote: tuple[str, ...] = ()
    size_differs: tuple[str, ...] = ()
    local_newer: tuple[str, ...] = ()    # same size, local file modified later
    remote_newer: tuple[str, ...] = ()   # same size, cluster file modified later
    notes: tuple[str, ...] = field(default=())

    @property
    def identical(self) -> bool:
        return not (self.only_local or self.size_differs or self.local_newer or self.remote_newer)


def compare_trees(
    local: Iterable[tuple[str, int, float]],
    remote: Mapping[str, tuple[int, float]],
    tolerance: float = MTIME_TOLERANCE_S,
) -> Comparison:
    """Sizes decide whether files differ, as in CommonUI. Modification times (kept by `scp -p`) only
    label same-size files as newer on one side. Files that exist only on the cluster are listed
    but do not make the trees non-identical (outputs and other files are expected there)."""
    match, only_l, diff, l_new, r_new = [], [], [], [], []
    seen = set()
    for rel, size, mtime in sorted(local):
        seen.add(rel)
        have = remote.get(rel)
        if have is None:
            only_l.append(rel)
        elif have[0] != size:
            diff.append(rel)
        elif mtime > have[1] + tolerance:
            l_new.append(rel)
        elif have[1] > mtime + tolerance:
            r_new.append(rel)
        else:
            match.append(rel)
    only_r = sorted(set(remote) - seen)
    return Comparison(tuple(match), tuple(only_l), tuple(only_r), tuple(diff), tuple(l_new), tuple(r_new))
