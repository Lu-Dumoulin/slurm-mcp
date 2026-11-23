from slurm_mcp.config import load_config
from slurm_mcp.setup import run_init


def no_links(*_):
    return []


def scripted(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_init_writes_valid_config(tmp_path):
    path = tmp_path / "cfg" / "config.toml"
    run_init(path, scripted(["h.example.org", "bob", "n", "/s/bob/a, /s/bob/b", "", "", "/s/bob/DF.csv"]), lambda m: None)
    cfg = load_config(path)
    assert cfg.host == "h.example.org" and cfg.remote_roots == ("/s/bob/a", "/s/bob/b")
    assert cfg.df_csv == "/s/bob/DF.csv" and cfg.local_dir.name == "slurm-mcp-downloads"


def test_init_reprompts_on_invalid_input(tmp_path):
    path = tmp_path / "config.toml"
    msgs = []
    run_init(path, scripted(["h", "bad user", "n", "/r", "", "", "", "h.example.org", "bob", "n", "/s/bob", "/tmp/x", "", ""]), msgs.append)
    assert any("Invalid" in m for m in msgs)
    assert load_config(path).user == "bob"


def test_init_does_not_overwrite_without_yes(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("keep")
    run_init(path, scripted(["n"]), lambda m: None)
    assert path.read_text() == "keep"


def test_init_uses_probed_home_as_default_root(tmp_path):
    path = tmp_path / "config.toml"
    msgs = []
    seen = []

    def probe(host, user):
        seen.append((host, user))
        return "/home/bob"

    # Enter at the connection question accepts the default (yes), Enter at roots accepts the home.
    run_init(path, scripted(["h.example.org", "bob", "", "", "", "", ""]), msgs.append, probe, no_links)
    assert seen == [("h.example.org", "bob")]
    assert load_config(path).remote_roots == ("/home/bob",)
    assert any("home directory is /home/bob" in m for m in msgs)


def test_init_failed_probe_can_continue_with_manual_roots(tmp_path):
    path = tmp_path / "config.toml"
    msgs = []
    # connection test fails, user declines to retry, enters roots by hand
    run_init(path, scripted(["h.example.org", "bob", "y", "n", "/s/bob", "", "", ""]), msgs.append, lambda h, u: None, no_links)
    assert any("Could not connect" in m for m in msgs)
    assert load_config(path).remote_roots == ("/s/bob",)


def test_init_failed_probe_lets_user_correct_host_and_user(tmp_path):
    path = tmp_path / "config.toml"
    tried = []

    def probe(host, user):
        tried.append((host, user))
        return "/home/alice" if (host, user) == ("good.example.org", "alice") else None

    # wrong host and user first; retry keeps the previous values as defaults (Enter), then fixes both
    answers = ["bad.example.org", "bob", "y", "y", "good.example.org", "alice", "y", "", "", "", ""]
    run_init(path, scripted(answers), lambda m: None, probe, no_links)
    assert tried == [("bad.example.org", "bob"), ("good.example.org", "alice")]
    cfg = load_config(path)
    assert (cfg.host, cfg.user, cfg.remote_roots) == ("good.example.org", "alice", ("/home/alice",))


def test_init_offers_symlinked_folders_as_extra_roots(tmp_path):
    path = tmp_path / "config.toml"
    links = lambda h, u, home: [("scratch", "/srv/scratch/bob"), ("notes", "/srv/notes/bob")]
    # test connection (default y), accept scratch, decline notes, accept suggested roots, defaults for the rest
    run_init(path, scripted(["h.example.org", "bob", "", "y", "n", "", "", "", ""]), lambda m: None,
             lambda h, u: "/home/bob", links)
    assert load_config(path).remote_roots == ("/home/bob", "/srv/scratch/bob")


def test_init_can_set_local_upload_folders(tmp_path):
    path = tmp_path / "config.toml"
    run_init(path, scripted(["h.example.org", "bob", "n", "/s/bob", "", f"{tmp_path}, ~/zz-upload-test", ""]), lambda m: None,
             lambda h, u: None, no_links)
    cfg = load_config(path)
    assert cfg.local_roots[0] == tmp_path.resolve() and cfg.local_roots[1].name == "zz-upload-test"
