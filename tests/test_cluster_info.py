import asyncio

from fakes import FakeRunner
from slurm_mcp.cluster import Cluster
from slurm_mcp.parsing import parse_partitions, parse_slurm_limits
from slurm_mcp.server import build_server

SINFO = (
    "debug-cpu*|15:00|1|16|62000|(null)\n"
    "shared-gpu|12:00:00|2|12+|251000|gpu:nvidia_titan_x:5(S:0-1),VramPerGpu:no_consume:12G\n"
    "shared-gpu|12:00:00|3|64|251000|gpu:nvidia_a100-pcie-40gb:4(S:0-3),VramPerGpu:no_consume:40G\n"
    "shared-gpu|12:00:00|1|64|251000|gpu:nvidia_a100-pcie-40gb:4(S:0-3),VramPerGpu:no_consume:40G\n"
    "broken line\n"
)
CONFIG = "MaxArraySize            = 10000\nMaxJobCount             = 60000\nSomethingElse           = secret\nMinJobAge = 300\n"


def test_partitions_are_merged_and_gres_cleaned():
    parts = {p["partition"]: p for p in parse_partitions(SINFO)}
    assert parts["debug-cpu"]["default"] is True and parts["debug-cpu"]["gres"] == []
    gpu = parts["shared-gpu"]
    assert gpu["nodes"] == 6 and gpu["time_limit"] == "12:00:00" and gpu["default"] is False
    assert gpu["gres"] == ["gpu:nvidia_titan_x:5", "VramPerGpu:no_consume:12G",
                           "gpu:nvidia_a100-pcie-40gb:4", "VramPerGpu:no_consume:40G"]       # socket notes dropped, no duplicates
    assert gpu["cpus_per_node"] == ["12+", "64"]


def test_only_whitelisted_limits_are_returned():
    assert parse_slurm_limits(CONFIG, ("MaxArraySize", "MaxJobCount", "MinJobAge")) == {
        "MaxArraySize": "10000", "MaxJobCount": "60000", "MinJobAge": "300"}


def test_cluster_info_uses_fixed_read_only_commands(cfg):
    cl = Cluster(cfg, FakeRunner(ssh_replies={"sinfo": (SINFO, 0), "scontrol show config": (CONFIG, 0)}))
    out = cl.cluster_info()
    assert out["limits"]["MaxArraySize"] == "10000" and {p["partition"] for p in out["partitions"]} == {"debug-cpu", "shared-gpu"}
    assert cl.runner.remote_commands() == ["sinfo -h -S P -o '%P|%l|%D|%c|%m|%G'", "scontrol show config"]


def test_cluster_info_survives_a_failing_config_query(cfg):
    out = Cluster(cfg, FakeRunner(ssh_replies={"sinfo": (SINFO, 0), "scontrol": ("", 1)})).cluster_info()
    assert out["limits"] == {} and out["partitions"]


def test_tools_are_registered_read_only_and_the_instructions_say_to_use_them(cfg):
    server = build_server(Cluster(cfg, FakeRunner()))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["cluster_info"].annotations.read_only_hint and tools["check_setup"].annotations.read_only_hint
    assert "do not run ssh, scp, rsync or sbatch yourself" in server.instructions


def test_check_setup_returns_the_doctor_lines(cfg):
    replies = {"true": ("", 0), "squeue": ("", 0), "test -d": ("", 0)}
    server = build_server(Cluster(cfg, FakeRunner(ssh_replies=replies)))
    result = asyncio.run(server.call_tool("check_setup", {}))
    text = str(result)
    assert "ssh connection" in text and "OK" in text
