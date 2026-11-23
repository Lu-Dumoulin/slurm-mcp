"""Confirmation tokens for actions that change the cluster.

A dry run stores the exact remote command and returns a random single-use token. The
real call takes only that token, never the parameters again, so what runs is exactly
what was shown. Tokens expire and live in memory only: restarting the server
invalidates them.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .errors import SlurmMcpError


@dataclass(frozen=True)
class PendingAction:
    kind: str
    remote_args: tuple[str, ...]
    summary: str
    expires_at: float
    script_text: str | None = None   # generated script to upload before running remote_args
    script_path: str | None = None
    upload: tuple[tuple[str, str, int, int], ...] = ()   # (local path, remote path, size, mtime_ns)
    mkdirs: tuple[str, ...] = ()
    plan_sha256: str = ""
    dest: str = ""
    review: str = ""      # the text the human sees in the approval prompt; the write call must repeat it


class ConfirmationStore:
    def __init__(self, ttl_s: float = 300, clock: Callable[[], float] = time.monotonic, max_pending: int = 20):
        self.ttl_s = ttl_s
        self._clock = clock
        self._max = max_pending
        self._pending: dict[str, PendingAction] = {}

    def _purge(self) -> None:
        now = self._clock()
        for tok in [t for t, a in self._pending.items() if a.expires_at <= now]:
            del self._pending[tok]

    def issue(self, kind: str, remote_args: list[str], summary: str,
              script_text: str | None = None, script_path: str | None = None,
              upload: tuple[tuple[str, str, int, int], ...] = (), mkdirs: tuple[str, ...] = (),
              plan_sha256: str = "", dest: str = "", review: str = "") -> str:
        self._purge()
        while len(self._pending) >= self._max:
            del self._pending[next(iter(self._pending))]  # drop the oldest
        token = secrets.token_urlsafe(12)
        self._pending[token] = PendingAction(kind, tuple(remote_args), summary, self._clock() + self.ttl_s,
                                              script_text, script_path, upload, mkdirs, plan_sha256, dest, review)
        return token

    def peek(self, token: str, kind: str) -> PendingAction:
        """Like redeem, but the token stays valid (used to refuse a call that lacks a required extra confirmation)."""
        self._purge()
        action = self._pending.get(token)
        if action is None:
            raise SlurmMcpError("unknown or expired confirmation token: run the dry run again")
        if action.kind != kind:
            raise SlurmMcpError(f"this token is for a {action.kind} action, not {kind}")
        return action

    def redeem(self, token: str, kind: str) -> PendingAction:
        """Return the stored action and invalidate the token (single use)."""
        self._purge()
        action = self._pending.get(token)
        if action is None:
            raise SlurmMcpError("unknown or expired confirmation token: run the dry run again")
        if action.kind != kind:
            raise SlurmMcpError(f"this token is for a {action.kind} action, not {kind}")
        del self._pending[token]
        return action
