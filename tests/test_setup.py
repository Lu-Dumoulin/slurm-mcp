from slurm_mcp.config import load_config
from slurm_mcp.setup import run_init


def scripted(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_init_writes_valid_config(tmp_path):
    path = tmp_path / "cfg" / "config.toml"
    run_init(path, scripted(["h.example.org", "bob", "/s/bob/a, /s/bob/b", "", "/s/bob/DF.csv"]), lambda m: None)
    cfg = load_config(path)
    assert cfg.host == "h.example.org" and cfg.remote_roots == ("/s/bob/a", "/s/bob/b")
    assert cfg.df_csv == "/s/bob/DF.csv" and cfg.local_dir.name == "slurm-mcp-downloads"


def test_init_reprompts_on_invalid_input(tmp_path):
    path = tmp_path / "config.toml"
    msgs = []
    run_init(path, scripted(["h", "bad user", "/r", "", "", "h.example.org", "bob", "/s/bob", "/tmp/x", ""]), msgs.append)
    assert any("Invalid" in m for m in msgs)
    assert load_config(path).user == "bob"


def test_init_does_not_overwrite_without_yes(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("keep")
    run_init(path, scripted(["n"]), lambda m: None)
    assert path.read_text() == "keep"
