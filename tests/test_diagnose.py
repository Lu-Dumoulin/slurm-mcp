import pytest

from fakes import FakeRunner
from slurm_mcp.cluster import Cluster
from slurm_mcp.diagnose import diagnose, parse_size_mb

ROW = dict(jobid="42", state="COMPLETED", exitcode="0:0", elapsed="00:10:00", reqmem="8G", maxrss="",
           partition="cpu", nodelist="n1", start="", end="", alloccpus="4", jobname="sim")


def rows(state="COMPLETED", exitcode="0:0", maxrss="1G", reqmem="8G"):
    main = {**ROW, "state": state, "exitcode": exitcode, "reqmem": reqmem}
    step = {**ROW, "jobid": "42.batch", "state": state, "maxrss": maxrss, "reqmem": ""}
    return [main, step]


def test_size_parsing():
    assert parse_size_mb("1024K") == 1 and parse_size_mb("3.5G") == 3584 and parse_size_mb("8Gn") == 8192
    assert parse_size_mb("3000Mc") == 3000 and parse_size_mb("") is None and parse_size_mb("abc") is None


@pytest.mark.parametrize("state,code,needle", [
    ("TIMEOUT", "0:0", "time limit"),
    ("OUT_OF_MEMORY", "0:125", "memory request"),
    ("FAILED", "1:0", "exit code 1"),
    ("FAILED", "0:9", "signal 9"),
    ("CANCELLED by 1234", "0:0", "cancelled"),
    ("NODE_FAIL", "0:0", "node failed"),
    ("COMPLETED", "0:0", "exit code 0"),
])
def test_verdicts(state, code, needle):
    out = diagnose(rows(state, code), None)
    assert needle in " ".join(out["findings"])


def test_pending_reason_is_explained():
    out = diagnose([{**ROW, "state": "PENDING"}], {"Reason": "PartitionTimeLimit"})
    assert "never start" in out["findings"][0]
    assert "waiting" in diagnose([{**ROW, "state": "PENDING"}], {"Reason": "Resources"})["findings"][0]


def test_memory_tight_and_over_requested():
    tight = diagnose(rows(maxrss="7.5G"), None)
    assert tight["memory"]["ratio"] > 0.9 and any("tight" in f for f in tight["findings"])
    loose = diagnose(rows(maxrss="200M", reqmem="8G"), None)
    assert any("over-requested" in f for f in loose["findings"])
    assert diagnose(rows(maxrss=""), None)["memory"] is None  # no data, no claim


def test_unknown_job():
    assert diagnose([], None)["state"] == "UNKNOWN"


SACCT = ("42|TIMEOUT|0:0|00:30:00|2026-01-01T10:00:00|2026-01-01T10:30:00|cpu|n1|4|8G||sim\n"
         "42.batch|CANCELLED|0:15|00:30:00|2026-01-01T10:00:00|2026-01-01T10:30:00||n1|4||1G|batch\n")
SCONTROL = "JobId=42 JobName=sim JobState=TIMEOUT TimeLimit=00:30:00 StdOut=/scratch/alice/run/o.out StdErr=/scratch/alice/run/e.err"


def test_diagnose_job_combines_verdict_and_logs(cfg):
    runner = FakeRunner(ssh_replies={"sacct": (SACCT, 0), "scontrol": (SCONTROL, 0),
                                     "e.err": ("Killed\n", 0), "o.out": ("step 100\n", 0)})
    out = Cluster(cfg, runner).diagnose_job("42")
    assert out["state"] == "TIMEOUT" and "limit 00:30:00" in out["findings"][0]
    assert out["logs"]["stderr"]["text"] == "Killed\n" and out["logs"]["stdout"]["text"] == "step 100\n"


def test_diagnose_job_survives_an_unreadable_log(cfg):
    evil = SCONTROL.replace("/scratch/alice/run/e.err", "/etc/shadow")
    runner = FakeRunner(ssh_replies={"sacct": (SACCT, 0), "scontrol": (evil, 0), "o.out": ("ok\n", 0)})
    out = Cluster(cfg, runner).diagnose_job("42")
    assert "outside the configured roots" in out["logs"]["stderr"]["note"]
    assert out["logs"]["stdout"]["text"] == "ok\n"
    assert not any("shadow" in c for c in runner.remote_commands())
