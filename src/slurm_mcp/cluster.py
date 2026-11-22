"""Read-only operations on the cluster. Depends on a Runner, never on subprocess."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from . import commands as cmd
from .config import Config
from .confirm import ConfirmationStore
from .errors import RemoteError, RemoteFileNotFound, SlurmMcpError
from .parsing import parse_find, parse_sacct_paths, parse_partition_features, parse_sacct, parse_scontrol, parse_squeue
from .diagnose import clean_reason, diagnose, never_started, resolve_log_paths
from .jobscript import build_script, resolve_spec, summarize
from .paths import is_safe_relative, validate_remote_path
from .runner import Runner
from .sync import matches, plan_sync, scan_local

_MAX_TABLE_BYTES = 1_000_000


class Cluster:
    def __init__(self, cfg: Config, runner: Runner, confirmations: ConfirmationStore | None = None):
        self.cfg = cfg
        self.runner = runner
        self.confirmations = confirmations or ConfirmationStore(cfg.confirmation_ttl_s)

    # --- plumbing ---------------------------------------------------------------
    def _master_alive(self) -> bool:
        return self.runner.run(cmd.check_argv(self.cfg), 10).returncode == 0

    def _ssh(self, remote_args: list[str], *, allow_failure: bool = False) -> str | None:
        # auto_connect: the ControlMaster=auto option opens the shared connection by itself
        # when a key is available (BatchMode, no prompt). With auto_connect off, only an
        # already-open connection is used, so the server never logs in on its own.
        if not self.cfg.auto_connect and not self._master_alive():
            raise RemoteError(
                f"No open SSH connection and auto_connect is off. Run `{cmd.cli_name()} login` in a terminal, then retry."
            )
        res = self.runner.run(cmd.ssh_argv(self.cfg, remote_args), self.cfg.command_timeout)
        if res.returncode == 0:
            return res.stdout
        if allow_failure:
            return None
        if res.returncode == 255:
            hint = (" The key may not be loaded: try `ssh-add`, or" if "Permission denied" in res.stderr else "")
            raise RemoteError(
                "SSH connection failed and could not be opened without a prompt."
                f"{hint} Run `{cmd.cli_name()} login` in a terminal, then retry. "
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
            if details.get("JobState") == "PENDING" and not clean_reason(details.get("Reason")):
                # scontrol often says "None" for a job that has just been queued: ask squeue
                row = next((r for r in self.queue() if r["job_id"] == job_id), None)
                reason = clean_reason(row["reason"] if row else "")
                details.pop("Reason", None)
                if reason:
                    details["Reason"] = reason
        if not accounting and not details:
            raise SlurmMcpError(f"job {job_id} not found")
        return {"job_id": job_id, "accounting": accounting, "scontrol": details}

    def _log_paths(self, status: dict[str, Any]):
        """Where the job wrote its output, from scontrol while it is known and sacct afterwards."""
        job_id = status["job_id"]
        live = status["scontrol"]
        acct_paths = None
        if not (live and live.get("StdOut")):
            acct_paths = parse_sacct_paths(self._ssh(cmd.sacct_paths_cmd(job_id), allow_failure=True) or "")
        rows = status["accounting"]
        name = (live or {}).get("JobName") or (rows[0].get("jobname", "") if rows else "")
        return resolve_log_paths(live, acct_paths, job_id, name, self.cfg.user)

    def job_log(self, job_id: str, stream: str = "out", lines: int = 50) -> dict[str, Any]:
        if stream not in ("out", "err"):
            raise SlurmMcpError("stream must be 'out' or 'err'")
        status = self.job_status(job_id)
        logs = self._log_paths(status)
        path = logs.stdout if stream == "out" or logs.merged else logs.stderr
        if not path:
            raise SlurmMcpError(
                f"log path for job {job_id} is not known to Slurm; use tail_file with the path"
            )
        result = self.tail_file(path, lines)
        if stream == "err" and logs.merged:
            result["note"] = "stderr is merged into the stdout log, so this is the same file"
        return result

    def diagnose_job(self, job_id: str, lines: int = 30) -> dict[str, Any]:
        """Verdict from accounting data plus the end of stdout and stderr (read-only)."""
        status = self.job_status(job_id)
        verdict = diagnose(status["accounting"], status["scontrol"])
        if verdict["state"] == "PENDING" or never_started(status["accounting"]):
            text = ("the job has not started yet, so there are no logs yet" if verdict["state"] == "PENDING"
                    else "the job never started, so there are no logs")
            logs: dict[str, Any] = {"stdout": {"note": text}, "stderr": {"note": text}}
            return {"job_id": status["job_id"], **verdict, "logs": logs, "accounting": status["accounting"]}

        paths = self._log_paths(status)
        logs = {}
        if paths.stdout is None:
            logs["stdout"] = {"note": "log path not available from Slurm; use tail_file with the path"}
        else:
            logs["stdout"] = self._tail_or_note(paths.stdout, lines)
        if paths.merged:
            logs["stderr"] = {"merged_into_stdout": True,
                              "note": "stderr is merged into the stdout log (no separate --error file)"}
        elif paths.stderr is None:
            logs["stderr"] = {"note": "stderr log path no longer known to Slurm; use tail_file with the path"}
        else:
            logs["stderr"] = self._tail_or_note(paths.stderr, lines)
        return {"job_id": status["job_id"], **verdict, "logs": logs, "accounting": status["accounting"]}

    def _tail_or_note(self, path: str, lines: int) -> dict[str, Any]:
        try:
            return self.tail_file(path, lines)
        except RemoteFileNotFound:
            return {"path": path, "note": f"log file not found at {path}"}
        except SlurmMcpError as exc:  # keep the verdict even if one log cannot be read
            return {"path": path, "note": str(exc)}

    def tail_file(self, path: str, lines: int = 50) -> dict[str, Any]:
        path = self._path(path)
        n = self._clamp_lines(lines)
        res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.tail_cmd(path, n)), self.cfg.command_timeout)
        if res.returncode == 1 and "No such file" in res.stderr:
            raise RemoteFileNotFound(f"file not found at {path}")
        if res.returncode != 0:
            self._ssh(cmd.tail_cmd(path, n))  # re-raises with the standard SSH or remote error message
        return {"path": path, "lines_requested": n, "text": res.stdout}

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
            "files": [
                {"path": f.rel, "type": "file", "size": f.size, "mtime": f.mtime} if f.kind == "f"
                else {"path": f.rel, "type": "dir", "mtime": f.mtime}
                for f in files[:limit]
            ],
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
        files = [f for f in files if f.kind == "f"]
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

    # --- writes: submit and cancel, each in two steps ----------------------------------
    def _require_writes(self) -> None:
        if not self.cfg.enable_writes:
            raise SlurmMcpError("job submission and cancellation are disabled (set enable_writes = true in the config)")

    def _probe(self, remote_args: list[str]) -> bool:
        """True if the remote command exits 0, False if it exits 1; raises if SSH itself failed."""
        res = self.runner.run(cmd.ssh_argv(self.cfg, remote_args), self.cfg.command_timeout)
        if res.returncode == 255:
            self._ssh(["true"])  # raises the standard connection error
        return res.returncode == 0

    def _is_regular_file(self, path: str) -> bool:
        return self._probe(cmd.test_file_cmd(path))

    def plan_submit(self, script_path: str) -> dict[str, Any]:
        """Dry run: validate the script, return the exact command and a confirmation token."""
        self._require_writes()
        path = self._path(script_path)
        if not self._is_regular_file(path):
            raise SlurmMcpError(f"not a regular file on the cluster: {path}")
        preview = self._ssh(cmd.head_bytes_cmd(path, 2000)) or ""
        argv = cmd.sbatch_cmd(path)
        token = self.confirmations.issue("submit", argv, shlex.join(argv))
        return {
            "dry_run": True,
            "command": shlex.join(argv),
            "script": path,
            "script_preview": preview,
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the command and script to the user. Call submit_job with this token only after they agree.",
        }

    def describe_presets(self) -> dict[str, Any]:
        """What the model should offer the user: presets, their caps and the GPU choices."""
        self._require_writes()
        return {
            "presets": [
                {
                    "name": p.name, "description": p.description, "gpu": p.gpu,
                    "partition": p.partition,
                    "defaults": {"time": p.time, "cpus": p.cpus, "mem": p.mem,
                                 **({"gpus": p.gpus, "gpu_types": list(p.gpu_types)} if p.gpu else {})},
                    "caps": {"time": p.max_time, "mem": p.max_mem,
                             **({"gpus": p.max_gpus, "cpus_per_gpu": p.max_cpus_per_gpu} if p.gpu
                                else {"cpus": p.max_cpus})},
                }
                for p in self.cfg.presets.values()
            ],
            "gpu_types": sorted(self.cfg.gpu_types),
            "array": {"max_tasks": self.cfg.max_array_size, "concurrency_limit": self.cfg.array_concurrency},
            "ask_the_user": [
                "Does the job need a GPU? (required, never assume)",
                "If yes: which GPU type(s) from gpu_types?",
                "Is the default time enough, or is a longer run needed?",
            ],
        }

    def plan_submit_preset(
        self, preset: str, gpu: bool, command: list[str], workdir: str, job_name: str,
        gpu_types: list[str] | None = None, time: str | None = None, cpus: int | None = None,
        mem: str | None = None, gpus: int | None = None, array: str | None = None,
        modules: list[str] | None = None,
    ) -> dict[str, Any]:
        """Dry run: build the full script from a preset, show it, bind the token to it."""
        self._require_writes()
        chosen = self.cfg.presets.get(preset)
        if chosen is None:
            raise SlurmMcpError(f"unknown preset {preset!r}; available: {sorted(self.cfg.presets) or 'none configured'}")
        workdir = self._path(workdir)
        if not self._probe(cmd.test_dir_cmd(workdir)):
            raise SlurmMcpError(f"working directory does not exist on the cluster: {workdir}")
        spec = resolve_spec(
            chosen, self.cfg.gpu_types, gpu=gpu, command=command, workdir=workdir, job_name=job_name,
            gpu_types=gpu_types, time=time, cpus=cpus, mem=mem, gpus=gpus, array=array, modules=modules,
            max_array_size=self.cfg.max_array_size, array_concurrency=self.cfg.array_concurrency,
        )
        warnings = self.check_gpu_availability(spec)
        script = build_script(spec)
        digest = hashlib.sha256(script.encode()).hexdigest()
        script_path = self._path(f"{self.cfg.scripts_dir}/{spec.job_name}-{digest[:8]}.sh")
        argv = cmd.sbatch_cmd(script_path, chdir=workdir)
        summary = summarize(spec)
        token = self.confirmations.issue("submit", argv, summary, script, script_path)
        return {
            "dry_run": True,
            "summary": summary,
            "warnings": warnings,
            "script": script,
            "script_sha256": digest,
            "script_will_be_written_to": script_path,
            "command": shlex.join(argv),
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the user the summary and the full script. Call submit_job with this token only after they agree.",
        }

    def ensure_scripts_dir(self) -> str:
        """Create the directory for generated scripts (inside the roots). Used by the doctor."""
        self._require_writes()
        path = self._path(self.cfg.scripts_dir)
        self._ssh(cmd.mkdir_cmd(path))
        return path

    def check_gpu_availability(self, spec) -> list[str]:
        """Read-only sinfo check that the chosen GPU features exist in the chosen partitions.

        Several partitions with an OR constraint are fine as long as at least one partition
        has a matching node, so a partition lacking some type only earns a warning. If no
        partition has any of the chosen types the job could never start, which is an error.
        """
        if not spec.constraint:
            return []
        wanted = {self.cfg.gpu_types[n]: n for n in spec.gpu_types}
        out = self._ssh(cmd.partition_features_cmd(spec.partition), allow_failure=True)
        if out is None:
            return ["could not check GPU availability on the partitions (sinfo failed)"]
        available = parse_partition_features(out)
        warnings = []
        for part in spec.partition.split(","):
            have = available.get(part, set())
            missing = [name for feat, name in wanted.items() if feat not in have]
            if len(missing) == len(wanted):
                warnings.append(f"partition {part} has none of the chosen GPU types, jobs cannot start there")
            elif missing:
                warnings.append(f"{', '.join(missing)} is not available on {part}")
        if all(f"partition {p} has none" in " ".join(warnings) for p in spec.partition.split(",")):
            raise SlurmMcpError(
                f"none of the chosen GPU types ({', '.join(spec.gpu_types)}) exist on partition(s) "
                f"{spec.partition}; Slurm would reject or never start the job. Choose other GPU types or another preset."
            )
        return warnings

    def _upload_script(self, text: str, remote_path: str) -> None:
        self._ssh(cmd.mkdir_cmd(remote_path.rsplit("/", 1)[0]))
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "job.sh"
            local.write_text(text)
            res = self.runner.run(cmd.scp_upload_argv(self.cfg, str(local), remote_path), self.cfg.command_timeout)
        if res.returncode != 0:
            raise RemoteError(f"could not write the job script: {res.stderr.strip()[:300]}")

    def submit(self, confirmation_token: str) -> dict[str, Any]:
        self._require_writes()
        action = self.confirmations.redeem(confirmation_token, "submit")
        if action.script_text is not None and action.script_path:
            self._upload_script(action.script_text, action.script_path)
        out = (self._ssh(list(action.remote_args)) or "").strip()
        job_id = out.split(";")[0]
        if not cmd.JOB_ID_RE.fullmatch(job_id):
            raise RemoteError(f"sbatch gave unexpected output: {out[:200]!r}")
        return {"submitted": True, "job_id": job_id, "summary": action.summary, "script": action.script_path}

    def plan_cancel(self, job_id: str) -> dict[str, Any]:
        """Dry run: only active jobs of the current user can be cancelled."""
        self._require_writes()
        job_id = cmd.validate_job_id(job_id)
        base = job_id.split("_")[0]
        mine = [r for r in self.queue()
                if r["job_id"] == job_id or ("_" not in job_id and r["job_id"].split("_")[0] == base)]
        if not mine:
            raise SlurmMcpError(f"job {job_id} is not one of your pending or running jobs")
        argv = cmd.scancel_cmd(job_id)
        token = self.confirmations.issue("cancel", argv, shlex.join(argv))
        return {
            "dry_run": True,
            "command": shlex.join(argv),
            "would_cancel": mine,
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the jobs to the user. Call cancel_job with this token only after they agree.",
        }

    def cancel(self, confirmation_token: str) -> dict[str, Any]:
        self._require_writes()
        action = self.confirmations.redeem(confirmation_token, "cancel")
        self._ssh(list(action.remote_args))
        return {"cancelled": True, "command": action.summary}
