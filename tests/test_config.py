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
