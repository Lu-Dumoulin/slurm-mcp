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


def scp_upload_argv(cfg: Config, local_path: str, remote_path: str, *, preserve_times: bool = False) -> list[str]:
    flags = ["-q", "-p"] if preserve_times else ["-q"]
    return ["scp", *flags, *ssh_options(cfg), "--", local_path, f"{target(cfg)}:{remote_path}"]


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


def table_rows_cmd(path: str) -> list[str]:
    """Number of lines of a file; the awk program is a fixed constant."""
    return ["awk", "END{print NR}", path]


def df_cmd(path: str) -> list[str]:
    return ["df", "-P", "-k", path]


def du_cmd(path: str) -> list[str]:
    """Total size of a tree: summary only, one filesystem, symlinks not followed."""
    return ["du", "-s", "-k", "-x", "-P", path]


def sha256_cmd(path: str) -> list[str]:
    return ["sha256sum", path]


def test_file_cmd(path: str) -> list[str]:
    return ["test", "-f", path]


def partitions_cmd() -> list[str]:
    return ["sinfo", "-h", "-S", "P", "-o", f"%P{FIELD_SEP}%l{FIELD_SEP}%D{FIELD_SEP}%c{FIELD_SEP}%m{FIELD_SEP}%G"]


def slurm_config_cmd() -> list[str]:
    return ["scontrol", "show", "config"]


LIMIT_KEYS = ("MaxArraySize", "MaxJobCount", "MinJobAge")


def sbatch_cmd(script_path: str, chdir: str | None = None, extra_flags: list[str] | None = None) -> list[str]:
    """sbatch of one script, run from `chdir` (default: the script's own directory). `extra_flags` come
    from sbatchopts and are already validated."""
    directory = chdir or script_path.rsplit("/", 1)[0] or "/"
    return ["sbatch", "--parsable", f"--chdir={directory}", *(extra_flags or []), script_path]


def scontrol_hold_cmd(job_id: str, hold: bool) -> list[str]:
    return ["scontrol", "hold" if hold else "release", validate_job_id(job_id)]


def scancel_cmd(job_id: str) -> list[str]:
    return ["scancel", validate_job_id(job_id)]


def test_dir_cmd(path: str) -> list[str]:
    return ["test", "-d", path]


def realpath_cmd(path: str) -> list[str]:
    """Resolve symlinks; -m tolerates path components that do not exist yet."""
    return ["realpath", "-m", "--", path]


PART_SUFFIX = ".slurm-mcp-part"


def mv_cmd(src: str, dst: str) -> list[str]:
    return ["mv", "-f", "--", src, dst]


def rm_part_cmd(path: str) -> list[str]:
    """Remove a temporary upload file. Only names made by this server (suffix PART_SUFFIX) are accepted."""
    if not path.endswith(PART_SUFFIX):
        raise SlurmMcpError(f"refusing to remove {path}: not a temporary upload file")
    return ["rm", "-f", "--", path]


def mkdir_cmd(path: str) -> list[str]:
    return ["mkdir", "-p", "--", path]


def mkdir_many_cmd(paths: list[str]) -> list[str]:
    return ["mkdir", "-p", "--", *paths]


def sed_lines_cmd(path: str, first: int, last: int) -> list[str]:
    return ["sed", "-n", f"{int(first)},{int(last)}p", path]


def grep_cmd(path: str, pattern: str, *, regex: bool, ignore_case: bool, context: int, max_matches: int) -> list[str]:
    """grep with the pattern passed through -e, so it can never be read as an option."""
    args = ["grep", "-n", "-I", "-m", str(int(max_matches))]
    if context:
        args += ["-C", str(int(context))]
    if ignore_case:
        args.append("-i")
    args.append("-E" if regex else "-F")
    return args + ["-e", pattern, "--", path]


def scancel_many_cmd(job_ids: list[str], state: str | None = None) -> list[str]:
    """scancel of explicit job ids (never -u or a user name), optionally only in one state."""
    args = ["scancel"]
    if state:
        if state not in ("PENDING", "RUNNING"):
            raise SlurmMcpError(f"invalid state {state!r}")
        args.append(f"--state={state}")
    return args + [validate_job_id(j) for j in job_ids]


def test_exists_cmd(path: str) -> list[str]:
    return ["test", "-e", path]


def find_tree_cmd(path: str) -> list[str]:
    """Every entry below (and including) `path`, on one filesystem: type, size and path, each record ended by NUL
    so that a file name with a newline cannot hide or fake an entry."""
    return ["find", path, "-xdev", "-printf", "%y\\t%s\\t%p\\0"]


def rm_tree_cmd(path: str) -> list[str]:
    return ["rm", "-r", "--", path]


def head_bytes_cmd(path: str, nbytes: int) -> list[str]:
    return ["head", "-c", str(int(nbytes)), "--", path]


def find_cmd(directory: str, max_depth: int) -> list[str]:
    """Files and directories below `directory`, one `type<TAB>path<TAB>size<TAB>mtime` line each."""
    return ["find", directory, "-mindepth", "1", "-maxdepth", str(int(max_depth)),
            "(", "-type", "f", "-o", "-type", "d", ")", "-printf", "%y\\t%P\\t%s\\t%T@\\n"]
