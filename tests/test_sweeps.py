import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import ConfigError, PathError, SlurmMcpError
from slurm_mcp.jobscript import build_script, resolve_spec
from slurm_mcp.presets import Preset, check_env

GPUS = {"H100": "nvidia_h100_nvl", "A100-40Gb": "nvidia_a100-pcie-40gb", "A100-80Gb": "nvidia_a100_80gb_pcie"}


def julia_preset(**over):
    d = dict(partition="private-kruse-gpu,shared-gpu", time="0-12:00:00", mem="3000", gpu=True, gpus=1, max_gpus=2,
             gpu_types=["A100-40Gb", "H100"], max_time="2-00:00:00", max_mem="64G", modules=["Julia"],
             env={"use_gpu": "true"})
    d.update(over)
    return Preset.from_dict("julia_sim", d, GPUS)


SCRATCH = "/srv/beegfs/scratch/users/d/user"
DATA = f"{SCRATCH}/Data/ActiveDewetting"
CODE = "/home/users/d/user/Code/ActiveDewetting/main.jl"


def sweep_spec(**kw):
    base = dict(gpu=True, command=["julia", "--optimize=3", CODE], workdir=DATA, job_name="dewetting",
                array="1-8", env={"path_to_data": DATA + "/{task_id}/"}, task_dir=DATA + "/{task_id}")
    base.update(kw)
    return resolve_spec(julia_preset(), GPUS, **base)


def test_golden_script_matches_what_commonui_writes_for_activedewetting():
    assert build_script(sweep_spec()) == (
        "#!/bin/bash\n"
        "#SBATCH --job-name=dewetting\n"
        "#SBATCH --partition=private-kruse-gpu,shared-gpu\n"
        "#SBATCH --time=0-12:00:00\n"
        "#SBATCH --cpus-per-task=1\n"
        "#SBATCH --mem=3000M\n"
        "#SBATCH --output=%x-%A_%a.out\n"
        "#SBATCH --array=1-8%40\n"
        "#SBATCH --gpus=1\n"
        "#SBATCH --constraint=nvidia_a100-pcie-40gb|nvidia_h100_nvl\n"
        "\n"
        "module load Julia\n"
        "\n"
        "export use_gpu=true\n"
        f'export path_to_data={DATA}/"${{SLURM_ARRAY_TASK_ID}}"/\n'
        "\n"
        f'task_dir={DATA}/"${{SLURM_ARRAY_TASK_ID}}"\n'
        'mkdir -p "$task_dir"\n'
        'cd "$task_dir" || exit 1\n'
        f"srun julia --optimize=3 {CODE}\n"
    )


def test_summary_mentions_env_and_task_folders():
    from slurm_mcp.jobscript import summarize

    s = summarize(sweep_spec())
    assert "env: use_gpu, path_to_data" in s and f"one folder per task under {DATA}/" in s


def test_request_env_overrides_the_preset_default_and_modules_can_be_replaced():
    spec = sweep_spec(env={"use_gpu": "false"}, modules=["Julia/1.10"])
    assert dict(spec.env)["use_gpu"] == "false" and spec.modules == ("Julia/1.10",)
    assert sweep_spec(env=None, task_dir=None).task_dir is None


@pytest.mark.parametrize("env", [
    {"PATH": "/tmp"}, {"LD_PRELOAD": "x"}, {"SLURM_JOB_ID": "1"}, {"bad name": "x"}, {"1ABC": "x"},
    {"A": "line1\nline2"}, {"A": "x" * 2001}, {"A": 5}, {f"V{i}": "x" for i in range(21)}, ["not", "a", "dict"],
])
def test_env_is_validated(env):
    with pytest.raises(SlurmMcpError):
        check_env(env)


def test_env_values_are_quoted_so_they_cannot_run_commands():
    text = build_script(sweep_spec(env={"NOTE": "$(rm -rf ~); `id` 'q'"}, task_dir=None))
    assert "export NOTE='$(rm -rf ~); `id` '\"'\"'q'\"'\"''\n" in text
    assert "\nrm" not in text


def test_task_id_in_env_or_task_dir_needs_an_array():
    for kw in ({"env": {"X": "{task_id}"}}, {"task_dir": DATA + "/{task_id}"}):
        with pytest.raises(SlurmMcpError, match="needs an array"):
            sweep_spec(array=None, **{"env": None, "task_dir": None, **kw})


def test_preset_defaults_are_validated_in_the_config():
    with pytest.raises(ConfigError, match="env"):
        julia_preset(env={"PATH": "x"})
    with pytest.raises(ConfigError, match="modules"):
        julia_preset(modules=["Julia; id"])
    assert julia_preset().modules == ("Julia",)


# --- through the cluster: array size from the table ---------------------------------------------------------------

def swcluster(tmp_path, awk="9\n", **replies):
    cfg = Config.from_dict(dict(host="h.example.org", user="user", remote_roots=["/home/users/d/user", SCRATCH],
                                local_dir=str(tmp_path), enable_writes=True, gpu_types=GPUS,
                                presets={"julia_sim": dict(partition="private-kruse-gpu,shared-gpu", time="0-12:00:00",
                                         mem="3000", gpu=True, gpu_types=["H100"], modules=["Julia"], env={"use_gpu": "true"},
                                         max_time="1-00:00:00")}))
    base = {"test -d": ("", 0), "test -f": ("", 0), "awk": (awk, 0), "sinfo": ("private-kruse-gpu|nvidia_h100_nvl\nshared-gpu|nvidia_h100_nvl\n", 0)}
    base.update(replies)
    return Cluster(cfg, FakeRunner(ssh_replies=base))


def dry(cl, **kw):
    args = dict(preset="julia_sim", gpu=True, command=["julia", "--optimize=3", CODE], workdir=DATA, job_name="dewetting",
                array_from_table=f"{DATA}/DF.csv", env={"path_to_data": DATA + "/{task_id}/"}, task_dir=DATA + "/{task_id}")
    args.update(kw)
    return cl.plan_submit_preset(**args)


def test_array_size_comes_from_the_table_rows(tmp_path):
    out = dry(swcluster(tmp_path, awk="9\n"))                      # header + 8 data rows
    assert out["array"] == "1-8" and out["array_from_table"] == {"path": f"{DATA}/DF.csv", "rows": 8}
    assert "#SBATCH --array=1-8%40" in out["script"] and "module load Julia" in out["script"]
    assert "export use_gpu=true" in out["script"] and 'mkdir -p "$task_dir"' in out["script"]
    assert "8 task(s)" in out["summary"]


def test_table_must_have_data_rows_and_be_a_file_inside_the_roots(tmp_path):
    with pytest.raises(SlurmMcpError, match="no data rows"):
        dry(swcluster(tmp_path, awk="1\n"))
    with pytest.raises(SlurmMcpError, match="no data rows"):
        dry(swcluster(tmp_path, awk="\n"))
    with pytest.raises(SlurmMcpError, match="not a regular file"):
        dry(swcluster(tmp_path, **{"test -f": ("", 1)}))
    with pytest.raises(PathError):
        dry(swcluster(tmp_path), array_from_table="/etc/passwd")


def test_array_limit_applies_to_table_rows_and_both_array_options_conflict(tmp_path):
    with pytest.raises(SlurmMcpError, match="indices must be below 10000|limit is 10000"):
        dry(swcluster(tmp_path, awk="10001\n"))                         # 10000 data rows: index 10000 is too high
    assert dry(swcluster(tmp_path, awk="10000\n"))["array"] == "1-9999"  # 9999 rows: the last valid index
    with pytest.raises(SlurmMcpError, match="not both"):
        dry(swcluster(tmp_path), array="1-3")


def test_task_dir_must_be_inside_the_roots(tmp_path):
    with pytest.raises(PathError):
        dry(swcluster(tmp_path), task_dir="/tmp/{task_id}")
    with pytest.raises(PathError):
        dry(swcluster(tmp_path), task_dir=DATA + "/../../x/{task_id}")


def test_the_dry_run_changes_nothing_and_the_table_is_counted_with_a_fixed_command(tmp_path):
    cl = swcluster(tmp_path)
    dry(cl)
    assert f"awk 'END{{print NR}}' {DATA}/DF.csv" in cl.runner.remote_commands()
    assert not any(c.startswith(("mkdir", "sbatch")) for c in cl.runner.remote_commands())
