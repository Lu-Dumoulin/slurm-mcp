"""Generate an sbatch script from a preset and validated parameters.

`resolve_spec` decides (applies caps, validates every field) and `build_script` renders.
Both are pure, and the rendered text is pinned by a golden test, as in CommonUI. Every
value that reaches the script has passed a strict pattern or shell quoting, so the model
cannot add an #SBATCH line or a shell command outside the single `srun` line.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import SlurmMcpError
from .presets import (
    FEATURE_RE, Preset, format_time, parse_mem_mb, parse_time, normalize_mem,
)

JOB_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
MODULE_RE = re.compile(r"[A-Za-z0-9_./+-]{1,60}")
ARRAY_RE = re.compile(r"\d{1,6}(-\d{1,6})?(,\d{1,6}(-\d{1,6})?)*")
TASK_ID = "{task_id}"
MAX_ARGS = 50
MAX_ARG_LEN = 2000


@dataclass(frozen=True)
class JobSpec:
    preset: str
    job_name: str
    partition: str
    time: str
    cpus: int
    mem: str
    workdir: str
    command: tuple[str, ...]
    gpu: bool = False
    gpus: int = 0
    gpu_types: tuple[str, ...] = ()
    constraint: str | None = None
    array: str | None = None
    array_tasks: int = 0
    array_concurrency: int = 40
    modules: tuple[str, ...] = ()


def count_array_tasks(spec: str) -> int:
    if not ARRAY_RE.fullmatch(spec):
        raise SlurmMcpError(f"invalid array {spec!r}; use e.g. 1-10 or 1,3,5-7")
    total = 0
    for item in spec.split(","):
        lo, _, hi = item.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        if hi_i < lo_i:
            raise SlurmMcpError(f"invalid array range {item!r}")
        total += hi_i - lo_i + 1
    return total


def resolve_spec(
    preset: Preset,
    gpu_table: dict[str, str],
    *,
    gpu: bool,
    command: Sequence[str],
    workdir: str,
    job_name: str,
    gpu_types: Sequence[str] | None = None,
    time: str | None = None,
    cpus: int | None = None,
    mem: str | None = None,
    gpus: int | None = None,
    array: str | None = None,
    modules: Sequence[str] | None = None,
    max_array_size: int = 100,
    array_concurrency: int = 40,
) -> JobSpec:
    if not isinstance(gpu, bool):
        raise SlurmMcpError("gpu must be true or false: ask the user whether the job needs a GPU")
    if gpu != preset.gpu:
        kind = "a GPU" if preset.gpu else "a CPU-only"
        raise SlurmMcpError(f"preset {preset.name!r} is {kind} preset, which does not match gpu={str(gpu).lower()}")
    if not JOB_NAME_RE.fullmatch(job_name or ""):
        raise SlurmMcpError("job_name must be 1 to 40 characters of letters, digits, '_', '.' or '-'")

    use_time = time or preset.time
    if parse_time(use_time) > parse_time(preset.max_time):
        raise SlurmMcpError(f"time {use_time} exceeds the cap {preset.max_time} of preset {preset.name!r}")
    use_cpus = preset.cpus if cpus is None else cpus
    if not isinstance(use_cpus, int) or isinstance(use_cpus, bool) or not 1 <= use_cpus <= preset.max_cpus:
        raise SlurmMcpError(f"cpus must be between 1 and {preset.max_cpus} for preset {preset.name!r}")
    use_mem = normalize_mem(mem) if mem else preset.mem
    if parse_mem_mb(use_mem) > parse_mem_mb(preset.max_mem):
        raise SlurmMcpError(f"mem {use_mem} exceeds the cap {preset.max_mem} of preset {preset.name!r}")

    use_gpus, names, constraint = 0, (), None
    if gpu:
        use_gpus = preset.gpus if gpus is None else gpus
        if not isinstance(use_gpus, int) or isinstance(use_gpus, bool) or not 1 <= use_gpus <= preset.max_gpus:
            raise SlurmMcpError(f"gpus must be between 1 and {preset.max_gpus} for preset {preset.name!r}")
        names = tuple(gpu_types) if gpu_types else preset.gpu_types
        unknown = [n for n in names if n not in gpu_table]
        if unknown:
            raise SlurmMcpError(f"unknown GPU type(s) {unknown}; choose from {sorted(gpu_table)}")
        if names:
            constraint = "|".join(gpu_table[n] for n in names)
            assert FEATURE_RE.fullmatch(constraint.replace("|", ""))  # config-validated
    elif gpu_types or gpus:
        raise SlurmMcpError("gpu_types and gpus only apply when gpu is true")

    args = tuple(command)
    if not args or len(args) > MAX_ARGS:
        raise SlurmMcpError(f"command must be a list of 1 to {MAX_ARGS} arguments")
    for a in args:
        if not isinstance(a, str) or not a or len(a) > MAX_ARG_LEN or "\n" in a or "\r" in a or "\0" in a:
            raise SlurmMcpError("each command argument must be a non-empty single-line string")

    tasks = 0
    if array:
        tasks = count_array_tasks(array)
        if tasks > max_array_size:
            raise SlurmMcpError(f"array has {tasks} tasks, the limit is {max_array_size}")
    elif any(TASK_ID in a for a in args):
        raise SlurmMcpError(f"{TASK_ID} in the command needs an array")

    mods = tuple(modules or ())
    if len(mods) > 10 or not all(MODULE_RE.fullmatch(m) for m in mods):
        raise SlurmMcpError("modules: at most 10 names made of letters, digits and ./+-_")

    return JobSpec(preset.name, job_name, preset.partition, use_time, use_cpus, use_mem, workdir,
                   args, gpu, use_gpus, names, constraint, array or None, tasks, array_concurrency, mods)


def quote_arg(arg: str) -> str:
    """shlex quoting, except {task_id} becomes the array task id (expanded by the shell)."""
    pieces = [shlex.quote(p) if p else "" for p in arg.split(TASK_ID)]
    return '"${SLURM_ARRAY_TASK_ID}"'.join(pieces)


def build_script(spec: JobSpec) -> str:
    out_file = "%x-%A_%a.out" if spec.array else "%x-%j.out"
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={spec.job_name}",
        f"#SBATCH --partition={spec.partition}",
        f"#SBATCH --time={spec.time}",
        f"#SBATCH --cpus-per-task={spec.cpus}",
        f"#SBATCH --mem={spec.mem}",
        f"#SBATCH --output={out_file}",
    ]
    if spec.array:
        lines.append(f"#SBATCH --array={spec.array}%{spec.array_concurrency}")
    if spec.gpu:
        lines.append(f"#SBATCH --gpus={spec.gpus}")
        if spec.constraint:
            lines.append(f"#SBATCH --constraint={spec.constraint}")
    lines.append("")
    lines += [f"module load {m}" for m in spec.modules]
    if spec.modules:
        lines.append("")
    lines.append(f"cd {shlex.quote(spec.workdir)} || exit 1")
    lines.append("srun " + " ".join(quote_arg(a) for a in spec.command))
    return "\n".join(lines) + "\n"


def summarize(spec: JobSpec) -> str:
    tasks = spec.array_tasks or 1
    gpu = (f"GPU: yes, {spec.gpus} x {'|'.join(spec.gpu_types) or 'any'}" if spec.gpu else "GPU: no")
    return (f"{tasks} task(s), {spec.cpus} CPU(s) each, up to {format_time(parse_time(spec.time))} each, "
            f"{spec.mem} each, partition {spec.partition}, {gpu}")
