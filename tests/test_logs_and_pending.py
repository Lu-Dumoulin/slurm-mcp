import pytest

from fakes import FakeRunner
from slurm_mcp.cluster import Cluster
from slurm_mcp.diagnose import diagnose, expand_log_pattern, resolve_log_paths
from slurm_mcp.errors import SlurmMcpError
from slurm_mcp.parsing import parse_sacct_paths

PENDING_ROW = "42|PENDING|0:0|00:00:00|Unknown|Unknown|debug-cpu|None assigned|0|2G||sim\n"
DONE_ROW = "42|COMPLETED|0:0|00:00:03|2026-01-01T10:00:00|2026-01-01T10:00:03|debug-cpu|n1|1|2G||sim\n"
QUEUE_PRIORITY = "42|PENDING|debug-cpu|0:00|15:00|1|Priority|2026-01-01T10:00:00|sim\n"
QUEUE_NONE = "42|PENDING|debug-cpu|0:00|15:00|1|None|2026-01-01T10:00:00|sim\n"


def scontrol(state="PENDING", reason="None", **more):
    extra = "".join(f" {k}={v}" for k, v in more.items())
    return f"JobId=42 JobName=sim JobState={state} Reason={reason}{extra}"


def cluster(cfg, **replies):
    return Cluster(cfg, FakeRunner(ssh_replies=replies))


# --- pending reason ---------------------------------------------------------------------

def test_pending_without_a_reason_says_just_pending():
    for none in ("None", "", None, "(null)"):
        out = diagnose([{"jobid": "42", "state": "PENDING", "exitcode": "0:0"}], {"Reason": none})
        assert out["findings"] == ["pending"]


def test_pending_reason_given_directly_beats_scontrol():
    out = diagnose([{"jobid": "42", "state": "PENDING"}], {"Reason": "None"}, reason="Resources")
    assert out["findings"][0].startswith("pending (Resources): waiting for the requested resources")
    bad = diagnose([{"jobid": "42", "state": "PENDING"}], None, reason="BadConstraints")
    assert "never start" in bad["findings"][0]


def test_job_status_takes_the_reason_from_squeue_when_scontrol_says_none(cfg):
    cl = cluster(cfg, sacct=(PENDING_ROW, 0), scontrol=(scontrol(), 0), squeue=(QUEUE_PRIORITY, 0))
    assert cl.job_status("42")["scontrol"]["Reason"] == "Priority"
    assert cl.diagnose_job("42")["findings"][0].startswith("pending (Priority)")


def test_job_status_drops_a_useless_none_reason(cfg):
    cl = cluster(cfg, sacct=(PENDING_ROW, 0), scontrol=(scontrol(), 0), squeue=(QUEUE_NONE, 0))
    assert "Reason" not in cl.job_status("42")["scontrol"]
    assert cl.diagnose_job("42")["findings"] == ["pending"]


def test_a_real_scontrol_reason_needs_no_squeue_call(cfg):
    cl = cluster(cfg, sacct=(PENDING_ROW, 0), scontrol=(scontrol(reason="Resources"), 0))
    assert cl.job_status("42")["scontrol"]["Reason"] == "Resources"
    assert not any(c.startswith("squeue") for c in cl.runner.remote_commands())


def test_pending_job_has_no_logs_yet_and_no_tail_is_attempted(cfg):
    cl = cluster(cfg, sacct=(PENDING_ROW, 0), scontrol=(scontrol(StdOut="/scratch/alice/o.out"), 0),
                 squeue=(QUEUE_PRIORITY, 0))
    out = cl.diagnose_job("42")
    assert "not started yet" in out["logs"]["stdout"]["note"] and "not started yet" in out["logs"]["stderr"]["note"]
    assert not any(c.startswith("tail") for c in cl.runner.remote_commands())


# --- stderr and stdout paths --------------------------------------------------------------

PATHS_MERGED = "/scratch/alice/work|/scratch/alice/work/%x-%j.out|\n"


def finished(cfg, paths=PATHS_MERGED, **more):
    replies = {"StdOut,StdErr": (paths, 0), "sacct": (DONE_ROW, 0), "scontrol": ("", 1),
               "sim-42.out": ("line1\nsmoke-ok\n", 0)}
    replies.update(more)
    return cluster(cfg, **replies)


def test_finished_job_without_error_file_reports_stderr_as_merged(cfg):
    cl = finished(cfg)
    out = cl.diagnose_job("42")
    assert out["logs"]["stdout"]["path"] == "/scratch/alice/work/sim-42.out"   # %x-%j expanded
    assert out["logs"]["stdout"]["text"] == "line1\nsmoke-ok\n"
    assert out["logs"]["stderr"]["merged_into_stdout"] is True
    assert "merged into the stdout log" in out["logs"]["stderr"]["note"]
    assert "no longer known" not in str(out["logs"])
    assert sum(c.startswith("tail") for c in cl.runner.remote_commands()) == 1  # not repeated


def test_scontrol_reporting_the_same_file_for_both_streams_is_merged(cfg):
    live = scontrol("RUNNING", "None", StdOut="/scratch/alice/work/o.out", StdErr="/scratch/alice/work/o.out")
    cl = cluster(cfg, sacct=(DONE_ROW, 0), scontrol=(live, 0), **{"o.out": ("hello\n", 0)})
    out = cl.diagnose_job("42")
    assert out["logs"]["stderr"]["merged_into_stdout"] is True
    assert sum(c.startswith("tail") for c in cl.runner.remote_commands()) == 1


def test_separate_error_file_is_tailed_too(cfg):
    paths = "/scratch/alice/work|/scratch/alice/work/%x-%j.out|/scratch/alice/work/%x-%j.err\n"
    cl = finished(cfg, paths, **{"sim-42.err": ("warning\n", 0)})
    out = cl.diagnose_job("42")
    assert out["logs"]["stderr"]["text"] == "warning\n" and "merged_into_stdout" not in out["logs"]["stderr"]


def test_separate_error_file_with_unknown_path_keeps_the_old_note(cfg):
    paths = "/scratch/alice/work|/scratch/alice/work/%x-%j.out|/scratch/alice/work/%N.err\n"   # %N cannot be expanded
    out = finished(cfg, paths).diagnose_job("42")
    assert "no longer known" in out["logs"]["stderr"]["note"]
    assert out["logs"]["stdout"]["text"].endswith("smoke-ok\n")


def test_slurm_default_output_file_is_used_when_nothing_was_set(cfg):
    cl = finished(cfg, "/scratch/alice/work||\n", **{"slurm-42.out": ("default\n", 0)})
    out = cl.diagnose_job("42")
    assert out["logs"]["stdout"]["path"] == "/scratch/alice/work/slurm-42.out"
    assert out["logs"]["stderr"]["merged_into_stdout"] is True


def test_nothing_known_says_so_for_stdout(cfg):
    cl = finished(cfg, "", **{"StdOut,StdErr": ("", 1)})
    out = cl.diagnose_job("42")
    assert "not available from Slurm" in out["logs"]["stdout"]["note"]
    assert "no longer known" in out["logs"]["stderr"]["note"]


def test_log_outside_the_roots_is_still_refused(cfg):
    cl = finished(cfg, "/etc|/etc/%x-%j.out|\n")
    out = cl.diagnose_job("42")
    assert "outside the configured roots" in out["logs"]["stdout"]["note"]
    assert not any(c.startswith("tail") for c in cl.runner.remote_commands())


def test_job_log_err_on_a_merged_job_returns_the_stdout_file_with_a_note(cfg):
    out = finished(cfg).job_log("42", "err", 10)
    assert out["path"].endswith("sim-42.out") and "merged" in out["note"]


def test_job_log_without_any_path_raises(cfg):
    cl = finished(cfg, "", **{"StdOut,StdErr": ("", 1)})
    with pytest.raises(SlurmMcpError, match="not known"):
        cl.job_log("42")


# --- pure helpers -------------------------------------------------------------------------

def test_expand_log_pattern():
    assert expand_log_pattern("%x-%j.out", "42", "sim", "alice") == "sim-42.out"
    assert expand_log_pattern("%u/%A_%a.log", "42_7", "sim", "alice") == "alice/42_7.log"
    assert expand_log_pattern("100%%.out", "42", "sim", "alice") == "100%.out"
    assert expand_log_pattern("%j.out", "42_7", "sim", "alice") is None   # job id of an array task unknown
    assert expand_log_pattern("%N.out", "42", "sim", "alice") is None


def test_resolve_log_paths_prefers_live_values_and_joins_relative_names():
    live = {"StdOut": "/w/a.out", "StdErr": "/w/b.err", "WorkDir": "/w"}
    assert resolve_log_paths(live, None, "42", "sim", "u").stderr == "/w/b.err"
    rel = resolve_log_paths(None, {"workdir": "/w", "stdout": "out/%j.txt", "stderr": ""}, "42", "sim", "u")
    assert rel.stdout == "/w/out/42.txt" and rel.merged
    assert resolve_log_paths(None, None, "42", "sim", "u").stdout is None


def test_parse_sacct_paths():
    assert parse_sacct_paths("/w|/w/%j.out|\n") == {"workdir": "/w", "stdout": "/w/%j.out", "stderr": ""}
    assert parse_sacct_paths("") is None and parse_sacct_paths("garbage\n") is None
