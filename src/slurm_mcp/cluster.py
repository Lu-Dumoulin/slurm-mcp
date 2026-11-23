"""Read-only operations on the cluster. Depends on a Runner, never on subprocess."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

from . import commands as cmd
from .config import Config
from .confirm import ConfirmationStore
from .errors import RemoteError, RemoteFileNotFound, SlurmMcpError
from .parsing import parse_beegfs_quota, parse_df, parse_du, parse_find, parse_partitions, parse_slurm_limits, parse_sacct_paths, parse_partition_features, parse_sacct, parse_scontrol, parse_squeue
from .diagnose import clean_reason, diagnose, never_started, resolve_log_paths
from .sbatchopts import resolve_options
from .jobscript import build_script, resolve_spec, summarize
from .paths import is_safe_relative, sensitive_match, validate_remote_path
from . import review as rv
from .runner import Runner
from .sync import RemoteFile, SyncPlan, matches, plan_sync, scan_local
from .uploads import DEFAULT_EXCLUDE, compare_trees, effective_roots, plan_hash, resolve_local_path
from .uploads import scan_local as scan_local_files

_MAX_TABLE_BYTES = 1_000_000


class Cluster:
    def __init__(self, cfg: Config, runner: Runner, confirmations: ConfirmationStore | None = None):
        self.cfg = cfg
        self.runner = runner
        self.confirmations = confirmations or ConfirmationStore(cfg.confirmation_ttl_s)
        # Folders upload may read: the folder the server started in (the agent's project) and local_roots.
        self.upload_roots, self.upload_notes = effective_roots(cfg.local_roots, from_cwd=cfg.upload_from_cwd)

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

    def _read_path(self, path: str) -> str:
        """A path that may be read, listed or downloaded: inside the roots, not a secret by name, and not a symlink
        that leads to one (the path is resolved on the cluster and checked again). Returns the resolved path."""
        lexical = self._path(path)
        extra = self.cfg.sensitive_extra
        hit = sensitive_match(lexical, extra)
        if hit:
            raise SlurmMcpError(f"refusing to read {lexical}: it looks like a secret (matches {hit})")
        res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.realpath_cmd(lexical)), self.cfg.command_timeout)
        if res.returncode == 255:
            self._ssh(["true"])  # standard connection error
        if res.returncode != 0 or not res.stdout.strip():
            raise SlurmMcpError(f"could not resolve {lexical} on the cluster; refusing to read it")
        real = self._path(res.stdout.strip())                 # still inside the roots
        hit = sensitive_match(real, extra)
        if hit:
            raise SlurmMcpError(f"refusing to read {lexical}: it resolves to {real}, which looks like a secret (matches {hit})")
        return real

    def _gate(self, token: str, kind: str, review_text: str | None) -> None:
        """The write call must repeat the review text of its plan exactly, so that the client's approval
        prompt shows the command and the files. None skips the check (internal callers and tests only)."""
        if review_text is None:
            return
        if review_text != self.confirmations.peek(token, kind).review:
            raise SlurmMcpError("review_text does not match the plan: pass the review_text returned by the dry run, unchanged")

    def _queue_warnings(self, new_jobs: int) -> list[str]:
        """Refuse a submission that would push your queue past max_queued_jobs (array tasks count one each)."""
        try:
            rows = self.queue()
        except RemoteError:
            return ["could not check how many jobs are queued"]
        existing = 0
        for r in rows:
            m = re.search(r"\[(\d+)-(\d+)", r["job_id"])
            existing += (int(m.group(2)) - int(m.group(1)) + 1) if m else 1
        total = existing + new_jobs
        if total > self.cfg.max_queued_jobs:
            raise SlurmMcpError(
                f"this would make {total} queued jobs (you have {existing}); the limit is "
                f"max_queued_jobs = {self.cfg.max_queued_jobs}"
            )
        if total > self.cfg.warn_queued_jobs:
            return [f"LARGE SUBMISSION: this adds {new_jobs} job(s) or tasks; you would have {total} jobs queued "
                    f"(you have {existing} now; warning threshold {self.cfg.warn_queued_jobs}, hard limit {self.cfg.max_queued_jobs}). "
                    "Check that this is intended."]
        return []

    def _path(self, path: str) -> str:
        return validate_remote_path(path, self.cfg.remote_roots)

    def _clamp_lines(self, lines: int) -> int:
        return max(1, min(int(lines), self.cfg.max_tail_lines))

    # --- the cluster itself ------------------------------------------------------
    def cluster_info(self) -> dict[str, Any]:
        """Partitions (time limits, nodes, CPUs, memory, GPUs) and a few Slurm limits, read-only."""
        partitions = parse_partitions(self._ssh(cmd.partitions_cmd()) or "")
        config = self._ssh(cmd.slurm_config_cmd(), allow_failure=True) or ""
        return {"partitions": partitions, "limits": parse_slurm_limits(config, cmd.LIMIT_KEYS),
                "note": "Array task indices must be below MaxArraySize."}

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
        path = self._read_path(path)
        n = self._clamp_lines(lines)
        res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.tail_cmd(path, n)), self.cfg.command_timeout)
        if res.returncode == 1 and "No such file" in res.stderr:
            raise RemoteFileNotFound(f"file not found at {path}")
        if res.returncode != 0:
            self._ssh(cmd.tail_cmd(path, n))  # re-raises with the standard SSH or remote error message
        return {"path": path, "lines_requested": n, "text": res.stdout}

    # --- files ------------------------------------------------------------------
    def _list(self, directory: str, max_depth: int):
        directory = self._read_path(directory)
        depth = max(1, min(int(max_depth), 6))
        listing = parse_find(self._ssh(cmd.find_cmd(directory, depth)) or "")
        visible = [f for f in listing if not sensitive_match(f.rel, self.cfg.sensitive_extra)]   # secrets are not listed
        self._hidden = len(listing) - len(visible)
        return directory, visible

    def storage_info(self, path: str | None = None) -> dict[str, Any]:
        """Free and used space of each root (df), optionally the size of one folder (du), and any configured
        quota commands, as the cluster reports them. Read-only."""
        filesystems = []
        for root in self.cfg.remote_roots:
            row = parse_df(self._ssh(cmd.df_cmd(root), allow_failure=True) or "")
            filesystems.append({"root": root, **(row or {"error": "df failed"})})
        out: dict[str, Any] = {"filesystems": filesystems}
        if path:
            path = self._path(path)
            try:
                res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.du_cmd(path)), self.cfg.du_timeout_s)
            except RemoteError:
                out["du"] = {"path": path, "error": f"du did not finish within {self.cfg.du_timeout_s} s: the folder is too big to measure quickly"}
            else:
                if res.returncode == 255:
                    self._ssh(["true"])
                size = parse_du(res.stdout)
                out["du"] = ({"path": path, "bytes": size} if size is not None and res.returncode == 0
                             else {"path": path, "error": res.stderr.strip()[:200] or "du failed", "bytes": size})
        quotas = []
        for argv in self.cfg.quota_commands:
            res = self.runner.run(cmd.ssh_argv(self.cfg, list(argv)), self.cfg.command_timeout)
            entry = {"command": " ".join(argv), "exit_code": res.returncode, "output": (res.stdout or res.stderr)[:4000]}
            parsed = parse_beegfs_quota(res.stdout) if res.returncode == 0 else None
            if parsed:
                entry["parsed"] = parsed          # used and hard limits for size and for number of files
            quotas.append(entry)
        if quotas:
            out["quota"] = quotas
        return out

    MAX_READ_LINES = 1000
    MAX_READ_CHARS = 200_000

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> dict[str, Any]:
        """A range of lines of a text file under a root (read-only)."""
        path = self._read_path(path)
        first = max(1, int(start_line))
        count = max(1, min(int(max_lines), self.MAX_READ_LINES))
        res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.sed_lines_cmd(path, first, first + count - 1)),
                              self.cfg.command_timeout)
        if res.returncode == 255:
            self._ssh(["true"])
        if res.returncode != 0:
            raise SlurmMcpError(f"cannot read {path}: {res.stderr.strip()[:200] or 'sed failed'}")
        text = res.stdout
        if "\0" in text:
            return {"path": path, "start_line": first, "note": "binary file, not shown"}
        truncated = len(text) > self.MAX_READ_CHARS
        shown = text[: self.MAX_READ_CHARS]
        got = shown.count("\n") + (1 if shown and not shown.endswith("\n") else 0)
        return {"path": path, "start_line": first, "lines_returned": got, "end_of_file": got < count and not truncated,
                "truncated": truncated, "text": shown}

    def grep_file(self, path: str, pattern: str, regex: bool = False, ignore_case: bool = False, context: int = 0,
                  max_matches: int = 100) -> dict[str, Any]:
        """Search one text file under a root. The pattern is a fixed string unless regex is true."""
        if not isinstance(pattern, str) or not pattern or len(pattern) > 500 or "\n" in pattern or "\0" in pattern:
            raise SlurmMcpError("pattern must be a non-empty single-line string of at most 500 characters")
        path = self._read_path(path)
        ctx = max(0, min(int(context), 10))
        limit = max(1, min(int(max_matches), 500))
        argv = cmd.grep_cmd(path, pattern, regex=bool(regex), ignore_case=bool(ignore_case), context=ctx, max_matches=limit)
        res = self.runner.run(cmd.ssh_argv(self.cfg, argv), self.cfg.command_timeout)
        if res.returncode == 255:
            self._ssh(["true"])
        if res.returncode not in (0, 1):                      # grep: 1 means no match, 2 means an error
            raise SlurmMcpError(f"cannot search {path}: {res.stderr.strip()[:200] or 'grep failed'}")
        text = res.stdout[: self.MAX_READ_CHARS]
        matches = sum(1 for line in text.splitlines() if line.partition(":")[0].isdigit() and ":" in line)
        return {"path": path, "pattern": pattern, "regex": bool(regex), "matches": matches,
                "limit_reached": matches >= limit, "truncated": len(res.stdout) > self.MAX_READ_CHARS, "text": text}

    def list_files(self, directory: str, max_depth: int = 1, pattern: str = "*") -> dict[str, Any]:
        directory, files = self._list(directory, max_depth)
        files = [f for f in files if matches(f.rel, pattern)]
        files.sort(key=lambda f: f.rel)
        limit = self.cfg.max_list_entries
        return {
            "directory": directory,
            "count": len(files),
            "hidden_secret_names": self._hidden,
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
        path = self._read_path(path)
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
        self, directory: str, pattern: str = "*", max_depth: int = 3, dry_run: bool = True
    ) -> dict[str, Any]:
        """Download missing or newer files into local_dir. dry_run defaults to True: look first, then repeat with
        dry_run=false. Secrets are never downloaded and a local file that is newer is never replaced."""
        directory, files = self._list(directory, max_depth)
        dest_root = self.cfg.local_dir / directory.lstrip("/")
        files = [f for f in files if f.kind == "f"]
        safe = [f for f in files if is_safe_relative(f.rel)]
        local_state = scan_local(dest_root)
        plan = plan_sync(safe, local_state, pattern)
        kept = [p.file.rel for p in plan.download if p.file.rel in local_state and local_state[p.file.rel][1] > p.file.mtime + 2]
        if kept:
            plan = SyncPlan(tuple(p for p in plan.download if p.file.rel not in kept), plan.up_to_date)
        report: dict[str, Any] = {
            "remote_dir": directory,
            "local_dir": str(dest_root),
            "to_download": [{"path": p.file.rel, "size": p.file.size, "reason": p.reason} for p in plan.download],
            "up_to_date": len(plan.up_to_date),
            "kept_newer_local_files": kept,
            "hidden_secret_names": self._hidden,
            "skipped_unsafe_names": len(files) - len(safe),
            "total_bytes": plan.total_bytes,
            "dry_run": dry_run,
        }
        local_root = self.cfg.local_dir.resolve()
        if not (dest_root.resolve() == local_root or local_root in dest_root.resolve().parents):
            raise SlurmMcpError(f"refusing to write to {dest_root}: it resolves outside the download folder {local_root}")
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

    def plan_submit(self, script_path: str, dependency: str | None = None, qos: str | None = None,
                    exclusive: bool = False, hold: bool = False, mail_type: list[str] | None = None) -> dict[str, Any]:
        """Dry run: validate the script, return the exact command and a confirmation token."""
        self._require_writes()
        options = resolve_options(dependency=dependency, qos=qos, exclusive=exclusive, hold=hold, mail_type=mail_type,
                                  allowed_qos=self.cfg.allowed_qos, allow_exclusive=self.cfg.allow_exclusive,
                                  mail_user=self.cfg.mail_user)
        path = self._path(script_path)
        if not self._is_regular_file(path):
            raise SlurmMcpError(f"not a regular file on the cluster: {path}")
        digest = self._script_sha256(path)
        text = self._ssh(cmd.head_bytes_cmd(path, self.MAX_REVIEW_SCRIPT + 1)) or ""
        if len(text.encode()) > self.MAX_REVIEW_SCRIPT:
            raise SlurmMcpError(f"{path} is larger than {self.MAX_REVIEW_SCRIPT} bytes: too large to review in full, "
                                "so it will not be submitted through this tool")
        warnings = self._queue_warnings(1)
        argv = cmd.sbatch_cmd(path, extra_flags=options.flags())
        review = rv.submit(shlex.join(argv), path, digest, text, writes_script=False, warnings=warnings)
        token = self.confirmations.issue("submit", argv, shlex.join(argv), script_path=path, plan_sha256=digest, review=review)
        return {
            "dry_run": True,
            "command": shlex.join(argv),
            "script": path,
            "script_sha256": digest,
            "script_text": text,
            "warnings": warnings,
            "review_text": review,
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the user the command and the full script. Call submit_job with this token and with "
                         "review_text exactly as returned here, only after they agree.",
        }

    MAX_REVIEW_SCRIPT = 20_000

    def _script_sha256(self, path: str) -> str:
        out = (self._ssh(cmd.sha256_cmd(path)) or "").split()
        if not out or len(out[0]) != 64:
            raise SlurmMcpError(f"could not hash {path} on the cluster")
        return out[0]

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
        modules: list[str] | None = None, env: dict[str, str] | None = None, task_dir: str | None = None,
        array_from_table: str | None = None, dependency: str | None = None, qos: str | None = None,
        exclusive: bool = False, hold: bool = False, mail_type: list[str] | None = None,
    ) -> dict[str, Any]:
        """Dry run: build the full script from a preset, show it, bind the token to it."""
        self._require_writes()
        chosen = self.cfg.presets.get(preset)
        if chosen is None:
            raise SlurmMcpError(f"unknown preset {preset!r}; available: {sorted(self.cfg.presets) or 'none configured'}")
        workdir = self._path(workdir)
        if not self._probe(cmd.test_dir_cmd(workdir)):
            raise SlurmMcpError(f"working directory does not exist on the cluster: {workdir}")
        table_info = None
        if array_from_table:
            if array:
                raise SlurmMcpError("give either array or array_from_table, not both")
            table = self._path(array_from_table)
            rows = self._table_rows(table)
            array = f"1-{rows}"
            table_info = {"path": table, "rows": rows}
        if task_dir is not None:
            self._path(task_dir.replace("{task_id}", "1"))  # the folders must lie inside the roots
        options = resolve_options(dependency=dependency, qos=qos, exclusive=exclusive, hold=hold, mail_type=mail_type,
                                  allowed_qos=chosen.allowed_qos, allow_exclusive=chosen.allow_exclusive,
                                  mail_user=self.cfg.mail_user)
        spec = resolve_spec(
            chosen, self.cfg.gpu_types, gpu=gpu, command=command, workdir=workdir, job_name=job_name,
            gpu_types=gpu_types, time=time, cpus=cpus, mem=mem, gpus=gpus, array=array, modules=modules,
            env=env, task_dir=task_dir, options=options,
            max_array_size=self.cfg.max_array_size, array_concurrency=self.cfg.array_concurrency,
        )
        warnings = self.check_gpu_availability(spec)
        script = build_script(spec)
        digest = hashlib.sha256(script.encode()).hexdigest()
        script_path = self._path(f"{self.cfg.scripts_dir}/{spec.job_name}-{digest[:8]}.sh")
        argv = cmd.sbatch_cmd(script_path, chdir=workdir)
        summary = summarize(spec)
        warnings += self._queue_warnings(spec.array_tasks or 1)
        review = rv.submit(shlex.join(argv), script_path, digest, script, writes_script=True, warnings=warnings)
        token = self.confirmations.issue("submit", argv, summary, script, script_path, review=review)
        return {
            "dry_run": True,
            "summary": summary,
            "array": spec.array,
            "array_from_table": table_info,
            "warnings": warnings,
            "script": script,
            "script_sha256": digest,
            "review_text": review,
            "script_will_be_written_to": script_path,
            "command": shlex.join(argv),
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the user the summary and the full script. Call submit_job with this token and with review_text exactly as returned here, only after they agree.",
        }

    def ensure_scripts_dir(self) -> str:
        """Create the directory for generated scripts (inside the roots). Used by the doctor."""
        self._require_writes()
        path = self._path(self.cfg.scripts_dir)
        self._ssh(cmd.mkdir_cmd(path))
        return path

    def _table_rows(self, path: str) -> int:
        """Data rows of a CSV on the cluster (all lines but the header), for one array task per row."""
        if not self._is_regular_file(path):
            raise SlurmMcpError(f"not a regular file on the cluster: {path}")
        text = (self._ssh(cmd.table_rows_cmd(path)) or "").strip()
        lines = int(text) if text.isdigit() else 0
        if lines < 2:
            raise SlurmMcpError(f"{path} has no data rows (a header line and at least one row are expected)")
        return lines - 1

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

    # --- code upload (local folder to the cluster), same two steps ---------------------
    def _real_remote_dir(self, remote_dir: str) -> str:
        """Lexically validated, then resolved on the cluster so a symlink cannot lead out of the roots."""
        lexical = self._path(remote_dir)
        res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.realpath_cmd(lexical)), self.cfg.command_timeout)
        if res.returncode == 255:
            self._ssh(["true"])  # standard connection error
        if res.returncode != 0 or not res.stdout.strip():
            raise SlurmMcpError(f"could not resolve {lexical} on the cluster; refusing to use it")
        return self._path(res.stdout.strip())  # must still lie inside the roots

    def _local_scan(self, local_path: str | None):
        real, root = resolve_local_path(local_path, tuple(self.upload_roots))
        exclude = tuple(DEFAULT_EXCLUDE) + self.cfg.upload_exclude
        return real, root, scan_local_files(real, exclude, self.cfg.max_upload_files)

    def _remote_state(self, dest: str) -> dict[str, tuple[int, float]] | None:
        """Files below `dest` as {relative path: (size, mtime)}, or None if the folder does not exist."""
        if not self._probe(cmd.test_dir_cmd(dest)):
            return None
        listing = parse_find(self._ssh(cmd.find_cmd(dest, 12)) or "")
        return {f.rel: (f.size, f.mtime) for f in listing if f.kind == "f"}

    def plan_upload_code(self, remote_dir: str, local_path: str | None = None) -> dict[str, Any]:
        """Dry run: what would be copied where. The contents of a folder go into remote_dir."""
        self._require_writes()
        real, root, scan = self._local_scan(local_path)
        dest = self._real_remote_dir(remote_dir)
        existing = self._remote_state(dest) or {}
        plan = plan_sync([RemoteFile(f.rel, f.size, f.mtime) for f in scan.files], existing)
        by_rel = {f.rel: f for f in scan.files}

        large_bytes = self.cfg.large_file_mb * 1024 * 1024
        large = [p for p in plan.download if p.file.size > large_bytes]
        items = [(p.file.rel, p.file.size, by_rel[p.file.rel].mtime_ns, p.reason) for p in plan.download]
        digest = plan_hash(str(real), dest, items)
        sent = [(by_rel[p.file.rel].path, f"{dest}/{p.file.rel}", p.file.size, by_rel[p.file.rel].mtime_ns)
                for p in plan.download]
        dirs = sorted({d.rsplit("/", 1)[0] for _, d, _, _ in sent} | {dest})  # mkdir -p is idempotent

        warnings = []
        overwrites = [p for p in plan.download if p.reason != "missing"]
        if overwrites:
            warnings.append(f"{len(overwrites)} existing remote file(s) would be overwritten")
        newer = [p.file.rel for p in overwrites if existing[p.file.rel][1] > by_rel[p.file.rel].mtime + 2]
        if newer:
            warnings.append(f"{len(newer)} remote file(s) are newer than the local copy and would still be replaced: {', '.join(newer[:5])}")
        if large:
            warnings.append(
                f"{len(large)} file(s) are larger than {self.cfg.large_file_mb} MB: upload_code needs "
                "confirm_large_files listing them, after the user has agreed to send them"
            )
        if scan.unsafe_names:
            warnings.append(f"{scan.unsafe_names} file(s) skipped because their names have spaces or unusual characters")
        if scan.symlinks:
            warnings.append(f"{scan.symlinks} symbolic link(s) skipped (links are never followed)")
        warnings += self.upload_notes
        if not plan.download:
            warnings.append("nothing to upload: the cluster already has these files")

        token, review = "", ""
        if plan.download:
            files_view = [(p.file.rel, p.file.size, "create" if p.reason == "missing" else "overwrite", p.file.size > large_bytes)
                          for p in plan.download]
            review = rv.upload(f"{self.cfg.user}@{self.cfg.host}:{dest}", str(real),
                               shlex.join(cmd.mkdir_many_cmd(dirs)) if dirs else None, files_view, plan.total_bytes, digest)
            token = self.confirmations.issue("upload", [], f"upload {len(sent)} file(s) to {dest}", upload=tuple(sent),
                                             mkdirs=tuple(dirs), plan_sha256=digest, dest=dest, review=review)
        shown = [{"path": p.file.rel, "size": p.file.size, "action": "create" if p.reason == "missing" else "overwrite",
                  "reason": p.reason} for p in plan.download[:200]]
        return {
            "dry_run": True,
            "from": str(real),
            "allowed_because": root.rule,
            "to": f"{self.cfg.user}@{self.cfg.host}:{dest}",
            "commands": {
                "mkdir": shlex.join(cmd.mkdir_many_cmd(dirs)) if dirs else None,
                "copy": f"per file: scp -p <file> {self.cfg.user}@{self.cfg.host}:<destination>/.<name>{cmd.PART_SUFFIX}, "
                        "then mv -f to <name> (the old file stays intact if the copy fails)",
            },
            "files_to_send": len(plan.download),
            "files_up_to_date": len(plan.up_to_date),
            "total_bytes": plan.total_bytes,
            "files": shown,
            "files_truncated": len(plan.download) > 200,
            "large_files": [{"path": p.file.rel, "size": p.file.size} for p in large],
            "needs_large_file_confirmation": bool(large),
            "excluded_by_name": scan.excluded,
            "deletes_remote_files": False,
            "warnings": warnings,
            "plan_sha256": digest,
            "review_text": review or None,
            "confirmation_token": token or None,
            "expires_in_s": self.confirmations.ttl_s if token else None,
            "next_step": ("Show the user the destination, the file list, the large files and the warnings. Call "
                          "upload_code with this token and with review_text exactly as returned here, only after they agree" +
                          (", and pass confirm_large_files with exactly the large files listed." if large else ".")
                          if token else "Nothing to do."),
        }

    def upload_code(self, confirmation_token: str, confirm_large_files: list[str] | None = None,
                    verify: bool = False, review_text: str | None = None) -> dict[str, Any]:
        self._require_writes()
        self._gate(confirmation_token, "upload", review_text)
        action = self.confirmations.peek(confirmation_token, "upload")
        large_bytes = self.cfg.large_file_mb * 1024 * 1024
        prefix = action.dest + "/"
        large = sorted(r[len(prefix):] for _, r, size, _ in action.upload if size > large_bytes)
        if large and sorted(confirm_large_files or []) != large:
            raise SlurmMcpError(
                f"{len(large)} file(s) are larger than {self.cfg.large_file_mb} MB and need an explicit confirmation. "
                f"Ask the user, then call upload_code again with confirm_large_files={large}"
            )  # the token is still valid
        self.confirmations.redeem(confirmation_token, "upload")
        for local, _, size, mtime_ns in action.upload:  # what is sent must be what was reviewed
            if Path(local).is_symlink():
                raise SlurmMcpError(f"{local} is now a symbolic link: refusing to upload it; run the dry run again")
            try:
                st = Path(local).stat()
            except OSError:
                raise SlurmMcpError(f"{local} disappeared since the dry run; run the dry run again") from None
            if st.st_size != size or st.st_mtime_ns != mtime_ns:
                raise SlurmMcpError(f"{local} changed since the dry run; run the dry run again")
        for i in range(0, len(action.mkdirs), 50):
            self._ssh(cmd.mkdir_many_cmd(list(action.mkdirs[i:i + 50])))
        done, failed = 0, []
        for local, remote, _, _ in action.upload:
            folder, _, name = remote.rpartition("/")
            part = f"{folder}/.{name}{cmd.PART_SUFFIX}"   # copy aside, then rename: a failure keeps the old file
            res = self.runner.run(cmd.scp_upload_argv(self.cfg, local, part, preserve_times=True),
                                  self.cfg.command_timeout * 10)
            if res.returncode == 0:
                res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.mv_cmd(part, remote)), self.cfg.command_timeout)
            if res.returncode == 0:
                done += 1
            else:
                failed.append({"path": remote, "error": res.stderr.strip()[:200]})
                self.runner.run(cmd.ssh_argv(self.cfg, cmd.rm_part_cmd(part)), self.cfg.command_timeout)  # only our temp file
        out: dict[str, Any] = {"uploaded": done, "failed": failed, "plan_sha256": action.plan_sha256,
                               "summary": action.summary}
        if verify:
            out["verification"] = self._verify_after_upload(action)
        return out

    def _verify_after_upload(self, action) -> dict[str, Any]:
        """Compare what was just sent with the cluster (sizes, like CommonUI's check)."""
        remote = self._remote_state(action.dest) or {}
        prefix = action.dest + "/"
        wrong, missing = [], []
        for _, r, size, _ in action.upload:
            rel = r[len(prefix):]
            if rel not in remote:
                missing.append(rel)
            elif remote[rel][0] != size:
                wrong.append(rel)
        return {"checked": len(action.upload), "all_match": not wrong and not missing,
                "missing_on_cluster": missing[:50], "size_differs": wrong[:50]}

    def compare_code(self, remote_dir: str, local_path: str | None = None) -> dict[str, Any]:
        """Read-only: how a local folder compares with a cluster folder (sizes decide, as in CommonUI)."""
        real, root, scan = self._local_scan(local_path)
        dest = self._real_remote_dir(remote_dir)
        remote = self._remote_state(dest)
        result = compare_trees([(f.rel, f.size, f.mtime) for f in scan.files], remote or {})

        def cap(items):
            return {"count": len(items), "files": list(items[:100]), "truncated": len(items) > 100}

        return {
            "from": str(real), "allowed_because": root.rule, "to": f"{self.cfg.user}@{self.cfg.host}:{dest}",
            "remote_folder_exists": remote is not None,
            "identical": remote is not None and result.identical,
            "matching": len(result.matching),
            "only_local": cap(result.only_local),
            "only_remote": cap(result.only_remote),
            "size_differs": cap(result.size_differs),
            "local_newer": cap(result.local_newer),
            "remote_newer": cap(result.remote_newer),
            "excluded_by_name": scan.excluded,
            "note": "Files only on the cluster (outputs, data) do not count against identical.",
        }

    def _upload_script(self, text: str, remote_path: str) -> None:
        self._ssh(cmd.mkdir_cmd(remote_path.rsplit("/", 1)[0]))
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "job.sh"
            local.write_text(text)
            res = self.runner.run(cmd.scp_upload_argv(self.cfg, str(local), remote_path), self.cfg.command_timeout)
        if res.returncode != 0:
            raise RemoteError(f"could not write the job script: {res.stderr.strip()[:300]}")

    def submit(self, confirmation_token: str, review_text: str | None = None) -> dict[str, Any]:
        self._require_writes()
        self._gate(confirmation_token, "submit", review_text)
        action = self.confirmations.redeem(confirmation_token, "submit")
        if action.script_text is not None and action.script_path:
            self._upload_script(action.script_text, action.script_path)
        elif action.script_path and action.plan_sha256:      # an existing script: it must be what was reviewed
            if self._script_sha256(action.script_path) != action.plan_sha256:
                raise SlurmMcpError(f"{action.script_path} changed since the dry run, nothing was submitted: run the dry run again")
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
        review = rv.jobs("cancel jobs", shlex.join(argv), mine)
        token = self.confirmations.issue("cancel", argv, shlex.join(argv), review=review)
        return {
            "dry_run": True,
            "command": shlex.join(argv),
            "would_cancel": mine,
            "review_text": review,
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the jobs to the user. Call cancel_job with this token and review_text exactly as returned here, only after they agree.",
        }

    MAX_DELETE_ENTRIES = 100_000
    _PROTECTED_PARTS = {".ssh", ".gnupg"}

    def _delete_target(self, path: str) -> str:
        """The path to delete: strictly below a root, never a root or anything above one, no symlinks involved."""
        lexical = self._path(path)
        roots = self.cfg.remote_roots
        if any(lexical == r or r.startswith(lexical.rstrip("/") + "/") for r in roots):
            raise SlurmMcpError(f"refusing to delete {lexical}: it is a configured root or contains one")
        if not any(lexical.startswith(r.rstrip("/") + "/") for r in roots):
            raise SlurmMcpError(f"refusing to delete {lexical}: it is not strictly inside a configured root")
        if self._PROTECTED_PARTS & set(lexical.split("/")):
            raise SlurmMcpError(f"refusing to delete {lexical}: it is inside a protected folder")
        res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.realpath_cmd(lexical)), self.cfg.command_timeout)
        if res.returncode == 255:
            self._ssh(["true"])
        if res.returncode != 0 or not res.stdout.strip():
            raise SlurmMcpError(f"could not resolve {lexical} on the cluster; refusing to delete it")
        real = self._path(res.stdout.strip())          # must still lie inside the roots
        if real != lexical:
            raise SlurmMcpError(f"refusing to delete {lexical}: it resolves to {real} (a symbolic link is involved); give the real path")
        return lexical

    def _tree(self, path: str) -> list[tuple[str, str, int]]:
        if not self._probe(cmd.test_exists_cmd(path)):
            raise SlurmMcpError(f"{path} does not exist")
        entries = []
        for record in (self._ssh(cmd.find_tree_cmd(path)) or "").split("\0"):
            kind, _, rest = record.partition("\t")
            size, _, name = rest.partition("\t")
            if kind and name and size.isdigit():
                entries.append((kind, name, int(size)))
            if len(entries) > self.MAX_DELETE_ENTRIES:
                raise SlurmMcpError(f"more than {self.MAX_DELETE_ENTRIES} entries: delete smaller parts one at a time")
        return entries

    @staticmethod
    def _tree_hash(path: str, entries: list[tuple[str, str, int]]) -> str:
        return hashlib.sha256("\n".join([path] + [f"{k}\t{n}\t{s}" for k, n, s in sorted(entries)]).encode()).hexdigest()

    def plan_delete(self, path: str) -> dict[str, Any]:
        """Dry run: exactly what a recursive delete would remove, with sizes."""
        self._require_writes()
        target = self._delete_target(path)
        entries = self._tree(target)
        files = [e for e in entries if e[0] == "f"]
        dirs = [e for e in entries if e[0] == "d"]
        links = [e for e in entries if e[0] == "l"]
        total = sum(s for k, _, s in entries if k == "f")
        digest = self._tree_hash(target, entries)
        listing = sorted(entries, key=lambda e: e[1])
        review = rv.delete(shlex.join(cmd.rm_tree_cmd(target)), target, len(files), len(dirs), len(links), total, listing, digest)
        token = self.confirmations.issue("delete", [target], f"delete {target} ({len(files)} files, {total} bytes)",
                                         plan_sha256=digest, dest=target, review=review)
        largest = sorted(files, key=lambda e: -e[2])[:20]
        return {
            "dry_run": True, "path": target, "files": len(files), "directories": len(dirs), "symbolic_links": len(links),
            "other_entries": len(entries) - len(files) - len(dirs) - len(links), "total_bytes": total,
            "command": shlex.join(cmd.rm_tree_cmd(target)),
            "largest_files": [{"path": n, "size": s} for _, n, s in largest],
            "first_entries": [{"path": n, "type": k, "size": s} for k, n, s in sorted(entries, key=lambda e: e[1])[:100]],
            "entries_truncated": len(entries) > 100,
            "warnings": ["Deleted files cannot be recovered.", "Symbolic links inside are removed as links; what they point to is not touched."],
            "review_text": review,
            "plan_sha256": digest, "confirmation_token": token, "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the user the path, the counts and the largest files. Call delete with this token and review_text exactly as returned here, only after they agree.",
        }

    def delete(self, confirmation_token: str, review_text: str | None = None) -> dict[str, Any]:
        self._require_writes()
        self._gate(confirmation_token, "delete", review_text)
        action = self.confirmations.redeem(confirmation_token, "delete")
        target = action.dest
        self._delete_target(target)                       # the same guards again
        if self._tree_hash(target, self._tree(target)) != action.plan_sha256:
            raise SlurmMcpError("the contents changed since the dry run, nothing was deleted: run the dry run again")
        self._ssh(cmd.rm_tree_cmd(target))
        return {"deleted": target, "summary": action.summary, "plan_sha256": action.plan_sha256}

    def plan_hold(self, job_id: str, hold: bool) -> dict[str, Any]:
        """Dry run for holding a pending job of yours, or releasing a job that you held."""
        self._require_writes()
        job_id = cmd.validate_job_id(job_id)
        rows = [r for r in self.queue() if r["job_id"] == job_id or ("_" not in job_id and r["job_id"].split("_")[0] == job_id)]
        if not rows:
            raise SlurmMcpError(f"job {job_id} is not one of your pending or running jobs")
        if hold and any(r["state"] != "PENDING" for r in rows):
            raise SlurmMcpError("only pending jobs can be held; a running job cannot")
        if not hold and not all(r["state"] == "PENDING" and r["reason"] == "JobHeldUser" for r in rows):
            raise SlurmMcpError("only a job held by you (reason JobHeldUser) can be released; administrator holds cannot")
        argv = cmd.scontrol_hold_cmd(job_id, hold)
        kind = "hold" if hold else "release"
        review = rv.jobs(f"{kind} jobs", shlex.join(argv), rows)
        token = self.confirmations.issue(kind, argv, shlex.join(argv), review=review)
        return {"dry_run": True, "command": shlex.join(argv), "jobs": rows, "review_text": review,
                "confirmation_token": token, "expires_in_s": self.confirmations.ttl_s,
                "next_step": f"Show the user the jobs. Call {kind}_job with this token and review_text exactly as returned here, only after they agree."}

    def apply_hold(self, confirmation_token: str, hold: bool, review_text: str | None = None) -> dict[str, Any]:
        self._require_writes()
        self._gate(confirmation_token, "hold" if hold else "release", review_text)
        action = self.confirmations.redeem(confirmation_token, "hold" if hold else "release")
        self._ssh(list(action.remote_args))
        return {"held" if hold else "released": True, "command": action.summary}

    MAX_CANCEL = 1000
    _COMPRESSED_ARRAY = re.compile(r"(\d{1,12})_\[[\d,%-]+\]")

    def plan_cancel_many(self, job_ids: list[str] | None = None, name: str | None = None,
                         state: str | None = None, all_mine: bool = False) -> dict[str, Any]:
        """Dry run for cancelling several of your own jobs. Exactly one selector; every match is listed."""
        self._require_writes()
        chosen = [s for s, v in (("job_ids", job_ids), ("name", name), ("state", state), ("all_mine", all_mine or None)) if v]
        if len(chosen) != 1:
            raise SlurmMcpError("give exactly one selector: job_ids, name, state (PENDING or RUNNING) or all_mine=true")
        if state is not None and state not in ("PENDING", "RUNNING"):
            raise SlurmMcpError("state must be PENDING or RUNNING")
        if job_ids is not None:
            job_ids = [cmd.validate_job_id(j) for j in job_ids]
        rows = self.queue()                                           # squeue --me: only your own jobs
        base = lambda r: r["job_id"].split("_")[0]                    # noqa: E731

        def selected(r: dict[str, str]) -> bool:
            if job_ids is not None:
                return any(r["job_id"] == j or ("_" not in j and base(r) == j) for j in job_ids)
            if name is not None:
                return r["name"] == name
            if state is not None:
                return r["state"] == state
            return True

        matches = [r for r in rows if selected(r)]
        if not matches:
            raise SlurmMcpError("none of your pending or running jobs match")
        targets: list[str] = []
        warnings: list[str] = []
        for r in matches:
            m = self._COMPRESSED_ARRAY.fullmatch(r["job_id"])
            target = m.group(1) if m else r["job_id"]
            if m:
                warnings.append(f"{r['job_id']} is a pending array: scancel works on the whole array {target}"
                                + (" but only on its pending tasks" if state == "PENDING" else ", including tasks that already run"))
            if target not in targets:
                targets.append(target)
        whole = {t for t in targets if "_" not in t}
        targets = [t for t in targets if "_" not in t or t.split("_")[0] not in whole]   # a whole array covers its tasks
        if len(targets) > self.MAX_CANCEL:
            raise SlurmMcpError(f"{len(targets)} jobs match; narrow the selection (limit {self.MAX_CANCEL})")
        argv_state = state if state == "PENDING" else None
        affected = [r for r in rows if any(r["job_id"] == t or base(r) == t for t in targets)]
        digest = hashlib.sha256("\n".join(sorted(targets) + [argv_state or ""]).encode()).hexdigest()
        summary = f"cancel {len(targets)} job(s): {' '.join(targets[:10])}{' ...' if len(targets) > 10 else ''}"
        shown_cmd = shlex.join(cmd.scancel_many_cmd(targets, argv_state))
        review = rv.jobs("cancel jobs", shown_cmd, affected)
        token = self.confirmations.issue("cancel_many", [argv_state or "", *targets], summary, plan_sha256=digest, review=review)
        return {
            "dry_run": True,
            "selector": chosen[0],
            "would_cancel": affected,
            "scancel_targets": targets,
            "command": shown_cmd if len(targets) <= 10 else shlex.join(cmd.scancel_many_cmd(targets[:10], argv_state)) + " ...",
            "review_text": review,
            "warnings": sorted(set(warnings)),
            "plan_sha256": digest,
            "confirmation_token": token,
            "expires_in_s": self.confirmations.ttl_s,
            "next_step": "Show the user the full list. Call cancel_jobs with this token and review_text exactly as returned here, only after they agree.",
        }

    def cancel_many(self, confirmation_token: str, review_text: str | None = None) -> dict[str, Any]:
        self._require_writes()
        self._gate(confirmation_token, "cancel_many", review_text)
        action = self.confirmations.redeem(confirmation_token, "cancel_many")
        state, ids = (action.remote_args[0] or None), list(action.remote_args[1:])
        failed = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            res = self.runner.run(cmd.ssh_argv(self.cfg, cmd.scancel_many_cmd(chunk, state)), self.cfg.command_timeout)
            if res.returncode == 255:
                self._ssh(["true"])
            if res.returncode != 0:
                failed.append({"jobs": chunk, "error": res.stderr.strip()[:200]})
        return {"cancelled": [j for j in ids if not any(j in f["jobs"] for f in failed)], "failed": failed,
                "plan_sha256": action.plan_sha256}

    def cancel(self, confirmation_token: str, review_text: str | None = None) -> dict[str, Any]:
        self._require_writes()
        self._gate(confirmation_token, "cancel", review_text)
        action = self.confirmations.redeem(confirmation_token, "cancel")
        self._ssh(list(action.remote_args))
        return {"cancelled": True, "command": action.summary}
