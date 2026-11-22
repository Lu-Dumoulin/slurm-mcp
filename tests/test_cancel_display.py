import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.diagnose import clean_reason, never_started
from slurm_mcp.errors import RemoteFileNotFound
from slurm_mcp.parsing import parse_scontrol, parse_squeue
from slurm_mcp.runner import Result

# A real scontrol line shape from Baobab: a key with a colon, a name with a space, a quoted-looking value
LINE = ("JobId=14502013 JobName=my job UserId=dumoulil(372536) GroupId=hpc_users(1) Priority=1 Nice=0 "
        "JobState=PENDING Reason=(None) Dependency=(null) RunTime=00:00:00 TimeLimit=00:15:00 "
        "SubmitTime=2026-11-22T13:44:00 Partition=public-short-cpu AllocNode:Sid=login1:3988852 "
        "NumCPUs=1 WorkDir=/home/users/d/dumoulil/slurm-mcp-jobs "
        "StdOut=/home/users/d/dumoulil/slurm-mcp-jobs/slurm-mcp-smoke-14502013.out "
        "Command=/home/users/d/dumoulil/slurm-mcp-jobs/job-1.sh")


def test_scontrol_key_with_a_colon_does_not_glue_onto_partition():
    d = parse_scontrol(LINE)
    assert d["Partition"] == "public-short-cpu"
    assert d["AllocNode:Sid"] == "login1:3988852"
    assert d["JobName"] == "my job" and d["NumCPUs"] == "1"
    assert d["Command"].endswith("job-1.sh") and d["StdOut"].endswith("-14502013.out")


@pytest.mark.parametrize("raw", ["None", "(None)", "(null)", "", None, " (none) "])
def test_clean_reason_treats_all_none_forms_alike(raw):
    assert clean_reason(raw) == ""
    assert clean_reason("Priority") == "Priority" and clean_reason("(Resources)") == "(Resources)"


def test_squeue_reason_none_forms_are_normalised():
    rows = parse_squeue("7|PENDING|cpu|0:00|1:00|1|(None)|t|x\n8|PENDING|cpu|0:00|1:00|1|Priority|t|y\n")
    assert [r["reason"] for r in rows] == ["", "Priority"]


SACCT_PENDING = "42|PENDING|0:0|00:00:00|Unknown|Unknown|debug-cpu|None assigned|0|2G||sim\n"
SCONTROL_PENDING = "JobId=42 JobName=sim JobState=PENDING Reason=(None) Partition=debug-cpu AllocNode:Sid=login1:1 NumCPUs=1"


def cluster(cfg, **replies):
    return Cluster(cfg, FakeRunner(ssh_replies=replies))


def test_job_status_drops_the_parenthesised_none_and_falls_back_to_squeue(cfg):
    cl = cluster(cfg, sacct=(SACCT_PENDING, 0), scontrol=(SCONTROL_PENDING, 0),
                 squeue=("42|PENDING|debug-cpu|0:00|15:00|1|Priority|t|sim\n", 0))
    status = cl.job_status("42")["scontrol"]
    assert status["Reason"] == "Priority" and status["Partition"] == "debug-cpu"
    none = cluster(cfg, sacct=(SACCT_PENDING, 0), scontrol=(SCONTROL_PENDING, 0),
                   squeue=("42|PENDING|debug-cpu|0:00|15:00|1|(None)|t|sim\n", 0))
    assert "Reason" not in none.job_status("42")["scontrol"]
    assert none.diagnose_job("42")["findings"] == ["pending"]


def test_cancel_dry_run_does_not_show_a_none_reason(tmp_path):
    cfg = make_config(tmp_path, enable_writes=True)
    cl = cluster(cfg, squeue=("42|PENDING|debug-cpu|0:00|15:00|1|(None)|t|sim\n", 0))
    row = cl.plan_cancel("42")["would_cancel"][0]
    assert row["reason"] == ""


# --- jobs that never started / missing log files -------------------------------------------

CANCELLED_BEFORE_START = "42|CANCELLED by 372536|0:0|00:00:00|Unknown|2026-11-22T13:44:30|debug-cpu|None assigned|0|2G||sim\n"
RAN = "42|COMPLETED|0:0|00:00:03|2026-01-01T10:00:00|2026-01-01T10:00:03|debug-cpu|n1|1|2G||sim\n"
PATHS = "/scratch/alice/work|/scratch/alice/work/%x-%j.out|\n"


def test_never_started_helper():
    assert never_started([{"jobid": "42", "start": "Unknown"}])
    assert never_started([{"jobid": "42", "start": "None"}])
    assert not never_started([{"jobid": "42", "start": "2026-01-01T10:00:00"}])
    assert not never_started([])


def test_cancelled_before_start_says_so_and_does_not_tail(cfg):
    cl = cluster(cfg, **{"StdOut,StdErr": (PATHS, 0)}, sacct=(CANCELLED_BEFORE_START, 0), scontrol=("", 1))
    out = cl.diagnose_job("42")
    assert out["logs"]["stdout"]["note"] == "the job never started, so there are no logs"
    assert out["logs"]["stderr"]["note"] == "the job never started, so there are no logs"
    assert out["state"] == "CANCELLED" and "cancelled" in out["findings"][0]
    assert not any(c.startswith("tail") for c in cl.runner.remote_commands())


class NoFile(FakeRunner):
    def run(self, argv, timeout):
        if argv[0] == "ssh" and argv[-1].startswith("tail"):
            self.calls.append(list(argv))
            return Result(tuple(argv), 1, "", "tail: cannot open '/x' for reading: No such file or directory")
        return super().run(argv, timeout)


def test_missing_log_of_a_job_that_ran_gives_a_short_note(cfg):
    cl = Cluster(cfg, NoFile(ssh_replies={"StdOut,StdErr": (PATHS, 0), "sacct": (RAN, 0), "scontrol": ("", 1)}))
    out = cl.diagnose_job("42")
    assert out["logs"]["stdout"]["note"] == "log file not found at /scratch/alice/work/sim-42.out"
    assert "tail:" not in str(out["logs"]) and "exit 1" not in str(out["logs"])


def test_tail_file_raises_a_clear_not_found_error(cfg):
    cl = Cluster(cfg, NoFile())
    with pytest.raises(RemoteFileNotFound, match="file not found at /scratch/alice/x.log"):
        cl.tail_file("/scratch/alice/x.log")


def test_other_tail_failures_keep_their_own_message(cfg):
    cl = cluster(cfg, tail=("", 1))
    with pytest.raises(Exception, match="remote command failed"):
        cl.tail_file("/scratch/alice/x.log")
