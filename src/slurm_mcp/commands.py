"""Pure construction of ssh/scp argument vectors and of the fixed remote commands.

Nothing here spawns a process. The remote side is always built from a fixed
template with each argument shell-quoted, so there is no way to inject an
arbitrary command through a tool parameter.
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
from pathlib import Path
from collections.abc import Sequence

from .config import Config
from .errors import SlurmMcpError

JOB_ID_RE = re.compile(r"\d{1,12}(_\d{1,9})?")
FIELD_SEP = "|"

SQUEUE_FIELDS = (
    ("job_id", "%i"), ("state", "%T"), ("partition", "%P"), ("elapsed", "%M"),
    ("time_limit", "%l"), ("nodes", "%D"), ("reason", "%R"), ("submit_time", "%V"),
    ("name", "%j"),  # last: free text, may contain the separator
)
SACCT_FIELDS = (
    "JobID", "State", "ExitCode", "Elapsed", "Start", "End", "Partition", "NodeList",
    "AllocCPUS", "ReqMem", "MaxRSS", "JobName",  # JobName last, same reason
)
SCONTROL_KEYS = (
    "JobId", "JobName", "JobState", "Reason", "RunTime", "TimeLimit", "SubmitTime",
    "StartTime", "EndTime", "Partition", "NodeList", "NumCPUs", "WorkDir", "Command",
    "StdOut", "StdErr", "ExitCode",
)


def cli_name() -> str:
    """How to invoke this tool in messages: the bare name if it is on PATH, else the full path."""
    if shutil.which("slurm-mcp"):
        return "slurm-mcp"
    return str(Path(sys.executable).parent / "slurm-mcp")


def validate_job_id(job_id: str) -> str:
    job_id = str(job_id).strip()
    if not JOB_ID_RE.fullmatch(job_id):
        raise SlurmMcpError(f"invalid job id: {job_id!r} (expected digits, optionally _arrayindex)")
    return job_id


# --- local side ---------------------------------------------------------------------

def target(cfg: Config) -> str:
    return f"{cfg.user}@{cfg.host}"


def ssh_options(cfg: Config, *, batch: bool = True) -> list[str]:
    """ControlMaster multiplexing: one login, reused by every later call.

    %C is a short fixed-length hash of (local host, remote host, port, user), which
    keeps the socket path under the OS limit.
    """
    opts = [
        "-o", f"ConnectTimeout={cfg.connect_timeout}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={cfg.control_dir}/%C",
        "-o", f"ControlPersist={cfg.control_persist}",
    ]
    if batch:
        opts += ["-o", "BatchMode=yes"]
    opts += list(cfg.extra_ssh_options)
    return opts


def ssh_argv(cfg: Config, remote_args: Sequence[str]) -> list[str]:
    return ["ssh", *ssh_options(cfg), target(cfg), shlex.join(remote_args)]


def scp_download_argv(cfg: Config, remote_path: str, local_path: str) -> list[str]:
    return ["scp", "-q", *ssh_options(cfg), "--", f"{target(cfg)}:{remote_path}", local_path]


def scp_upload_argv(cfg: Config, local_path: str, remote_path: str) -> list[str]:
    return ["scp", "-q", *ssh_options(cfg), "--", local_path, f"{target(cfg)}:{remote_path}"]


def login_argv(cfg: Config) -> list[str]:
    """Interactive: opens the master connection in the background after authentication."""
    return ["ssh", *ssh_options(cfg, batch=False), "-fN", target(cfg)]


def check_argv(cfg: Config) -> list[str]:
    return ["ssh", *ssh_options(cfg), "-O", "check", target(cfg)]


# --- remote side: fixed templates ---------------------------------------------------

def squeue_cmd() -> list[str]:
    fmt = FIELD_SEP.join(code for _, code in SQUEUE_FIELDS)
    return ["squeue", "--me", "--noheader", "-o", fmt]


def sacct_job_cmd(job_id: str) -> list[str]:
    return ["sacct", "-j", validate_job_id(job_id), "-P", "-n", f"--format={','.join(SACCT_FIELDS)}"]


def sacct_recent_cmd(days: int) -> list[str]:
    days = max(1, min(int(days), 30))
    return ["sacct", "-X", "-S", f"now-{days}days", "-P", "-n", f"--format={','.join(SACCT_FIELDS)}"]


def sacct_paths_cmd(job_id: str) -> list[str]:
    """Working directory and log file patterns of a job (available even after scontrol forgets it)."""
    return ["sacct", "-j", validate_job_id(job_id), "-X", "-P", "-n", "--format=WorkDir,StdOut,StdErr"]


def scontrol_cmd(job_id: str) -> list[str]:
    return ["scontrol", "show", "job", "-o", validate_job_id(job_id)]


def tail_cmd(path: str, lines: int) -> list[str]:
    return ["tail", "-n", str(int(lines)), "--", path]


def dir_links_cmd(home: str) -> list[str]:
    """Symlinks directly inside `home` that point to directories: prints name, then resolved target."""
    return ["find", home, "-maxdepth", "1", "-type", "l", "-xtype", "d",
            "-printf", "%f\\n", "-exec", "readlink", "-f", "--", "{}", ";"]


def partition_features_cmd(partition: str) -> list[str]:
    """Node features per partition, read-only. `partition` is validated by the preset pattern."""
    return ["sinfo", "-h", "-p", partition, "-o", f"%P{FIELD_SEP}%f"]


def test_file_cmd(path: str) -> list[str]:
    return ["test", "-f", path]


def sbatch_cmd(script_path: str, chdir: str | None = None) -> list[str]:
    """sbatch of one script, run from `chdir` (default: the script's own directory)."""
    directory = chdir or script_path.rsplit("/", 1)[0] or "/"
    return ["sbatch", "--parsable", f"--chdir={directory}", script_path]


def scancel_cmd(job_id: str) -> list[str]:
    return ["scancel", validate_job_id(job_id)]


def test_dir_cmd(path: str) -> list[str]:
    return ["test", "-d", path]


def mkdir_cmd(path: str) -> list[str]:
    return ["mkdir", "-p", "--", path]


def head_bytes_cmd(path: str, nbytes: int) -> list[str]:
    return ["head", "-c", str(int(nbytes)), "--", path]


def find_cmd(directory: str, max_depth: int) -> list[str]:
    """Files and directories below `directory`, one `type<TAB>path<TAB>size<TAB>mtime` line each."""
    return ["find", directory, "-mindepth", "1", "-maxdepth", str(int(max_depth)),
            "(", "-type", "f", "-o", "-type", "d", ")", "-printf", "%y\\t%P\\t%s\\t%T@\\n"]
