"""Remote path validation.

A path coming from the model is untrusted. It must be absolute, made only of
a conservative character set (no spaces, globs, quotes or `~`), free of `..`
segments, and inside one of the configured roots. Symlinks on the cluster are
not resolved here, see the README.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence

from .errors import PathError

SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9_.+@%=,:/-]*")  # always used with fullmatch


def _check_chars(path: str) -> None:
    if not isinstance(path, str) or not SAFE_PATH_RE.fullmatch(path):
        raise PathError(f"path has unsupported characters or is not absolute: {path!r}")
    if ".." in path.split("/"):
        raise PathError(f"'..' is not allowed in paths: {path!r}")


def normalize_root(path: str) -> str:
    _check_chars(path)
    return posixpath.normpath(path)


def validate_remote_path(path: str, roots: Sequence[str]) -> str:
    """Return the normalized path, or raise PathError."""
    _check_chars(path)
    norm = posixpath.normpath(path)
    for root in roots:
        if norm == root or norm.startswith(root.rstrip("/") + "/"):
            return norm
    raise PathError(f"path is outside the configured roots: {path!r}")


def is_safe_relative(rel: str) -> bool:
    """True for a relative file name that is safe to pass to scp and to join locally."""
    if not rel or rel.startswith("/"):
        return False
    if ".." in rel.split("/"):
        return False
    return bool(SAFE_PATH_RE.fullmatch("/" + rel))
