"""Remote path validation.

A path coming from the model is untrusted. It must be absolute, made only of
a conservative character set (no spaces, globs, quotes or `~`), free of `..`
segments, and inside one of the configured roots. Symlinks on the cluster are
not resolved here, see the README.
"""

from __future__ import annotations

import fnmatch
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


# --- names that look like secrets: never read, listed, downloaded or uploaded ------------------------------
SENSITIVE_NAMES = (
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".env", ".env.*",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.jks",
    "*.keystore", "*.gpg", "*.ovpn", "*.keychain", "*.keychain-db", "credentials*", "secrets*", "*.secret", "*.token",
    "terraform.tfstate*", ".bash_history", ".zsh_history", ".python_history", ".psql_history", ".mysql_history",
    ".viminfo", "repl_history.jl",
)


def sensitive_match(path: str, extra: tuple[str, ...] = ()) -> str | None:
    """The pattern that makes `path` look like a secret (any component or the file name), or None."""
    for part in path.split("/"):
        low = part.lower()
        for pattern in SENSITIVE_NAMES + extra:
            if fnmatch.fnmatchcase(low, pattern.lower()):
                return pattern
    return None


_SYSTEM_ROOTS = {"bin", "sbin", "etc", "usr", "var", "dev", "opt", "proc", "sys", "boot", "root", "lib", "lib64", "run", "tmp"}


def broad_remote_root_problem(path: str) -> str | None:
    """Why a configured remote root is too broad: it would open the whole cluster (or a system folder)."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "it is the filesystem root"
    if parts[0] in _SYSTEM_ROOTS:
        return "it is a system folder"
    if len(parts) < 2:
        return "it is a top-level folder such as /home or /srv; use your own folder below it"
    return None
