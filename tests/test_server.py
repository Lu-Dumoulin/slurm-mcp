import asyncio

from fakes import FakeRunner
from slurm_mcp.cluster import Cluster
from slurm_mcp.server import build_server

EXPECTED = {"queue", "recent_jobs", "job_status", "job_log", "diagnose_job", "tail_file",
            "list_files", "read_parameter_table", "fetch_results"}


def test_tool_surface_and_annotations(cfg):
    server = build_server(Cluster(cfg, FakeRunner()))
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED
    # Nothing in phase 1 may write to the cluster, and only fetch_results writes locally.
    for name, t in by_name.items():
        assert t.annotations.destructive_hint is False
        assert t.annotations.read_only_hint is (name != "fetch_results")


def test_call_queue_through_server(cfg):
    runner = FakeRunner(ssh_replies={"squeue": ("7|PENDING|cpu|0:00|1:00|1|Priority|t|x\n", 0)})
    server = build_server(Cluster(cfg, runner))
    result = asyncio.run(server.call_tool("queue", {}))
    assert "PENDING" in str(result)


def test_errors_reach_the_model_with_their_message(cfg):
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    server = build_server(Cluster(cfg, FakeRunner()))
    with pytest.raises(ToolError, match="outside the configured roots"):
        asyncio.run(server.call_tool("tail_file", {"path": "/etc/passwd"}))
