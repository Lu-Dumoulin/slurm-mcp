import asyncio
import hashlib

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import ConfigError, PathError, SlurmMcpError
from slurm_mcp.server import build_server

GPUS = {"H100": "nvidia_h100_nvl", "A100-40Gb": "nvidia_a100-pcie-40gb", "A100-80Gb": "nvidia_a100_80gb_pcie"}
PRESETS = {
    "debug": dict(partition="public-short-cpu", time="00:30:00", mem="4G", cpus=2, max_time="01:00:00",
                  max_cpus=4, max_mem="8G", description="30 minute test run"),
    "gpu": dict(partition="private-kruse-gpu,shared-gpu", time="0-12:00:00", mem="3000", gpu=True,
                gpu_types=["A100-40Gb", "H100"], max_time="2-00:00:00", max_mem="64G"),
}
WORKDIR = "/scratch/alice/code"
# private partition: A100-40Gb and H100 only; shared partition: all three (as on Baobab)
SINFO = ("private-kruse-gpu|EPYC,nvidia_a100-pcie-40gb\nprivate-kruse-gpu|EPYC,nvidia_h100_nvl\n"
         "shared-gpu*|EPYC,nvidia_a100_80gb_pcie,nvidia_h100_nvl\nshared-gpu*|nvidia_a100-pcie-40gb\n")


def raw(tmp_path, **over):
    d = dict(host="h.example.org", user="alice", remote_roots=["/scratch/alice"],
             local_dir=str(tmp_path / "dl"), enable_writes=True, gpu_types=GPUS, presets=PRESETS)
    d.update(over)
    return d


@pytest.fixture
def pcfg(tmp_path):
    return Config.from_dict(raw(tmp_path))


def pcluster(cfg, **replies):
    base = {"test -d": ("", 0), "mkdir": ("", 0), "sbatch": ("777\n", 0), "sinfo": (SINFO, 0), "squeue": ("", 0)}
    base.update(replies)
    return Cluster(cfg, FakeRunner(ssh_replies=base))


def dry(cl, **kw):
    args = dict(preset="gpu", gpu=True, command=["julia", "main.jl"], workdir=WORKDIR, job_name="sim")
    args.update(kw)
    return cl.plan_submit_preset(**args)


def test_config_parses_presets_and_defaults(pcfg):
    assert set(pcfg.presets) == {"debug", "gpu"}
    assert pcfg.presets["gpu"].gpu_types == ("A100-40Gb", "H100")  # CommonUI default selection
    assert pcfg.array_concurrency == 40 and pcfg.scripts_dir == "/scratch/alice/slurm-mcp-jobs"


def test_config_rejects_bad_presets_and_jobs_dir(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_dict(raw(tmp_path, presets={"x": dict(partition="p", time="bad", mem="1G")}))
    with pytest.raises(ConfigError, match="jobs_dir"):
        Config.from_dict(raw(tmp_path, jobs_dir="/tmp/elsewhere"))
    with pytest.raises(ConfigError):
        Config.from_dict(raw(tmp_path, gpu_types={"H100": "bad feature;"}))


def test_list_presets_tells_the_model_what_to_ask(pcfg):
    info = pcluster(pcfg).describe_presets()
    gpu = next(p for p in info["presets"] if p["name"] == "gpu")
    assert info["gpu_types"] == ["A100-40Gb", "A100-80Gb", "H100"]
    assert gpu["defaults"]["gpu_types"] == ["A100-40Gb", "H100"] and gpu["caps"]["time"] == "2-00:00:00"
    assert any("GPU" in q for q in info["ask_the_user"])


def test_dry_run_shows_everything_and_writes_nothing(pcfg):
    cl = pcluster(pcfg)
    out = dry(cl)
    assert out["script"].startswith("#!/bin/bash") and "#SBATCH --gpus=1" in out["script"]
    assert "GPU: yes" in out["summary"] and out["confirmation_token"]
    assert out["script_sha256"] == hashlib.sha256(out["script"].encode()).hexdigest()
    assert out["script_will_be_written_to"] == f"/scratch/alice/slurm-mcp-jobs/sim-{out['script_sha256'][:8]}.sh"
    assert cl.runner.uploads == {}
    assert not any(c.startswith(("sbatch", "mkdir")) for c in cl.runner.remote_commands())


def test_submit_uploads_the_reviewed_script_then_runs_sbatch(pcfg):
    cl = pcluster(pcfg)
    out = dry(cl, array="1-3")
    res = cl.submit(out["confirmation_token"])
    assert res["job_id"] == "777"
    assert cl.runner.uploads[out["script_will_be_written_to"]] == out["script"]  # byte for byte what was shown
    cmds = cl.runner.remote_commands()
    assert cmds[-1] == f"sbatch --parsable --chdir={WORKDIR} {out['script_will_be_written_to']}"
    assert any(c.startswith("mkdir -p -- /scratch/alice/slurm-mcp-jobs") for c in cmds)
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        cl.submit(out["confirmation_token"])


def test_each_dry_run_binds_its_own_script(pcfg):
    cl = pcluster(pcfg)
    a = dry(cl, job_name="a")
    b = dry(cl, job_name="b", command=["julia", "other.jl"])
    cl.submit(b["confirmation_token"])
    assert list(cl.runner.uploads.values()) == [b["script"]]
    assert a["script_will_be_written_to"] not in cl.runner.uploads


def test_gpu_must_be_stated_and_consistent(pcfg):
    cl = pcluster(pcfg)
    with pytest.raises(SlurmMcpError, match="ask the user"):
        dry(cl, gpu=None)
    with pytest.raises(SlurmMcpError, match="CPU-only"):
        dry(cl, preset="debug", gpu=True)
    assert cl.runner.calls == [] or all("sbatch" not in c[-1] for c in cl.runner.calls)


def test_dry_run_rejects_bad_inputs_before_a_token(pcfg):
    cl = pcluster(pcfg)
    with pytest.raises(SlurmMcpError, match="unknown preset"):
        dry(cl, preset="huge")
    with pytest.raises(PathError):
        dry(cl, workdir="/etc")
    with pytest.raises(SlurmMcpError, match="exceeds the cap"):
        dry(cl, time="9-00:00:00")
    with pytest.raises(SlurmMcpError, match="job_name"):
        dry(cl, job_name="x\n#SBATCH --wrap=id")
    missing = pcluster(pcfg, **{"test -d": ("", 1)})
    with pytest.raises(SlurmMcpError, match="does not exist"):
        dry(missing)
    assert cl.confirmations._pending == {}


def test_presets_need_writes_enabled(tmp_path):
    cfg = Config.from_dict(raw(tmp_path, enable_writes=False))
    with pytest.raises(SlurmMcpError, match="disabled"):
        pcluster(cfg).plan_submit_preset("debug", False, ["x"], WORKDIR, "j")


def test_upload_failure_is_reported_and_nothing_is_submitted(pcfg):
    from slurm_mcp.runner import Result

    class NoUpload(FakeRunner):
        def _scp(self, argv):
            return Result(tuple(argv), 1, "", "disk quota exceeded")

    cl = Cluster(pcfg, NoUpload(ssh_replies={"test -d": ("", 0), "mkdir": ("", 0), "sbatch": ("1\n", 0)}))
    out = dry(cl)
    with pytest.raises(SlurmMcpError, match="disk quota"):
        cl.submit(out["confirmation_token"])
    assert not any(c.startswith("sbatch") for c in cl.runner.remote_commands())


def test_server_registers_preset_tools_with_gpu_required(pcfg):
    tools = {t.name: t for t in asyncio.run(build_server(pcluster(pcfg)).list_tools())}
    assert {"list_presets", "submit_preset_dry_run"} <= set(tools)
    schema = tools["submit_preset_dry_run"].input_schema
    assert "gpu" in schema["required"] and "default" not in schema["properties"]["gpu"]
    assert "ask" in tools["submit_preset_dry_run"].description.lower()
    assert tools["submit_preset_dry_run"].annotations.read_only_hint is True


def test_missing_type_on_one_partition_is_only_a_warning(pcfg):
    out = dry(pcluster(pcfg), gpu_types=["H100", "A100-40Gb", "A100-80Gb"])
    assert out["warnings"] == ["A100-80Gb is not available on private-kruse-gpu"]
    assert not any("shared-gpu" in w for w in out["warnings"])
    assert out["confirmation_token"]


def test_type_missing_on_every_partition_is_refused(tmp_path):
    only_private = {**PRESETS["gpu"], "partition": "private-kruse-gpu"}
    cfg = Config.from_dict(raw(tmp_path, presets={"gpu": only_private}))
    cl = pcluster(cfg)
    with pytest.raises(SlurmMcpError, match="exist on partition"):
        dry(cl, gpu_types=["A100-80Gb"])
    assert cl.confirmations._pending == {}
    assert dry(cl, gpu_types=["H100"])["warnings"] == []  # a type the private partition does have


def test_failed_availability_check_warns_but_does_not_block(pcfg):
    out = dry(pcluster(pcfg, sinfo=("", 1)))
    assert any("could not check" in w for w in out["warnings"]) and out["confirmation_token"]


def test_list_presets_shows_cpus_per_gpu_for_gpu_presets(tmp_path):
    presets = {"gpu": {**PRESETS["gpu"], "max_cpus_per_gpu": 4, "max_gpus": 2}, "debug": PRESETS["debug"]}
    info = pcluster(Config.from_dict(raw(tmp_path, presets=presets))).describe_presets()
    caps = {p["name"]: p["caps"] for p in info["presets"]}
    assert caps["gpu"]["cpus_per_gpu"] == 4 and "cpus" not in caps["gpu"]
    assert caps["debug"]["cpus"] == 4 and "cpus_per_gpu" not in caps["debug"]
