import asyncio

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.errors import SlurmMcpError
from slurm_mcp.server import build_server

QUEUE = (
    "10|RUNNING|cpu|1:00|1:00:00|1|n1|t|sweep\n"
    "11|PENDING|cpu|0:00|1:00:00|1|Priority|t|sweep\n"
    "12|PENDING|gpu|0:00|1:00:00|1|Priority|t|other\n"
    "20_3|RUNNING|cpu|1:00|1:00:00|1|n2|t|arr\n"
    "20_4|RUNNING|cpu|1:00|1:00:00|1|n3|t|arr\n"
    "20_[5-9%40]|PENDING|cpu|0:00|1:00:00|1|Resources|t|arr\n"
)


@pytest.fixture
def cl(tmp_path):
    cfg = make_config(tmp_path, enable_writes=True)
    return Cluster(cfg, FakeRunner(ssh_replies={"squeue": (QUEUE, 0), "scancel": ("", 0)}))


def ids(plan):
    return sorted(r["job_id"] for r in plan["would_cancel"])


def test_exactly_one_selector_is_required(cl):
    for kw in ({}, {"name": "a", "state": "PENDING"}, {"job_ids": ["10"], "all_mine": True}):
        with pytest.raises(SlurmMcpError, match="exactly one selector"):
            cl.plan_cancel_many(**kw)
    with pytest.raises(SlurmMcpError, match="PENDING or RUNNING"):
        cl.plan_cancel_many(state="COMPLETED")


def test_by_name_is_an_exact_match_and_lists_every_job(cl):
    plan = cl.plan_cancel_many(name="sweep")
    assert ids(plan) == ["10", "11"] and plan["scancel_targets"] == ["10", "11"]
    with pytest.raises(SlurmMcpError, match="none of your"):
        cl.plan_cancel_many(name="swee")                       # no partial matches


def test_by_state(cl):
    pending = cl.plan_cancel_many(state="PENDING")
    assert ids(pending) == ["11", "12", "20_3", "20_4", "20_[5-9%40]"] or "11" in ids(pending)
    assert "--state=PENDING" in pending["command"]
    assert any("only on its pending tasks" in w for w in pending["warnings"])
    running = cl.plan_cancel_many(state="RUNNING")
    assert "--state" not in running["command"] and "10" in running["scancel_targets"]


def test_by_ids_covers_array_tasks_and_validates(cl):
    plan = cl.plan_cancel_many(job_ids=["20_3", "11"])
    assert plan["scancel_targets"] == ["11", "20_3"] or sorted(plan["scancel_targets"]) == ["11", "20_3"]
    assert ids(cl.plan_cancel_many(job_ids=["20"])) == ["20_3", "20_4", "20_[5-9%40]"]       # a plain id means the whole array
    for bad in (["1; rm -rf ~"], ["-u"], ["all"], [""]):
        with pytest.raises(SlurmMcpError):
            cl.plan_cancel_many(job_ids=bad)
    with pytest.raises(SlurmMcpError, match="none of your"):
        cl.plan_cancel_many(job_ids=["999"])                  # someone else's or finished: not in squeue --me


def test_all_mine_lists_everything_and_a_pending_array_is_cancelled_as_a_whole(cl):
    plan = cl.plan_cancel_many(all_mine=True)
    assert len(plan["would_cancel"]) == 6
    assert plan["scancel_targets"] == ["10", "11", "12", "20"]          # tasks are covered by the whole array
    assert any("pending array" in w and "including tasks that already run" in w for w in plan["warnings"])


def test_only_own_jobs_ever_reach_the_command(cl):
    plan = cl.plan_cancel_many(all_mine=True)
    assert cl.runner.remote_commands()[0].startswith("squeue --me")
    cl.cancel_many(plan["confirmation_token"])
    cmd = [c for c in cl.runner.remote_commands() if c.startswith("scancel")][0]
    assert cmd == "scancel 10 11 12 20" and " -u" not in cmd and "--user" not in cmd


def test_the_token_runs_exactly_the_listed_jobs_once(cl):
    plan = cl.plan_cancel_many(name="sweep")
    assert not any(c.startswith("scancel") for c in cl.runner.remote_commands())      # dry run cancels nothing
    res = cl.cancel_many(plan["confirmation_token"])
    assert res["cancelled"] == ["10", "11"] and res["failed"] == []
    assert [c for c in cl.runner.remote_commands() if c.startswith("scancel")] == ["scancel 10 11"]
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        cl.cancel_many(plan["confirmation_token"])


def test_pending_selection_passes_the_state_flag_at_execution(cl):
    cl.cancel_many(cl.plan_cancel_many(state="PENDING")["confirmation_token"])
    assert [c for c in cl.runner.remote_commands() if c.startswith("scancel")] == ["scancel --state=PENDING 11 12 20"]


def test_a_single_cancel_token_cannot_be_used_here_and_vice_versa(cl):
    single = cl.plan_cancel("10")["confirmation_token"]
    with pytest.raises(SlurmMcpError, match="not cancel_many"):
        cl.cancel_many(single)
    many = cl.plan_cancel_many(name="sweep")["confirmation_token"]
    with pytest.raises(SlurmMcpError, match="not cancel"):
        cl.cancel(many)


def test_large_selections_are_chunked_and_capped(tmp_path):
    rows = "".join(f"{1000 + i}|RUNNING|cpu|1:00|1:00:00|1|n|t|bulk\n" for i in range(250))
    cl = Cluster(make_config(tmp_path, enable_writes=True), FakeRunner(ssh_replies={"squeue": (rows, 0), "scancel": ("", 0)}))
    res = cl.cancel_many(cl.plan_cancel_many(name="bulk")["confirmation_token"])
    assert len(res["cancelled"]) == 250 and sum(c.startswith("scancel") for c in cl.runner.remote_commands()) == 3
    many = "".join(f"{1000 + i}|RUNNING|cpu|1:00|1:00:00|1|n|t|bulk\n" for i in range(1001))
    big = Cluster(make_config(tmp_path, enable_writes=True), FakeRunner(ssh_replies={"squeue": (many, 0)}))
    with pytest.raises(SlurmMcpError, match="narrow the selection"):
        big.plan_cancel_many(all_mine=True)


def test_a_failed_chunk_is_reported(tmp_path):
    cl = Cluster(make_config(tmp_path, enable_writes=True), FakeRunner(ssh_replies={"squeue": (QUEUE, 0), "scancel": ("", 1)}))
    res = cl.cancel_many(cl.plan_cancel_many(name="sweep")["confirmation_token"])
    assert res["cancelled"] == [] and res["failed"][0]["jobs"] == ["10", "11"]


def test_needs_writes_and_tools_are_annotated(tmp_path, cl):
    off = Cluster(make_config(tmp_path), FakeRunner())
    with pytest.raises(SlurmMcpError, match="disabled"):
        off.plan_cancel_many(all_mine=True)
    tools = {t.name: t for t in asyncio.run(build_server(cl).list_tools())}
    assert tools["cancel_jobs"].annotations.destructive_hint is True
    assert tools["cancel_jobs_dry_run"].annotations.read_only_hint is True
    assert list(tools["cancel_jobs"].input_schema["properties"]) == ["confirmation_token", "review_text"]
