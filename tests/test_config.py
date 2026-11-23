import pytest

from slurm_mcp.config import Config, load_config
from slurm_mcp.errors import ConfigError

GOOD = {"host": "h.example.org", "user": "bob", "remote_roots": ["/scratch/bob/"], "local_dir": "~/dl"}


def test_from_dict_normalizes():
    cfg = Config.from_dict(GOOD)
    assert cfg.remote_roots == ("/scratch/bob",)
    assert "~" not in str(cfg.local_dir)


@pytest.mark.parametrize("patch", [
    {"host": "-oProxyCommand=evil"}, {"user": "a b"}, {"remote_roots": []},
    {"remote_roots": ["relative"]}, {"surprise": 1},
])
def test_rejects_bad_values(patch):
    with pytest.raises(ConfigError):
        Config.from_dict({**GOOD, **patch})


def test_missing_key():
    with pytest.raises(ConfigError, match="missing"):
        Config.from_dict({"host": "h"})


def test_load_from_file_and_env(tmp_path, monkeypatch):
    f = tmp_path / "c.toml"
    f.write_text('host="h.example.org"\nuser="bob"\nremote_roots=["/s/bob"]\nlocal_dir="/tmp/x"\n')
    assert load_config(f).user == "bob"
    monkeypatch.setenv("SLURM_MCP_CONFIG", str(f))
    assert load_config().host == "h.example.org"
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_defaults_and_bool_validation():
    cfg = Config.from_dict(GOOD)
    assert cfg.auto_connect is True and cfg.enable_writes is False
    with pytest.raises(ConfigError, match="true or false"):
        Config.from_dict({**GOOD, "enable_writes": "yes"})


def test_example_config_stays_valid():
    import tomllib
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    cfg = Config.from_dict(tomllib.loads(example.read_text()))
    assert {"smoke", "debug", "standard", "gpu_debug", "gpu", "julia_sim"} <= set(cfg.presets)
    assert cfg.presets["julia_sim"].modules == ("Julia",) and dict(cfg.presets["julia_sim"].env) == {"use_gpu": "true"}
    assert set(cfg.gpu_types) == {"H100", "A100-40Gb", "A100-80Gb"}  # CommonUI's ENUM_GPUs
    gpu = cfg.presets["gpu"]
    assert gpu.gpu_types == ("H100", "A100-40Gb", "A100-80Gb") and gpu.time == "0-12:00:00" and gpu.mem == "3000M"
    assert all(cfg.presets[n].cpus == 1 and cfg.presets[n].max_cpus_per_gpu == 4 for n in ("gpu", "gpu_debug"))
    assert cfg.presets["smoke"].time == "00:15:00" and cfg.presets["debug"].time == "00:30:00"
