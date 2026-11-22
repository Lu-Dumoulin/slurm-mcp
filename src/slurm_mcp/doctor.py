"""`slurm-mcp doctor`: check that the setup works, optionally run a tiny real job.

Without `--smoke` it only reads. With `--smoke` it shows the generated script and asks
for a typed yes in the terminal before anything is submitted, using the same dry-run and
token path as the MCP tools.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import commands as cmd
from .cluster import Cluster
from .config import Config
from .errors import RemoteError, SlurmMcpError
from .jobscript import resolve_spec
from .parsing import parse_squeue
from .runner import Runner

TERMINAL_STATES = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL",
                   "BOOT_FAIL", "DEADLINE", "PREEMPTED"}
SMOKE_COMMAND = ["bash", "-c", "hostname; date; echo smoke-ok"]


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


def run_doctor(
    cfg: Config,
    runner: Runner,
    out: Callable[[str], None] = print,
    input_fn: Callable[[str], str] = input,
    *,
    smoke: bool = False,
    preset: str = "smoke",
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 5,
    wait_s: float = 300,
) -> int:
    """Returns 0 when nothing failed, 1 otherwise. Warnings do not fail the run."""
    cluster = Cluster(cfg, runner)
    fails = 0

    def report(level: str, label: str, detail: str = "") -> None:
        nonlocal fails
        fails += level == "FAIL"
        out(f"[{level:>4}] {label}" + (f": {detail}" if detail else ""))

    report("OK", "config", f"{cfg.user}@{cfg.host}, {len(cfg.remote_roots)} root(s), "
           f"{len(cfg.presets)} preset(s), writes {'enabled' if cfg.enable_writes else 'disabled'}")
    try:
        cfg.local_dir.mkdir(parents=True, exist_ok=True)
        report("OK", "local download folder", str(cfg.local_dir))
    except OSError as exc:
        report("FAIL", "local download folder", str(exc))

    try:
        cluster._ssh(["true"])
        report("OK", "ssh connection", "shared connection works without a prompt")
    except RemoteError as exc:
        report("FAIL", "ssh connection", str(exc))
        out("Remote checks skipped.")
        return 1

    try:
        rows = parse_squeue(cluster._ssh(cmd.squeue_cmd()) or "")
        report("OK", "slurm", f"squeue works, {len(rows)} job(s) of yours in the queue")
    except RemoteError as exc:
        report("FAIL", "slurm", str(exc))

    for root in cfg.remote_roots:
        try:
            ok = cluster._probe(cmd.test_dir_cmd(root))
        except RemoteError as exc:
            report("FAIL", f"root {root}", str(exc))
            continue
        report("OK" if ok else "FAIL", f"root {root}", "" if ok else "not a directory on the cluster")

    if cfg.presets:
        raw = cluster._ssh(["sinfo", "-h", "-o", "%P"], allow_failure=True)
        known = {p.strip().rstrip("*") for p in (raw or "").splitlines() if p.strip()}
        for p in cfg.presets.values():
            missing = [x for x in p.partition.split(",") if known and x not in known]
            if missing:
                report("WARN", f"preset {p.name}", f"partition(s) not found on this cluster: {', '.join(missing)}")
            if p.gpu:
                spec = resolve_spec(p, cfg.gpu_types, gpu=True, command=["x"], workdir=cfg.remote_roots[0],
                                    job_name="check")
                try:
                    for w in cluster.check_gpu_availability(spec):
                        report("WARN", f"preset {p.name}", w)
                except SlurmMcpError as exc:
                    report("FAIL", f"preset {p.name}", str(exc))
        report("OK", "presets", ", ".join(cfg.presets))
    else:
        report("WARN", "presets", "none configured, only submit_job_dry_run (existing scripts) can be used")

    if cfg.enable_writes:
        exists = cluster._probe(cmd.test_dir_cmd(cfg.scripts_dir))
        report("OK" if exists else "WARN", "jobs folder", cfg.scripts_dir + ("" if exists else " (will be created)"))

    if smoke:
        fails += _smoke(cluster, cfg, preset, out, input_fn, sleep, poll_s, wait_s)
    else:
        out("Read-only checks done. Run `slurm-mcp doctor --smoke` to submit a tiny test job.")
    out("All checks passed." if not fails else f"{fails} check(s) failed.")
    return 1 if fails else 0


def _smoke(cluster: Cluster, cfg: Config, preset: str, out, input_fn, sleep, poll_s, wait_s) -> int:
    out("\n--- smoke test: one tiny real job ---")
    chosen = cfg.presets.get(preset)
    if not cfg.enable_writes or chosen is None:
        out(f"[FAIL] smoke test needs enable_writes = true and a preset named {preset!r}")
        return 1
    if not cluster._probe(cmd.test_dir_cmd(cfg.scripts_dir)):
        if not _yes(input_fn(f"Create the folder {cfg.scripts_dir} for job scripts? (y/N): ")):
            out("Stopped: nothing was created or submitted.")
            return 1
        cluster.ensure_scripts_dir()
    plan = cluster.plan_submit_preset(preset, chosen.gpu, SMOKE_COMMAND, cfg.scripts_dir, "slurm-mcp-smoke")
    out(f"\nSummary: {plan['summary']}")
    for w in plan["warnings"]:
        out(f"Warning: {w}")
    out(f"Script (will be written to {plan['script_will_be_written_to']}):\n")
    out(plan["script"])
    out(f"Command: {plan['command']}")
    if not _yes(input_fn("Submit this job now? (y/N): ")):
        out("Not submitted.")
        return 0
    job_id = cluster.submit(plan["confirmation_token"])["job_id"]
    out(f"Submitted job {job_id}. Waiting up to {wait_s:g}s ...")

    state, waited = "", 0.0
    while waited <= wait_s:
        try:
            rows = cluster.job_status(job_id)["accounting"]
            main = next((r for r in rows if "." not in r["jobid"]), None)
            now = main["state"].split()[0] if main else "PENDING"
        except SlurmMcpError:
            now = "PENDING"  # not visible in accounting yet
        if now != state:
            out(f"  state: {now}")
            state = now
        if state in TERMINAL_STATES:
            break
        sleep(poll_s)
        waited += poll_s
    else:
        out(f"[WARN] still {state} after {wait_s:g}s. The job {job_id} stays in the queue: check it with "
            f"the queue tool, or cancel it with cancel_job.")
        return 0

    verdict = cluster.diagnose_job(job_id, lines=10)
    for f in verdict["findings"]:
        out(f"  {f}")
    stdout = verdict["logs"].get("stdout", {}).get("text", "")
    if stdout:
        out("  output:\n" + "\n".join("    " + ln for ln in stdout.splitlines()))
    if state == "COMPLETED" and (not stdout or "smoke-ok" in stdout):
        out("[  OK] smoke test passed")
        return 0
    out("[FAIL] smoke test did not complete as expected")
    return 1
