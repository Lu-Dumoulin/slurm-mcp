"""Process-execution seam. Code that spawns ssh/scp depends on `Runner`, never on
subprocess directly, so the whole SSH layer can be tested without a cluster."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .errors import RemoteError


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(self, argv: Sequence[str], timeout: float) -> Result: ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], timeout: float) -> Result:
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RemoteError(f"command timed out after {timeout:g}s") from None
        except FileNotFoundError as exc:
            raise RemoteError(f"executable not found: {exc.filename}") from None
        return Result(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
