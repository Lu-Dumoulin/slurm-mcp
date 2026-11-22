"""Plain-language reading of a job's accounting data. Pure, heuristic, and conservative:
it states what the numbers say and does not guess beyond them."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any

_UNITS = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)([KMGT]?)([nc]?)")

PENDING_REASONS = {
    "Priority": "other jobs have higher priority; it will start when it reaches the front",
    "Resources": "waiting for the requested resources to become free",
    "Dependency": "waiting for a job it depends on",
    "ReqNodeNotAvail": "a requested node is unavailable (down, reserved or in maintenance)",
    "PartitionTimeLimit": "the requested time exceeds the partition limit, so it will never start; resubmit with a shorter time",
    "QOSMaxJobsPerUserLimit": "you reached the maximum number of running jobs for this QOS",
    "AssocGrpCpuLimit": "your group reached its CPU limit",
    "JobHeldUser": "held by the user (release with scontrol release)",
    "BadConstraints": "the job's constraints (for example GPU features) cannot be satisfied, so it will never start",
    "BeginTime": "waiting for its requested start time",
    "JobArrayTaskLimit": "the array's concurrent task limit is reached; more tasks start as others finish",
    "AssocMaxJobsLimit": "you reached the maximum number of running jobs for your account",
    "PartitionDown": "the partition is currently down",
    "PartitionNodeLimit": "the job asks for more nodes than the partition allows, so it will never start",
}


def clean_reason(reason: str | None) -> str:
    """Slurm reports "None" for a job without a reason yet; treat it like no reason."""
    r = (reason or "").strip()
    return "" if r.lower() in ("", "none", "(null)") else r


def parse_size_mb(text: str) -> float | None:
    """sacct sizes such as 1200K, 3.5G or 8Gn (n and c suffixes mean per node / per CPU)."""
    m = _SIZE_RE.fullmatch((text or "").strip())
    if not m:
        return None
    return float(m.group(1)) * _UNITS.get(m.group(2) or "M", 1)


def _base(state: str) -> str:
    return state.split()[0].rstrip("+") if state else ""


def diagnose(rows: list[dict[str, str]], scontrol: dict[str, str] | None, reason: str | None = None) -> dict[str, Any]:
    if not rows and not scontrol:
        return {"state": "UNKNOWN", "findings": ["no accounting data and Slurm no longer knows this job"]}
    main = next((r for r in rows if "." not in r["jobid"]), rows[0] if rows else {})
    state = _base(main.get("state", "") or (scontrol or {}).get("JobState", ""))
    findings: list[str] = []
    exit_code = main.get("exitcode", "")

    if state == "PENDING":
        why = clean_reason(reason) or clean_reason((scontrol or {}).get("Reason"))
        if not why:
            findings.append("pending")
        else:
            findings.append(f"pending ({why})" + (f": {PENDING_REASONS[why]}" if why in PENDING_REASONS else ""))
    elif state == "RUNNING":
        findings.append(f"running for {main.get('elapsed') or (scontrol or {}).get('RunTime', '?')}")
    elif state == "COMPLETED":
        findings.append("completed with exit code 0" if exit_code in ("0:0", "") else f"completed, exit code {exit_code}")
    elif state == "TIMEOUT":
        limit = (scontrol or {}).get("TimeLimit")
        findings.append(f"stopped at the time limit after {main.get('elapsed', '?')}" + (f" (limit {limit})" if limit else "")
                        + ": request more time, or make the job faster or restartable")
    elif state == "OUT_OF_MEMORY":
        findings.append("killed for exceeding its memory request: request more memory")
    elif state == "FAILED":
        code, _, sig = exit_code.partition(":")
        findings.append(f"failed with exit code {code}" + (f", signal {sig}" if sig not in ("", "0") else "")
                        + ": the program itself returned an error, see the log tail")
    elif state.startswith("CANCELLED"):
        findings.append("cancelled" + (f" ({main['state']})" if " by " in main.get("state", "") else ""))
    elif state == "NODE_FAIL":
        findings.append("the node failed: resubmitting usually works")
    else:
        findings.append(f"state {state or 'unknown'}")

    req = parse_size_mb(main.get("reqmem", ""))
    used = [parse_size_mb(r.get("maxrss", "")) for r in rows]
    peak = max((u for u in used if u is not None), default=None)
    memory = None
    if req and peak is not None and peak > 0:
        ratio = peak / req
        memory = {"requested_mb": round(req), "peak_used_mb": round(peak), "ratio": round(ratio, 2)}
        if ratio >= 0.9:
            findings.append(f"memory is tight: peak {peak:.0f} MB of {req:.0f} MB requested; ask for more next time")
        elif ratio < 0.3 and req >= 2048:
            findings.append(f"memory was over-requested: peak {peak:.0f} MB of {req:.0f} MB; you could ask for less")
    return {"state": state, "exit_code": exit_code or None, "findings": findings, "memory": memory}


# --- where a job wrote its output ---------------------------------------------------------

_SPEC_RE = re.compile(r"%(.)")


def expand_log_pattern(pattern: str, job_id: str, job_name: str, user: str) -> str | None:
    """Expand the filename patterns sbatch accepts (%j %A %a %x %u %%). None if not possible.

    %j of an array task cannot be recovered from `123_4`, and other specifiers (%N, %t, ...)
    depend on where the task ran, so those are reported as unknown rather than guessed.
    """
    base, _, task = job_id.partition("_")
    values = {"A": base, "x": job_name, "u": user, "%": "%"}
    if task:
        values["a"] = task
    else:
        values["j"] = values["J"] = base

    def one(m: re.Match[str]) -> str:
        if m.group(1) not in values:
            raise KeyError(m.group(1))
        return values[m.group(1)]

    try:
        return _SPEC_RE.sub(one, pattern)
    except KeyError:
        return None


@dataclass(frozen=True)
class LogPaths:
    stdout: str | None   # None: not known
    stderr: str | None   # None: not known (and not merged)
    merged: bool         # stderr goes to the stdout file (no separate --error)


def resolve_log_paths(
    scontrol: dict[str, str] | None,
    sacct_paths: dict[str, str] | None,
    job_id: str,
    job_name: str,
    user: str,
) -> LogPaths:
    """Combine what scontrol (live jobs, already expanded) and sacct (patterns) report."""
    live, acct = scontrol or {}, sacct_paths or {}
    workdir = live.get("WorkDir") or acct.get("workdir") or ""
    raw_out = live.get("StdOut") or acct.get("stdout") or ""
    raw_err = live.get("StdErr") or acct.get("stderr") or ""
    if not raw_out and not raw_err and sacct_paths is not None and workdir:
        raw_out = "slurm-%A_%a.out" if "_" in job_id else "slurm-%j.out"  # Slurm's default file

    def full(pattern: str) -> str | None:
        if not pattern:
            return None
        expanded = expand_log_pattern(pattern, job_id, job_name, user)
        if expanded is None:
            return None
        return expanded if expanded.startswith("/") else (posixpath.join(workdir, expanded) if workdir else None)

    out, err = full(raw_out), full(raw_err)
    merged = out is not None and (not raw_err or err == out)
    return LogPaths(out, None if merged else err, merged)
