"""A short whitelist of extra sbatch options, validated.

dependency, qos, exclusive, hold and mail-type. The mail address is never taken from the
caller: it is the one `mail_user` in the config, so a job cannot send mail to anyone else.
qos and exclusive are only allowed where the preset or the config says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import SlurmMcpError

DEPENDENCY_RE = re.compile(r"singleton|(after|afterok|afternotok|afterany|aftercorr):\d{1,12}(:\d{1,12})*")
QOS_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
MAIL_TYPES = {"BEGIN", "END", "FAIL", "REQUEUE", "ALL", "TIME_LIMIT", "TIME_LIMIT_50", "TIME_LIMIT_80", "TIME_LIMIT_90"}
MAIL_USER_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class SbatchOptions:
    dependency: str | None = None
    qos: str | None = None
    exclusive: bool = False
    hold: bool = False
    mail_type: tuple[str, ...] = ()
    mail_user: str | None = None

    def flags(self) -> list[str]:
        out = []
        if self.dependency:
            out.append(f"--dependency={self.dependency}")
        if self.qos:
            out.append(f"--qos={self.qos}")
        if self.exclusive:
            out.append("--exclusive")
        if self.hold:
            out.append("--hold")
        if self.mail_type:
            out += [f"--mail-type={','.join(self.mail_type)}", f"--mail-user={self.mail_user}"]
        return out

    def header(self) -> list[str]:
        return [f"#SBATCH {f}" for f in self.flags()]

    def describe(self) -> str:
        parts = []
        if self.dependency:
            parts.append(f"dependency {self.dependency}")
        if self.qos:
            parts.append(f"qos {self.qos}")
        if self.exclusive:
            parts.append("exclusive node")
        if self.hold:
            parts.append("submitted on hold")
        if self.mail_type:
            parts.append(f"mail on {','.join(self.mail_type)}")
        return ", ".join(parts)


def resolve_options(
    *,
    dependency: str | None = None,
    qos: str | None = None,
    exclusive: bool = False,
    hold: bool = False,
    mail_type: list[str] | None = None,
    allowed_qos: tuple[str, ...] = (),
    allow_exclusive: bool = False,
    mail_user: str | None = None,
) -> SbatchOptions:
    if dependency is not None and not DEPENDENCY_RE.fullmatch(dependency):
        raise SlurmMcpError("dependency must look like afterok:123 or afterany:123:124 or singleton "
                            "(after, afterok, afternotok, afterany, aftercorr, then numeric job ids)")
    if qos is not None:
        if not QOS_RE.fullmatch(qos):
            raise SlurmMcpError("invalid qos name")
        if qos not in allowed_qos:
            raise SlurmMcpError(f"qos {qos!r} is not allowed here; allowed: {list(allowed_qos) or 'none'}")
    if exclusive and not allow_exclusive:
        raise SlurmMcpError("exclusive nodes are not allowed for this preset or configuration")
    types: tuple[str, ...] = ()
    if mail_type:
        bad = [t for t in mail_type if t not in MAIL_TYPES]
        if bad:
            raise SlurmMcpError(f"invalid mail_type {bad}; choose from {sorted(MAIL_TYPES)}")
        if not mail_user:
            raise SlurmMcpError("mail_type needs a mail_user in the config (the address is never taken from a tool call)")
        types = tuple(dict.fromkeys(mail_type))
    return SbatchOptions(dependency, qos, bool(exclusive), bool(hold), types, mail_user if types else None)
