import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fakes import FakeRunner, make_config
from slurm_mcp import commands as c
from slurm_mcp.cluster import Cluster
from slurm_mcp.errors import PathError, SlurmMcpError
from slurm_mcp.server import build_server

SCRIPT = "/scratch/alice/run/job.sh"
QUEUE = "42|RUNNING|cpu|1:00|1:00:00|1|n1|t|sim\n43_1|PENDING|cpu|0:00|1:00|1|Priority|t|arr\n43_2|PENDING|cpu|0:00|1:00|1|Priority|t|arr\n"


@pytest.fixture
def wcfg(tmp_path):
    return make_config(tmp_path, enable_writes=True)


def wcluster(cfg, **replies):
    base = {"test -f": ("", 0), "head -c": ("#!/bin/bash\n#SBATCH -n 1\n", 0), "sbatch": ("1234\n", 0),
            "squeue": (QUEUE, 0), "scancel": ("", 0)}
    base.update(replies)
    return Cluster(cfg, FakeRunner(ssh_replies=base))


def test_writes_disabled_by_default(cfg):
    cl = Cluster(cfg, FakeRunner())
    for call in (lambda: cl.plan_submit(SCRIPT), lambda: cl.plan_cancel("42"),
                 lambda: cl.submit("x"), lambda: cl.cancel("x")):
        with pytest.raises(SlurmMcpError, match="disabled"):
            call()
    assert cl.runner.calls == []


def test_submit_dry_run_changes_nothing_and_shows_the_command(wcfg):
    cl = wcluster(wcfg)
    plan = cl.plan_submit(SCRIPT)
    assert plan["command"] == f"sbatch --parsable --chdir=/scratch/alice/run {SCRIPT}"
    assert "#SBATCH" in plan["script_preview"] and plan["confirmation_token"]
    assert not any(x.startswith("sbatch") for x in cl.runner.remote_commands())


def test_submit_runs_exactly_the_dry_run_command_once(wcfg):
    cl = wcluster(wcfg)
    tok = cl.plan_submit(SCRIPT)["confirmation_token"]
    out = cl.submit(tok)
    assert out["job_id"] == "1234"
    sbatch_calls = [x for x in cl.runner.remote_commands() if x.startswith("sbatch")]
    assert sbatch_calls == [f"sbatch --parsable --chdir=/scratch/alice/run {SCRIPT}"]
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        cl.submit(tok)  # no double submit


def test_submit_rejects_bad_scripts_before_any_token(wcfg):
    cl = wcluster(wcfg)
    with pytest.raises(PathError):
        cl.plan_submit("/etc/cron.daily/x.sh")
    with pytest.raises(PathError):
        cl.plan_submit("/scratch/alice/run/a;rm -rf ~")
    assert cl.runner.calls == []
    missing = wcluster(wcfg, **{"test -f": ("", 1)})
    with pytest.raises(SlurmMcpError, match="not a regular file"):
        missing.plan_submit(SCRIPT)


def test_submit_rejects_unexpected_sbatch_output(wcfg):
    cl = wcluster(wcfg, sbatch=("Submitted batch job 1\n", 0))
    tok = cl.plan_submit(SCRIPT)["confirmation_token"]
    with pytest.raises(SlurmMcpError, match="unexpected output"):
        cl.submit(tok)


def test_submit_accepts_parsable_cluster_suffix(wcfg):
    cl = wcluster(wcfg, sbatch=("1234;baobab\n", 0))
    assert cl.submit(cl.plan_submit(SCRIPT)["confirmation_token"])["job_id"] == "1234"


def test_cancel_only_own_active_jobs(wcfg):
    cl = wcluster(wcfg)
    with pytest.raises(SlurmMcpError, match="not one of your"):
        cl.plan_cancel("999")
    with pytest.raises(SlurmMcpError):
        cl.plan_cancel("42; rm -rf ~")
    assert not any(x.startswith("scancel") for x in cl.runner.remote_commands())


def test_cancel_two_steps(wcfg):
    cl = wcluster(wcfg)
    plan = cl.plan_cancel("43")
    assert [r["job_id"] for r in plan["would_cancel"]] == ["43_1", "43_2"]  # whole array
    assert not any(x.startswith("scancel") for x in cl.runner.remote_commands())
    cl.cancel(plan["confirmation_token"])
    assert [x for x in cl.runner.remote_commands() if x.startswith("scancel")] == ["scancel 43"]


def test_cancel_single_array_task(wcfg):
    cl = wcluster(wcfg)
    assert [r["job_id"] for r in cl.plan_cancel("43_2")["would_cancel"]] == ["43_2"]


def test_token_for_submit_cannot_cancel(wcfg):
    cl = wcluster(wcfg)
    tok = cl.plan_submit(SCRIPT)["confirmation_token"]
    with pytest.raises(SlurmMcpError, match="not cancel"):
        cl.cancel(tok)


def test_command_builders_are_fixed_templates():
    assert c.scancel_cmd("5") == ["scancel", "5"]
    assert c.sbatch_cmd("/a/b/c.sh") == ["sbatch", "--parsable", "--chdir=/a/b", "/a/b/c.sh"]


def test_server_exposes_write_tools_only_when_enabled(cfg, wcfg):
    off = {t.name for t in asyncio.run(build_server(Cluster(cfg, FakeRunner())).list_tools())}
    on = {t.name: t for t in asyncio.run(build_server(wcluster(wcfg)).list_tools())}
    assert not {"submit_job", "cancel_job"} & off
    assert {"submit_job_dry_run", "submit_job", "cancel_job_dry_run", "cancel_job"} <= set(on)
    assert on["cancel_job"].annotations.destructive_hint is True
    assert on["submit_job"].annotations.read_only_hint is False
    assert on["submit_job_dry_run"].annotations.read_only_hint is True
    for name in ("submit_job", "cancel_job"):  # the real calls take only a token
        assert list(on[name].input_schema["properties"]) == ["confirmation_token"]


def test_full_flow_through_server_with_messages(wcfg):
    server = build_server(wcluster(wcfg))
    with pytest.raises(ToolError, match="unknown or expired"):
        asyncio.run(server.call_tool("submit_job", {"confirmation_token": "guess"}))
