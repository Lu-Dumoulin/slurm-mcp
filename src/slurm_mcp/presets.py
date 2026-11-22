"""Resource presets and unit parsing.

A preset fixes partition, time, CPUs, memory (and GPUs) and carries hard caps. The model
chooses a preset by name and may override values only up to the caps. Partition names
and GPU features come from the user's config, nothing cluster-specific lives in the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError, SlurmMcpError

TIME_RE = re.compile(r"(?:(\d+)-)?(\d{1,2}):(\d{2}):(\d{2})")
MEM_RE = re.compile(r"(\d+)([MG]?)")
PARTITION_RE = re.compile(r"[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*")
FEATURE_RE = re.compile(r"[A-Za-z0-9_.-]+")
GPU_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,30}")
PRESET_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,30}")


def parse_time(text: str) -> int:
    """`[D-]HH:MM:SS` to seconds (the form CommonUI uses, e.g. 0-12:00:00)."""
    m = TIME_RE.fullmatch(str(text))
    if not m:
        raise SlurmMcpError(f"invalid time {text!r}; use [D-]HH:MM:SS, e.g. 00:30:00 or 1-12:00:00")
    days, h, mi, s = (int(x or 0) for x in m.groups())
    if mi > 59 or s > 59:
        raise SlurmMcpError(f"invalid time {text!r}")
    return ((days * 24 + h) * 60 + mi) * 60 + s


def parse_mem_mb(text: str) -> int:
    """`3000`, `3000M` or `4G` to megabytes (Slurm convention: a bare number is MB)."""
    m = MEM_RE.fullmatch(str(text))
    if not m or int(m.group(1)) == 0:
        raise SlurmMcpError(f"invalid memory {text!r}; use e.g. 3000M or 4G")
    return int(m.group(1)) * (1024 if m.group(2) == "G" else 1)


def normalize_mem(text: str) -> str:
    m = MEM_RE.fullmatch(str(text))
    parse_mem_mb(text)
    return f"{m.group(1)}{m.group(2) or 'M'}"


def format_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    mi, s = divmod(rem, 60)
    return f"{days}-{h:02d}:{mi:02d}:{s:02d}" if days else f"{h:02d}:{mi:02d}:{s:02d}"


@dataclass(frozen=True)
class Preset:
    name: str
    partition: str
    time: str
    cpus: int
    mem: str
    max_time: str
    max_cpus: int
    max_mem: str
    description: str = ""
    gpu: bool = False
    gpus: int = 1
    max_gpus: int = 1
    gpu_types: tuple[str, ...] = ()  # default selection, names from the config's gpu_types table

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any], known_gpu_types: dict[str, str]) -> Preset:
        def bad(msg: str) -> ConfigError:
            return ConfigError(f"preset {name!r}: {msg}")

        if not PRESET_NAME_RE.fullmatch(name):
            raise bad("invalid name")
        allowed = {"partition", "time", "cpus", "mem", "max_time", "max_cpus", "max_mem",
                   "description", "gpu", "gpus", "max_gpus", "gpu_types"}
        if set(d) - allowed:
            raise bad(f"unknown keys {sorted(set(d) - allowed)}")
        for key in ("partition", "time", "mem"):
            if key not in d:
                raise bad(f"missing {key}")
        if not PARTITION_RE.fullmatch(str(d["partition"])):
            raise bad("invalid partition")
        try:
            secs = parse_time(d["time"])
            max_secs = parse_time(d.get("max_time", d["time"]))
            mem = normalize_mem(str(d["mem"]))
            max_mem = normalize_mem(str(d.get("max_mem", d["mem"])))
        except SlurmMcpError as exc:
            raise bad(str(exc)) from None
        if secs > max_secs or parse_mem_mb(mem) > parse_mem_mb(max_mem):
            raise bad("default exceeds its own max")
        cpus = d.get("cpus", 1)
        max_cpus = d.get("max_cpus", cpus)
        gpu = d.get("gpu", False)
        gpus = d.get("gpus", 1 if gpu else 0)
        max_gpus = d.get("max_gpus", gpus)
        ints = (cpus, max_cpus, gpus, max_gpus)
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in ints):
            raise bad("cpus, max_cpus, gpus and max_gpus must be whole numbers")
        if cpus < 1 or cpus > max_cpus or gpus > max_gpus:
            raise bad("default exceeds its own max, or cpus < 1")
        if not isinstance(gpu, bool):
            raise bad("gpu must be true or false")
        if gpu and gpus < 1:
            raise bad("a gpu preset needs gpus >= 1")
        if not gpu and (gpus or d.get("gpu_types")):
            raise bad("gpus and gpu_types only make sense with gpu = true")
        types = tuple(d.get("gpu_types", ()))
        unknown = [t for t in types if t not in known_gpu_types]
        if unknown:
            raise bad(f"gpu_types {unknown} are not in the top-level gpu_types table")
        return cls(name, d["partition"], d["time"], cpus, mem, d.get("max_time", d["time"]),
                   max_cpus, max_mem, str(d.get("description", "")), gpu, gpus, max_gpus, types)


def parse_gpu_table(table: Any) -> dict[str, str]:
    """Display name to Slurm feature, e.g. {"H100": "nvidia_h100_nvl"}."""
    if not isinstance(table, dict):
        raise ConfigError("gpu_types must be a table of name = feature")
    for name, feature in table.items():
        if not GPU_NAME_RE.fullmatch(name) or not isinstance(feature, str) or not FEATURE_RE.fullmatch(feature):
            raise ConfigError(f"invalid gpu_types entry: {name!r}")
    return dict(table)
