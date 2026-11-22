"""MCP server wiring. Phase 1: every tool is read-only on the cluster."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .cluster import Cluster
from .errors import SlurmMcpError

INSTRUCTIONS = (
    "Inspect jobs and result files on a Slurm cluster. The inspection tools are read-only on "
    "the cluster. Paths must lie inside the configured roots. fetch_results copies files to "
    "the local download directory only; use dry_run=true first for large directories. "
    "If job tools are enabled, submitting or cancelling is always two steps: a dry run, then "
    "the real call with its token, and only after the user has agreed to the shown command."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
REMOTE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
REMOTE_CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                              openWorldHint=False)


def reportable(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn our own errors into ToolError so the model sees the message and can correct itself."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SlurmMcpError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(cluster: Cluster) -> MCPServer:
    mcp = MCPServer("slurm-mcp", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def queue() -> list[dict[str, str]]:
        """List your own pending and running jobs (squeue --me)."""
        return cluster.queue()

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def recent_jobs(days: int = 3) -> list[dict[str, str]]:
        """List your jobs from the last N days (1 to 30), including finished ones (sacct)."""
        return cluster.recent_jobs(days)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def job_status(job_id: str) -> dict[str, Any]:
        """State, exit code, timings and resources of one job (sacct), plus live details (scontrol) while Slurm still knows it."""
        return cluster.job_status(job_id)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def job_log(job_id: str, stream: str = "out", lines: int = 50) -> dict[str, Any]:
        """Last lines of a job's stdout ('out') or stderr ('err'). Falls back to tail_file when Slurm no longer knows the log path."""
        return cluster.job_log(job_id, stream, lines)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def diagnose_job(job_id: str, lines: int = 30) -> dict[str, Any]:
        """Explain why a job failed, timed out, is pending or ran fine: state, exit code, memory used versus requested, plain-language findings, and the end of its stderr and stdout. Use this first when a job misbehaves."""
        return cluster.diagnose_job(job_id, lines)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def tail_file(path: str, lines: int = 50) -> dict[str, Any]:
        """Last lines of a file under a configured root."""
        return cluster.tail_file(path, lines)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def list_files(directory: str, max_depth: int = 1, pattern: str = "*") -> dict[str, Any]:
        """List files and subdirectories (path, type, size, mtime) under a directory. pattern is a shell glob such as '*.csv'."""
        return cluster.list_files(directory, max_depth, pattern)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def read_parameter_table(path: str | None = None, max_rows: int = 200) -> dict[str, Any]:
        """Read a CSV parameter table (such as sim/DF.csv) from the cluster. Defaults to the configured df_csv."""
        return cluster.read_parameter_table(path, max_rows)

    @mcp.tool(annotations=LOCAL_WRITE)
    @reportable
    def fetch_results(directory: str, pattern: str = "*", max_depth: int = 3, dry_run: bool = False) -> dict[str, Any]:
        """Download missing or newer files from a remote directory into the local download directory. Never modifies the cluster. Use dry_run=true to preview."""
        return cluster.fetch_results(directory, pattern, max_depth, dry_run)

    if cluster.cfg.enable_writes:
        _add_write_tools(mcp, cluster)
    return mcp


def _add_write_tools(mcp: MCPServer, cluster: Cluster) -> None:
    """Only registered when enable_writes is true. Each change is a dry run plus a token."""

    @mcp.tool(annotations=READ_ONLY)  # changes nothing on the cluster, only stores a token in memory
    @reportable
    def submit_job_dry_run(script_path: str) -> dict[str, Any]:
        """Step 1 of 2. Check an existing batch script under a configured root and return the exact sbatch command, a preview of the script and a single-use confirmation token. Nothing is submitted."""
        return cluster.plan_submit(script_path)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def list_presets() -> dict[str, Any]:
        """Resource presets (for example smoke, debug, standard, gpu), their defaults and hard caps, and the GPU types that can be requested. Call this before submit_preset_dry_run and use it to ask the user real choices."""
        return cluster.describe_presets()

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def submit_preset_dry_run(
        preset: str,
        gpu: bool,
        command: list[str],
        workdir: str,
        job_name: str,
        gpu_types: list[str] | None = None,
        time: str | None = None,
        cpus: int | None = None,
        mem: str | None = None,
        gpus: int | None = None,
        array: str | None = None,
        modules: list[str] | None = None,
    ) -> dict[str, Any]:
        """Step 1 of 2. Generate an sbatch script from a preset and show it in full, with a cost summary and a single-use confirmation token. Nothing is written or submitted.

        `gpu` is required and must come from the user: ask "does this job need a GPU?" if they have not said. Never guess. If yes, ask which GPU type(s) from list_presets; gpu_types may be omitted only if the user has no preference (the preset default is then used and shown). `command` is one argument per list item, for example ["julia", "--optimize=3", "main.jl"]; it is quoted, not run through a shell. "{task_id}" inside an argument becomes the array task id (needs `array`, e.g. "1-10"). Values above the preset caps are refused. Present the summary and the full script to the user before calling submit_job."""
        return cluster.plan_submit_preset(preset, gpu, command, workdir, job_name, gpu_types, time, cpus,
                                          mem, gpus, array, modules)

    @mcp.tool(annotations=REMOTE_WRITE)
    @reportable
    def submit_job(confirmation_token: str) -> dict[str, Any]:
        """Step 2 of 2. Submit the job shown by submit_job_dry_run or submit_preset_dry_run. For a generated script this first writes the script under the jobs directory, then runs sbatch. Needs the token; ask the user to confirm first."""
        return cluster.submit(confirmation_token)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def cancel_job_dry_run(job_id: str) -> dict[str, Any]:
        """Step 1 of 2. Show which of your own pending or running jobs scancel would cancel, with a single-use confirmation token. Nothing is cancelled."""
        return cluster.plan_cancel(job_id)

    @mcp.tool(annotations=REMOTE_CANCEL)
    @reportable
    def cancel_job(confirmation_token: str) -> dict[str, Any]:
        """Step 2 of 2. Cancel the jobs shown by cancel_job_dry_run. Needs its token; ask the user to confirm first."""
        return cluster.cancel(confirmation_token)
