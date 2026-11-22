"""Plain-language reading of a job's accounting data. Pure, heuristic, and conservative:
it states what the numbers say and does not guess beyond them."""

from __future__ import annotations

import re
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
}


def parse_size_mb(text: str) -> float | None:
    """sacct sizes such as 1200K, 3.5G or 8Gn (n and c suffixes mean per node / per CPU)."""
    m = _SIZE_RE.fullmatch((text or "").strip())
    if not m:
        return None
    return float(m.group(1)) * _UNITS.get(m.group(2) or "M", 1)


def _base(state: str) -> str:
    return state.split()[0].rstrip("+") if state else ""


def diagnose(rows: list[dict[str, str]], scontrol: dict[str, str] | None) -> dict[str, Any]:
    if not rows and not scontrol:
        return {"state": "UNKNOWN", "findings": ["no accounting data and Slurm no longer knows this job"]}
    main = next((r for r in rows if "." not in r["jobid"]), rows[0] if rows else {})
    state = _base(main.get("state", "") or (scontrol or {}).get("JobState", ""))
    findings: list[str] = []
    exit_code = main.get("exitcode", "")

    if state == "PENDING":
        reason = (scontrol or {}).get("Reason", "")
        findings.append(f"pending ({reason or 'reason unknown'})" + (f": {PENDING_REASONS[reason]}" if reason in PENDING_REASONS else ""))
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
