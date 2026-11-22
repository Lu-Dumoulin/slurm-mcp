"""Test doubles. No test in this suite touches the network or needs ssh keys."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from slurm_mcp.config import Config
from slurm_mcp.runner import Result

Handler = Callable[[Sequence[str]], Result | None]


class FakeRunner:
    """Scripted Runner. `ssh_replies` maps a substring of the remote command to
    (stdout, returncode). `remote_files` maps a remote path to (content, mtime) for scp."""

    def __init__(self, ssh_replies: dict[str, tuple[str, int]] | None = None,
                 remote_files: dict[str, tuple[bytes, float]] | None = None):
        self.ssh_replies = ssh_replies or {}
        self.remote_files = remote_files or {}
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], timeout: float) -> Result:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "scp":
            return self._scp(argv)
        remote = argv[-1]
        for needle, (out, rc) in self.ssh_replies.items():
            if needle in remote:
                return Result(tuple(argv), rc, out, "" if rc == 0 else "boom")
        return Result(tuple(argv), 1, "", f"no scripted reply for: {remote}")

    def _scp(self, argv: list[str]) -> Result:
        remote_path = argv[-2].split(":", 1)[1]
        local = Path(argv[-1])
        if remote_path not in self.remote_files:
            return Result(tuple(argv), 1, "", "No such file")
        local.write_bytes(self.remote_files[remote_path][0])
        return Result(tuple(argv), 0)

    def remote_commands(self) -> list[str]:
        return [c[-1] for c in self.calls if c[0] == "ssh"]


def make_config(tmp_path: Path, **over) -> Config:
    base = dict(
        host="login.example.org", user="alice",
        remote_roots=("/scratch/alice",), local_dir=tmp_path / "dl",
        control_dir=tmp_path / "cm",
    )
    base.update(over)
    return Config(**base)
