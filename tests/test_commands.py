import pytest

from slurm_mcp import commands as c
from slurm_mcp.errors import SlurmMcpError


def test_ssh_uses_controlmaster_and_batch(cfg):
    argv = c.ssh_argv(cfg, ["squeue", "--me"])
    joined = " ".join(argv)
    assert argv[0] == "ssh" and argv[-2] == "alice@login.example.org"
    assert "ControlMaster=auto" in joined and f"ControlPath={cfg.control_dir}/%C" in joined
    assert "BatchMode=yes" in joined
    assert "BatchMode" not in " ".join(c.login_argv(cfg))  # login must be able to prompt


def test_remote_args_are_quoted(cfg):
    argv = c.ssh_argv(cfg, ["tail", "-n", "5", "--", "/scratch/a b"])
    assert argv[-1] == "tail -n 5 -- '/scratch/a b'"


@pytest.mark.parametrize("bad", ["1; rm -rf /", "abc", "", "12 34", "$(id)", "1_", "-1"])
def test_job_id_validation(bad):
    with pytest.raises(SlurmMcpError):
        c.validate_job_id(bad)


def test_job_id_accepts_array():
    assert c.validate_job_id("123_4") == "123_4"
    assert c.sacct_job_cmd("123")[:3] == ["sacct", "-j", "123"]


def test_recent_days_clamped():
    assert "now-30days" in c.sacct_recent_cmd(999)
    assert "now-1days" in c.sacct_recent_cmd(-5)


def test_scp_uses_double_dash(cfg):
    argv = c.scp_download_argv(cfg, "/scratch/alice/x", "/tmp/x")
    assert argv[-3:] == ["--", "alice@login.example.org:/scratch/alice/x", "/tmp/x"]


def test_dir_links_cmd_is_quoted_for_the_remote_shell(cfg):
    line = c.ssh_argv(cfg, c.dir_links_cmd("/home/alice"))[-1]
    assert line == "find /home/alice -maxdepth 1 -type l -xtype d -printf '%f\\n' -exec readlink -f -- '{}' ';'"
