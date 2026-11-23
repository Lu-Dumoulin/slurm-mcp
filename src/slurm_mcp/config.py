"""Configuration: everything cluster-specific lives here, nothing is hard-coded."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, PathError
from .paths import broad_remote_root_problem, normalize_root, validate_remote_path
from .presets import Preset, parse_gpu_table
from .sbatchopts import MAIL_USER_RE, QOS_RE
from .uploads import broad_folder_problem

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DEFAULT_CONFIG_PATH = Path("~/.config/slurm-mcp/config.toml")
ENV_VAR = "SLURM_MCP_CONFIG"


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    remote_roots: tuple[str, ...]
    local_dir: Path
    df_csv: str | None = None
    control_dir: Path = Path("~/.ssh/slurm-mcp-cm").expanduser()
    control_persist: str = "10m"
    connect_timeout: int = 15
    command_timeout: int = 60
    max_tail_lines: int = 500
    max_list_entries: int = 1000
    max_download_mb: int = 500
    extra_ssh_options: tuple[str, ...] = ()
    auto_connect: bool = True       # open the shared SSH connection non-interactively when needed
    enable_writes: bool = False     # expose submit/cancel tools (off by default)
    confirmation_ttl_s: int = 300   # lifetime of a confirmation token
    jobs_dir: str | None = None     # where generated scripts are written (default: first root + /slurm-mcp-jobs)
    max_array_size: int = 10000     # Baobab's Slurm MaxArraySize is 10000: indices 0 to 9999
    array_concurrency: int = 40     # the %N throttle added to every array
    local_roots: tuple[Path, ...] = ()   # local folders that may be uploaded (empty: uploading is off)
    upload_exclude: tuple[str, ...] = ()        # extra name patterns never uploaded, on top of the built-in list
    sensitive_extra: tuple[str, ...] = ()       # extra name patterns never read or downloaded, on top of the built-in list
    upload_from_cwd: bool = True                # also allow the folder the server was started in (the agent's project)
    warn_queued_jobs: int = 1000                # a submission that brings the queue above this gets a warning, also in the approval text
    max_queued_jobs: int = 10000                # refuse a submission that would exceed this many queued jobs and array tasks
    large_file_mb: int = 10                     # files above this need an explicit confirmation to upload
    max_upload_files: int = 5000                # sanity limit on the number of files, not on sizes
    allowed_qos: tuple[str, ...] = ()                   # qos names allowed on submit_job_dry_run (existing scripts)
    allow_exclusive: bool = False
    mail_user: str | None = None                        # the only address job mail can go to
    quota_commands: tuple[tuple[str, ...], ...] = ()   # fixed commands whose output storage_info returns as is
    du_timeout_s: int = 120
    gpu_types: dict[str, str] = field(default_factory=dict)   # display name -> Slurm feature
    presets: dict[str, Preset] = field(default_factory=dict)

    @property
    def scripts_dir(self) -> str:
        return self.jobs_dir or f"{self.remote_roots[0].rstrip('/')}/slurm-mcp-jobs"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        for key in ("host", "user", "remote_roots", "local_dir"):
            if key not in data:
                raise ConfigError(f"missing required config key: {key}")
        for key in ("host", "user"):
            if not isinstance(data[key], str) or not _NAME_RE.fullmatch(data[key]):
                raise ConfigError(f"invalid {key}: {data[key]!r}")
        for key in ("auto_connect", "enable_writes"):
            if key in data and not isinstance(data[key], bool):
                raise ConfigError(f"{key} must be true or false")
        roots = data["remote_roots"]
        if not isinstance(roots, list) or not roots:
            raise ConfigError("remote_roots must be a non-empty list")
        kwargs: dict[str, Any] = dict(data)
        try:
            kwargs["remote_roots"] = tuple(normalize_root(r) for r in roots)
        except PathError as exc:
            raise ConfigError(f"invalid remote_roots entry: {exc}") from None
        for r in kwargs["remote_roots"]:
            problem = broad_remote_root_problem(r)
            if problem:
                raise ConfigError(f"remote_roots entry {r} is too broad: {problem}")
        kwargs["local_dir"] = Path(data["local_dir"]).expanduser()
        gpu_table = parse_gpu_table(data.get("gpu_types", {}))
        kwargs["gpu_types"] = gpu_table
        presets = data.get("presets", {})
        if not isinstance(presets, dict):
            raise ConfigError("presets must be a table of tables")
        kwargs["presets"] = {n: Preset.from_dict(n, d, gpu_table) for n, d in presets.items()}
        if "jobs_dir" in data:
            try:
                kwargs["jobs_dir"] = validate_remote_path(data["jobs_dir"], kwargs["remote_roots"])
            except PathError as exc:
                raise ConfigError(f"jobs_dir: {exc}") from None
        roots_in = data.get("local_roots", [])
        if not isinstance(roots_in, list) or not all(isinstance(r, str) and r for r in roots_in):
            raise ConfigError("local_roots must be a list of folder paths")
        expanded = [Path(r).expanduser() for r in roots_in]
        if any(not r.is_absolute() for r in expanded):
            raise ConfigError("local_roots must be absolute paths (a leading ~ is fine)")
        kwargs["local_roots"] = tuple(r.resolve() for r in expanded)
        home = Path.home().resolve()
        for r in kwargs["local_roots"]:
            problem = broad_folder_problem(r, home)
            if problem:
                raise ConfigError(f"local_roots entry {r} is too broad: {problem}")
        mu = data.get("mail_user")
        if mu is not None and (not isinstance(mu, str) or not MAIL_USER_RE.fullmatch(mu)):
            raise ConfigError("mail_user must be an email address")
        aq = data.get("allowed_qos", [])
        if not isinstance(aq, list) or not all(isinstance(q, str) and QOS_RE.fullmatch(q) for q in aq):
            raise ConfigError("allowed_qos must be a list of qos names")
        kwargs["allowed_qos"] = tuple(aq)
        if not isinstance(data.get("allow_exclusive", False), bool):
            raise ConfigError("allow_exclusive must be true or false")
        qc = data.get("quota_commands", [])
        if not isinstance(qc, list) or not all(isinstance(c, list) and c and all(isinstance(a, str) and a for a in c) for c in qc):
            raise ConfigError("quota_commands must be a list of commands, each a list of strings")
        kwargs["quota_commands"] = tuple(tuple(c) for c in qc)
        excl = data.get("upload_exclude", [])
        if not isinstance(excl, list) or not all(isinstance(e, str) and e for e in excl):
            raise ConfigError("upload_exclude must be a list of name patterns")
        kwargs["upload_exclude"] = tuple(excl)
        sx = data.get("sensitive_extra", [])
        if not isinstance(sx, list) or not all(isinstance(e, str) and e for e in sx):
            raise ConfigError("sensitive_extra must be a list of name patterns")
        kwargs["sensitive_extra"] = tuple(sx)
        if "control_dir" in data:
            kwargs["control_dir"] = Path(data["control_dir"]).expanduser()
        if "extra_ssh_options" in data:
            kwargs["extra_ssh_options"] = tuple(data["extra_ssh_options"])
        return cls(**kwargs)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    chosen = Path(path or os.environ.get(ENV_VAR) or DEFAULT_CONFIG_PATH).expanduser()
    try:
        with chosen.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {chosen}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {chosen}: {exc}") from None
    return Config.from_dict(data)
