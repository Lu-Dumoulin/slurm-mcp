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
    "Inspect jobs and result files on a Slurm cluster. All tools are read-only on the "
    "cluster. Paths must lie inside the configured roots. fetch_results copies files to "
    "the local download directory only; use dry_run=true first for large directories."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
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
    def tail_file(path: str, lines: int = 50) -> dict[str, Any]:
        """Last lines of a file under a configured root."""
        return cluster.tail_file(path, lines)

    @mcp.tool(annotations=READ_ONLY)
    @reportable
    def list_files(directory: str, max_depth: int = 1, pattern: str = "*") -> dict[str, Any]:
        """List files (path, size, mtime) under a directory. pattern is a shell glob such as '*.csv'."""
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

    return mcp
