"""Configuration: everything cluster-specific lives here, nothing is hard-coded."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, PathError
from .paths import normalize_root, validate_remote_path
from .presets import Preset, parse_gpu_table

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
    max_array_size: int = 100
    array_concurrency: int = 40     # the %N throttle added to every array
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
