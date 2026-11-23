import os

import pytest

from fakes import FakeRunner
from slurm_mcp.cluster import Cluster
from slurm_mcp.errors import PathError, RemoteError, SlurmMcpError

SACCT = "42|RUNNING|0:0|00:01:00|2026-01-01T10:00:00|Unknown|cpu|n1|4|8G||sim\n"
SCONTROL = "JobId=42 JobName=sim JobState=RUNNING StdOut=/scratch/alice/run/slurm-42.out StdErr=/scratch/alice/run/slurm-42.err UserId=alice(1)"


def cluster(cfg, **kw):
    return Cluster(cfg, FakeRunner(**kw))


def test_queue(cfg):
    c = cluster(cfg, ssh_replies={"squeue": ("42|RUNNING|cpu|1:00|1:00:00|1|n1|t|sim\n", 0)})
    assert c.queue()[0]["job_id"] == "42"
    assert c.runner.remote_commands() == [c.runner.remote_commands()[0]]
    assert c.runner.remote_commands()[0].startswith("squeue --me")


def test_every_call_goes_through_the_shared_connection(cfg):
    c = cluster(cfg, ssh_replies={"squeue": ("", 0)})
    c.queue()
    assert "ControlMaster=auto" in " ".join(c.runner.calls[0])


def test_job_status_merges_sacct_and_scontrol(cfg):
    c = cluster(cfg, ssh_replies={"sacct": (SACCT, 0), "scontrol": (SCONTROL, 0)})
    st = c.job_status("42")
    assert st["accounting"][0]["state"] == "RUNNING"
    assert st["scontrol"]["StdOut"].endswith("slurm-42.out")
    assert "UserId" not in st["scontrol"]  # only whitelisted keys


def test_job_status_after_scontrol_forgets_it(cfg):
    c = cluster(cfg, ssh_replies={"sacct": (SACCT, 0), "scontrol": ("", 1)})
    assert c.job_status("42")["scontrol"] is None


def test_job_status_unknown_job(cfg):
    c = cluster(cfg, ssh_replies={"sacct": ("", 0), "scontrol": ("", 1)})
    with pytest.raises(SlurmMcpError, match="not found"):
        c.job_status("999")


def test_job_status_rejects_injection_before_any_ssh(cfg):
    c = cluster(cfg)
    with pytest.raises(SlurmMcpError):
        c.job_status("1; rm -rf ~")
    assert c.runner.calls == []


def test_job_log_resolves_path_and_clamps_lines(cfg):
    c = cluster(cfg, ssh_replies={"sacct": (SACCT, 0), "scontrol": (SCONTROL, 0),
                                  "tail": ("line1\nline2\n", 0)})
    out = c.job_log("42", "err", lines=10_000)
    assert out["path"].endswith("slurm-42.err")
    assert out["lines_requested"] == cfg.max_tail_lines
    assert c.runner.remote_commands()[-1].startswith(f"tail -n {cfg.max_tail_lines} --")


def test_job_log_refuses_path_outside_roots(cfg):
    evil = SCONTROL.replace("/scratch/alice/run/slurm-42.out", "/etc/shadow")
    c = cluster(cfg, ssh_replies={"sacct": (SACCT, 0), "scontrol": (evil, 0)})
    with pytest.raises(PathError):
        c.job_log("42")
    assert not any(x.startswith("tail") for x in c.runner.remote_commands())


def test_tail_file_outside_roots(cfg):
    c = cluster(cfg)
    with pytest.raises(PathError):
        c.tail_file("/etc/passwd")
    assert c.runner.calls == []


def test_ssh_failure_message_points_to_login(cfg):
    c = cluster(cfg, ssh_replies={"squeue": ("", 255)})
    with pytest.raises(RemoteError, match="slurm-mcp login"):
        c.queue()


def test_list_files_filters_and_truncates(tmp_path):
    from fakes import make_config
    cfg = make_config(tmp_path, max_list_entries=2)
    listing = "f\ta.csv\t5\t100.0\nf\tb.csv\t6\t101.0\nf\tc.csv\t7\t102.0\nf\td.txt\t1\t1.0\nd\tsub.csv\t4096\t3.0\n"
    c = cluster(cfg, ssh_replies={"find": (listing, 0)})
    out = c.list_files("/scratch/alice/run", pattern="*.csv")
    assert out["count"] == 4 and out["truncated"] and len(out["files"]) == 2


def test_read_parameter_table(cfg):
    c = cluster(cfg, ssh_replies={"head": ("id,alpha\n1,0.1\n2,0.2\n", 0)})
    out = c.read_parameter_table("/scratch/alice/sim/DF.csv", max_rows=1)
    assert out["columns"] == ["id", "alpha"] and out["rows"] == [{"id": "1", "alpha": "0.1"}]
    assert out["truncated"]
    with pytest.raises(SlurmMcpError, match="df_csv"):
        c.read_parameter_table()


LISTING = "f\tr/a.csv\t3\t1700000000.0\nf\tr/b.csv\t4\t1700000100.0\nf\tr/bad name.csv\t1\t1.0\nd\tr\t4096\t1.0\n"
REMOTE = {"/scratch/alice/run/r/a.csv": (b"aaa", 1700000000.0),
          "/scratch/alice/run/r/b.csv": (b"bbbb", 1700000100.0)}


def test_fetch_is_incremental(cfg):
    c = cluster(cfg, ssh_replies={"find": (LISTING, 0)}, remote_files=REMOTE)
    first = c.fetch_results("/scratch/alice/run", dry_run=False)
    assert first["downloaded"] == ["r/a.csv", "r/b.csv"]
    assert first["skipped_unsafe_names"] == 1
    dest = cfg.local_dir / "scratch/alice/run/r/a.csv"
    assert dest.read_bytes() == b"aaa" and int(os.stat(dest).st_mtime) == 1700000000
    assert not list(cfg.local_dir.rglob("*.part"))

    scp_before = sum(1 for x in c.runner.calls if x[0] == "scp")
    second = c.fetch_results("/scratch/alice/run", dry_run=False)
    assert second["downloaded"] == [] and second["up_to_date"] == 2
    assert sum(1 for x in c.runner.calls if x[0] == "scp") == scp_before


def test_fetch_picks_up_only_changed_file(cfg):
    c = cluster(cfg, ssh_replies={"find": (LISTING, 0)}, remote_files=REMOTE)
    c.fetch_results("/scratch/alice/run", dry_run=False)
    c.runner.ssh_replies["find"] = (LISTING.replace("r/b.csv\t4\t1700000100.0", "r/b.csv\t6\t1700000500.0"), 0)
    c.runner.remote_files["/scratch/alice/run/r/b.csv"] = (b"bbbbbb", 1700000500.0)
    again = c.fetch_results("/scratch/alice/run", dry_run=False)
    assert again["downloaded"] == ["r/b.csv"]


def test_fetch_dry_run_writes_nothing(cfg):
    c = cluster(cfg, ssh_replies={"find": (LISTING, 0)}, remote_files=REMOTE)
    out = c.fetch_results("/scratch/alice/run", dry_run=True)
    assert len(out["to_download"]) == 2 and out["downloaded"] == []
    assert not cfg.local_dir.exists()
    assert not any(x[0] == "scp" for x in c.runner.calls)


def test_fetch_size_cap(tmp_path):
    from fakes import make_config
    cfg = make_config(tmp_path, max_download_mb=1)
    c = cluster(cfg, ssh_replies={"find": (f"f\tbig.bin\t{2*1024*1024}\t1.0\n", 0)})
    with pytest.raises(SlurmMcpError, match="refusing"):
        c.fetch_results("/scratch/alice/run", dry_run=False)


def test_fetch_failed_file_leaves_no_partial(cfg):
    c = cluster(cfg, ssh_replies={"find": (LISTING, 0)}, remote_files={k: v for k, v in REMOTE.items() if k.endswith("a.csv")})
    out = c.fetch_results("/scratch/alice/run", dry_run=False)
    assert out["downloaded"] == ["r/a.csv"] and out["failed"][0]["path"] == "r/b.csv"
    assert not list(cfg.local_dir.rglob("*.part"))


def test_list_files_reports_directories(cfg):
    c = cluster(cfg, ssh_replies={"find": ("d\tsim1\t4096\t5.0\nd\tsim2\t4096\t6.0\n", 0)})
    out = c.list_files("/scratch/alice")
    assert [(e["path"], e["type"]) for e in out["files"]] == [("sim1", "dir"), ("sim2", "dir")]
    assert all("size" not in e for e in out["files"])


def test_auto_connect_default_opens_connection_in_the_same_call(cfg):
    c = cluster(cfg, ssh_replies={"squeue": ("", 0)})
    c.queue()
    assert not any("-O" in call for call in c.runner.calls)  # no separate check, ControlMaster=auto does it


def test_auto_connect_off_never_logs_in_on_its_own(tmp_path):
    from fakes import make_config
    cfg = make_config(tmp_path, auto_connect=False)
    c = cluster(cfg, ssh_replies={"squeue": ("", 0)})
    with pytest.raises(RemoteError, match="auto_connect is off"):
        c.queue()
    assert not any(call[-1] == "squeue --me" or "squeue" in call[-1] for call in c.runner.calls)
    c.runner.ssh_replies["alice@login"] = ("", 0)  # the `ssh -O check` call now succeeds
    c.queue()


def test_permission_denied_suggests_ssh_add(cfg):
    from slurm_mcp.runner import Result

    class Denied(FakeRunner):
        def run(self, argv, timeout):
            return Result(tuple(argv), 255, "", "Permission denied (publickey).")

    with pytest.raises(RemoteError, match="ssh-add"):
        Cluster(cfg, Denied()).queue()
