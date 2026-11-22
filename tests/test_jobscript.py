import pytest

from slurm_mcp.errors import ConfigError, SlurmMcpError
from slurm_mcp.jobscript import build_script, resolve_spec, summarize
from slurm_mcp.presets import Preset, parse_mem_mb, parse_time

GPUS = {"H100": "nvidia_h100_nvl", "A100-40Gb": "nvidia_a100-pcie-40gb", "A100-80Gb": "nvidia_a100_80gb_pcie"}


def preset(**over):
    d = dict(partition="private-kruse-gpu,shared-gpu", time="0-12:00:00", mem="3000", gpu=True,
             gpus=1, max_time="2-00:00:00", max_mem="64G", gpu_types=["A100-40Gb", "H100"], max_gpus=2)
    d.update(over)
    return Preset.from_dict("gpu", d, GPUS)


def cpu_preset(**over):
    d = dict(partition="public-short-cpu", time="00:30:00", mem="4G", cpus=2, max_time="01:00:00",
             max_cpus=4, max_mem="8G")
    d.update(over)
    return Preset.from_dict("debug", d, GPUS)


def spec(p=None, **kw):
    base = dict(gpu=True, command=["julia", "--optimize=3", "main.jl"], workdir="/home/u/code", job_name="sim")
    base.update(kw)
    return resolve_spec(p or preset(), GPUS, **base)


def test_golden_gpu_script_with_commonui_defaults():
    text = build_script(spec(array="1-8", modules=["Julia"]))
    assert text == (
        "#!/bin/bash\n"
        "#SBATCH --job-name=sim\n"
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
        "cd /home/u/code || exit 1\n"
        "srun julia --optimize=3 main.jl\n"
    )


def test_golden_cpu_script_has_no_gpu_lines():
    text = build_script(spec(cpu_preset(), gpu=False))
    assert text == (
        "#!/bin/bash\n#SBATCH --job-name=sim\n#SBATCH --partition=public-short-cpu\n"
        "#SBATCH --time=00:30:00\n#SBATCH --cpus-per-task=2\n#SBATCH --mem=4G\n"
        "#SBATCH --output=%x-%j.out\n\ncd /home/u/code || exit 1\nsrun julia --optimize=3 main.jl\n"
    )


def test_gpu_choice_is_explicit_and_must_match_preset():
    with pytest.raises(SlurmMcpError, match="ask the user"):
        spec(gpu=None)
    with pytest.raises(SlurmMcpError, match="CPU-only"):
        spec(cpu_preset(), gpu=True)
    with pytest.raises(SlurmMcpError, match="GPU preset"):
        spec(gpu=False)
    with pytest.raises(SlurmMcpError, match="only apply"):
        spec(cpu_preset(), gpu=False, gpu_types=["H100"])


def test_gpu_type_must_be_in_the_configured_list():
    assert spec(gpu_types=["A100-80Gb"]).constraint == "nvidia_a100_80gb_pcie"
    with pytest.raises(SlurmMcpError, match="unknown GPU"):
        spec(gpu_types=["V100; rm -rf ~"])


def test_caps_are_enforced():
    with pytest.raises(SlurmMcpError, match="exceeds the cap"):
        spec(time="3-00:00:00")
    with pytest.raises(SlurmMcpError, match="exceeds the cap"):
        spec(mem="65G")
    with pytest.raises(SlurmMcpError, match="gpus must be"):
        spec(gpus=3)
    with pytest.raises(SlurmMcpError, match="cpus must be"):
        spec(cpu_preset(), gpu=False, cpus=5)
    assert spec(time="1-00:00:00").time == "1-00:00:00"


def test_array_limits_and_task_id_placeholder():
    with pytest.raises(SlurmMcpError, match="limit is 100"):
        spec(array="1-101")
    with pytest.raises(SlurmMcpError, match="invalid array"):
        spec(array="1-5; scancel -u me")
    with pytest.raises(SlurmMcpError, match="needs an array"):
        spec(command=["run", "{task_id}"])
    text = build_script(spec(array="1-3", command=["julia", "main.jl", "--id={task_id}", "{task_id}"]))
    assert text.endswith('srun julia main.jl --id="${SLURM_ARRAY_TASK_ID}" "${SLURM_ARRAY_TASK_ID}"\n')


@pytest.mark.parametrize("name", ["", "a b", "x\n#SBATCH --wrap=id", "a;b", "n" * 41])
def test_job_name_cannot_inject(name):
    with pytest.raises(SlurmMcpError, match="job_name"):
        spec(job_name=name)


def test_command_is_quoted_and_single_line():
    text = build_script(spec(command=["echo", "a b; rm -rf ~", "$(id)", "it's"]))
    last = text.splitlines()[-1]
    assert last == "srun echo 'a b; rm -rf ~' '$(id)' 'it'\"'\"'s'"
    assert all(not ln.startswith("#SBATCH") for ln in text.splitlines()[9:])  # nothing injected after the header
    for bad in ([], [""], ["ok\n#SBATCH --wrap=x"], ["a"] * 51):
        with pytest.raises(SlurmMcpError):
            spec(command=bad)


def test_modules_validated():
    with pytest.raises(SlurmMcpError, match="modules"):
        spec(modules=["Julia; id"])


def test_summary_mentions_gpu_and_cost():
    s = summarize(spec(array="1-4", gpu_types=["H100"]))
    assert "4 task(s)" in s and "GPU: yes, 1 x H100" in s and "12:00:00" in s
    assert "GPU: no" in summarize(spec(cpu_preset(), gpu=False))


def test_unit_parsers():
    assert parse_time("0-12:00:00") == 43200 and parse_time("1-00:00:00") == 86400 and parse_time("00:30:00") == 1800
    assert parse_mem_mb("3000") == 3000 and parse_mem_mb("4G") == 4096
    for bad in ("12", "1:2:3", "00:61:00", "abc"):
        with pytest.raises(SlurmMcpError):
            parse_time(bad)
    for bad in ("0", "4T", "-1", "4 G"):
        with pytest.raises(SlurmMcpError):
            parse_mem_mb(bad)


def test_preset_config_validation():
    with pytest.raises(ConfigError, match="exceeds its own max"):
        cpu_preset(time="02:00:00")
    with pytest.raises(ConfigError, match="not in the top-level"):
        preset(gpu_types=["V100"])
    with pytest.raises(ConfigError, match="partition"):
        cpu_preset(partition="a;b")
    with pytest.raises(ConfigError, match="only make sense"):
        cpu_preset(gpus=1)
