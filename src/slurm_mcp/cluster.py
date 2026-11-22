"""Read-only operations on the cluster. Depends on a Runner, never on subprocess."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any

from . import commands as cmd
from .config import Config
from .errors import RemoteError, SlurmMcpError
from .parsing import parse_find, parse_sacct, parse_scontrol, parse_squeue
from .paths import is_safe_relative, validate_remote_path
from .runner import Runner
from .sync import matches, plan_sync, scan_local

_MAX_TABLE_BYTES = 1_000_000


class Cluster:
    def __init__(self, cfg: Config, runner: Runner):
        self.cfg = cfg
        self.runner = runner

    # --- plumbing ---------------------------------------------------------------
    def _ssh(self, remote_args: list[str], *, allow_failure: bool = False) -> str | None:
        res = self.runner.run(cmd.ssh_argv(self.cfg, remote_args), self.cfg.command_timeout)
        if res.returncode == 0:
            return res.stdout
        if allow_failure:
            return None
        if res.returncode == 255:
            raise RemoteError(
                "SSH connection failed. The shared connection may be closed: "
                "run `slurm-mcp login` in a terminal, then retry. "
                f"ssh said: {res.stderr.strip()[:300]}"
            )
        raise RemoteError(f"remote command failed (exit {res.returncode}): {res.stderr.strip()[:500]}")

    def _path(self, path: str) -> str:
        return validate_remote_path(path, self.cfg.remote_roots)

    def _clamp_lines(self, lines: int) -> int:
        return max(1, min(int(lines), self.cfg.max_tail_lines))

    # --- jobs -------------------------------------------------------------------
    def queue(self) -> list[dict[str, str]]:
        return parse_squeue(self._ssh(cmd.squeue_cmd()) or "")

    def recent_jobs(self, days: int = 3) -> list[dict[str, str]]:
        return parse_sacct(self._ssh(cmd.sacct_recent_cmd(days)) or "")

    def job_status(self, job_id: str) -> dict[str, Any]:
        job_id = cmd.validate_job_id(job_id)
        accounting = parse_sacct(self._ssh(cmd.sacct_job_cmd(job_id)) or "")
        live = self._ssh(cmd.scontrol_cmd(job_id), allow_failure=True)  # unknown once purged
        details = parse_scontrol(live) if live else None
        if details:
            details = {k: v for k, v in details.items() if k in cmd.SCONTROL_KEYS}
        if not accounting and not details:
            raise SlurmMcpError(f"job {job_id} not found")
        return {"job_id": job_id, "accounting": accounting, "scontrol": details}

    def job_log(self, job_id: str, stream: str = "out", lines: int = 50) -> dict[str, Any]:
        if stream not in ("out", "err"):
            raise SlurmMcpError("stream must be 'out' or 'err'")
        status = self.job_status(job_id)
        info = status["scontrol"] or {}
        key = "StdOut" if stream == "out" else "StdErr"
        path = info.get(key)
        if not path:
            raise SlurmMcpError(
                f"log path for job {job_id} is no longer known to Slurm; use tail_file with the path"
            )
        return self.tail_file(path, lines)

    def tail_file(self, path: str, lines: int = 50) -> dict[str, Any]:
        path = self._path(path)
        n = self._clamp_lines(lines)
        text = self._ssh(cmd.tail_cmd(path, n)) or ""
        return {"path": path, "lines_requested": n, "text": text}

    # --- files ------------------------------------------------------------------
    def _list(self, directory: str, max_depth: int):
        directory = self._path(directory)
        depth = max(1, min(int(max_depth), 6))
        return directory, parse_find(self._ssh(cmd.find_cmd(directory, depth)) or "")

    def list_files(self, directory: str, max_depth: int = 1, pattern: str = "*") -> dict[str, Any]:
        directory, files = self._list(directory, max_depth)
        files = [f for f in files if matches(f.rel, pattern)]
        files.sort(key=lambda f: f.rel)
        limit = self.cfg.max_list_entries
        return {
            "directory": directory,
            "count": len(files),
            "truncated": len(files) > limit,
            "files": [{"path": f.rel, "size": f.size, "mtime": f.mtime} for f in files[:limit]],
        }

    def read_parameter_table(self, path: str | None = None, max_rows: int = 200) -> dict[str, Any]:
        path = path or self.cfg.df_csv
        if not path:
            raise SlurmMcpError("no path given and no df_csv set in the config")
        path = self._path(path)
        text = self._ssh(cmd.head_bytes_cmd(path, _MAX_TABLE_BYTES)) or ""
        rows = list(csv.DictReader(io.StringIO(text)))
        truncated = len(rows) > max_rows or len(text.encode()) >= _MAX_TABLE_BYTES
        return {
            "path": path,
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows[:max_rows],
            "truncated": truncated,
        }

    def fetch_results(
        self, directory: str, pattern: str = "*", max_depth: int = 3, dry_run: bool = False
    ) -> dict[str, Any]:
        directory, files = self._list(directory, max_depth)
        dest_root = self.cfg.local_dir / directory.lstrip("/")
        safe = [f for f in files if is_safe_relative(f.rel)]
        plan = plan_sync(safe, scan_local(dest_root), pattern)
        report: dict[str, Any] = {
            "remote_dir": directory,
            "local_dir": str(dest_root),
            "to_download": [{"path": p.file.rel, "size": p.file.size, "reason": p.reason} for p in plan.download],
            "up_to_date": len(plan.up_to_date),
            "skipped_unsafe_names": len(files) - len(safe),
            "total_bytes": plan.total_bytes,
            "dry_run": dry_run,
        }
        if dry_run or not plan.download:
            report["downloaded"] = []
            return report
        cap = self.cfg.max_download_mb * 1024 * 1024
        if plan.total_bytes > cap:
            raise SlurmMcpError(
                f"refusing to download {plan.total_bytes} bytes (limit {cap}); "
                "narrow the pattern or raise max_download_mb"
            )
        done: list[str] = []
        failed: list[dict[str, str]] = []
        for item in plan.download:
            dest = dest_root / item.file.rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            res = self.runner.run(
                cmd.scp_download_argv(self.cfg, f"{directory}/{item.file.rel}", str(part)),
                self.cfg.command_timeout * 10,
            )
            if res.returncode != 0:
                part.unlink(missing_ok=True)
                failed.append({"path": item.file.rel, "error": res.stderr.strip()[:200]})
                continue
            os.replace(part, dest)
            os.utime(dest, (item.file.mtime, item.file.mtime))  # makes the next comparison exact
            done.append(item.file.rel)
        report["downloaded"] = done
        report["failed"] = failed
        return report
