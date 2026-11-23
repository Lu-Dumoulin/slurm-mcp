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
MODULE_RE = re.compile(r"[A-Za-z0-9_./+-]{1,60}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
# Variables that change how the shell or loader behaves are never set from outside the script.
ENV_BLOCKED = {"PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV", "IFS", "SHELL", "HOME",
               "USER", "LOGNAME", "PS1", "PROMPT_COMMAND", "BASH_XTRACEFD", "CDPATH", "GLOBIGNORE"}
MAX_ENV = 20
MAX_ENV_VALUE = 2000


def check_env(env: Any) -> dict[str, str]:
    """Validated environment variables to export: names, values and a count limit."""
    if not isinstance(env, dict):
        raise SlurmMcpError("env must be an object of NAME: value")
    if len(env) > MAX_ENV:
        raise SlurmMcpError(f"env: at most {MAX_ENV} variables")
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            raise SlurmMcpError(f"env: invalid variable name {name!r}")
        if name in ENV_BLOCKED or name.startswith(("SLURM_", "BASH_FUNC")):
            raise SlurmMcpError(f"env: {name} cannot be set")
        if not isinstance(value, str) or len(value) > MAX_ENV_VALUE or any(c in value for c in "\n\r\0"):
            raise SlurmMcpError(f"env: the value of {name} must be a single-line string")
    return dict(env)


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
    max_cpus_per_gpu: int = 0        # GPU presets: cap on CPUs is this times the GPUs requested
    allowed_qos: tuple[str, ...] = ()        # qos names the caller may ask for
    allow_exclusive: bool = False
    modules: tuple[str, ...] = ()    # default `module load` lines
    env: tuple[tuple[str, str], ...] = ()   # default exported variables

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any], known_gpu_types: dict[str, str]) -> Preset:
        def bad(msg: str) -> ConfigError:
            return ConfigError(f"preset {name!r}: {msg}")

        if not PRESET_NAME_RE.fullmatch(name):
            raise bad("invalid name")
        allowed = {"partition", "time", "cpus", "mem", "max_time", "max_cpus", "max_mem",
                   "description", "gpu", "gpus", "max_gpus", "gpu_types", "max_cpus_per_gpu", "modules", "env", "allowed_qos", "allow_exclusive"}
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
        gpu = d.get("gpu", False)
        gpus = d.get("gpus", 1 if gpu else 0)
        max_gpus = d.get("max_gpus", gpus)
        per_gpu = d.get("max_cpus_per_gpu", 0)
        if gpu and "max_cpus" in d:
            raise bad("GPU presets use max_cpus_per_gpu, not max_cpus")
        if not gpu and per_gpu:
            raise bad("max_cpus_per_gpu only makes sense with gpu = true")
        if gpu and "max_cpus_per_gpu" not in d and isinstance(cpus, int) and gpus >= 1:
            per_gpu = -(-cpus // gpus)  # smallest cap that allows the default
        max_cpus = per_gpu * max_gpus if gpu and isinstance(per_gpu, int) else d.get("max_cpus", cpus)
        ints = (cpus, max_cpus, gpus, max_gpus, per_gpu)
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in ints):
            raise bad("cpus, max_cpus, gpus and max_gpus must be whole numbers")
        if cpus < 1 or gpus > max_gpus:
            raise bad("default exceeds its own max, or cpus < 1")
        if gpu and cpus > per_gpu * gpus or not gpu and cpus > max_cpus:
            raise bad("default cpus exceed their own cap")
        if not isinstance(gpu, bool):
            raise bad("gpu must be true or false")
        if gpu and gpus < 1:
            raise bad("a gpu preset needs gpus >= 1")
        if not gpu and (gpus or d.get("gpu_types")):
            raise bad("gpus and gpu_types only make sense with gpu = true")
        modules = tuple(d.get("modules", ()))
        if len(modules) > 10 or not all(isinstance(m, str) and MODULE_RE.fullmatch(m) for m in modules):
            raise bad("modules: at most 10 names made of letters, digits and ./+-_")
        try:
            env = tuple(check_env(d.get("env", {})).items())
        except SlurmMcpError as exc:
            raise bad(str(exc)) from None
        qos = tuple(d.get("allowed_qos", ()))
        if not all(isinstance(q, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", q) for q in qos):
            raise bad("allowed_qos must be a list of qos names")
        if not isinstance(d.get("allow_exclusive", False), bool):
            raise bad("allow_exclusive must be true or false")
        types = tuple(d.get("gpu_types", ()))
        unknown = [t for t in types if t not in known_gpu_types]
        if unknown:
            raise bad(f"gpu_types {unknown} are not in the top-level gpu_types table")
        return cls(name, d["partition"], d["time"], cpus, mem, d.get("max_time", d["time"]),
                   max_cpus, max_mem, str(d.get("description", "")), gpu, gpus, max_gpus, types, per_gpu, qos, bool(d.get("allow_exclusive", False)), modules, env)


def parse_gpu_table(table: Any) -> dict[str, str]:
    """Display name to Slurm feature, e.g. {"H100": "nvidia_h100_nvl"}."""
    if not isinstance(table, dict):
        raise ConfigError("gpu_types must be a table of name = feature")
    for name, feature in table.items():
        if not GPU_NAME_RE.fullmatch(name) or not isinstance(feature, str) or not FEATURE_RE.fullmatch(feature):
            raise ConfigError(f"invalid gpu_types entry: {name!r}")
    return dict(table)
