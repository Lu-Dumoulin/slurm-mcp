"""Secrets are never read, listed or downloaded through the tools, even via a symlink."""
import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import ConfigError, PathError, SlurmMcpError
from slurm_mcp.paths import broad_remote_root_problem, sensitive_match

HOME = "/home/alice"


def cl(tmp_path, **replies):
    cfg = make_config(tmp_path, remote_roots=(HOME, "/scratch/alice"))
    return Cluster(cfg, FakeRunner(ssh_replies=replies))


@pytest.mark.parametrize("path", [
    f"{HOME}/.ssh/id_ed25519", f"{HOME}/.ssh/config", f"{HOME}/.bash_history", f"{HOME}/.zsh_history", f"{HOME}/.netrc",
    f"{HOME}/.gnupg/pubring.kbx", f"{HOME}/.aws/credentials", f"{HOME}/proj/.env", f"{HOME}/proj/.env.production",
    f"{HOME}/proj/server.pem", f"{HOME}/proj/api.key", f"{HOME}/proj/credentials.json", f"{HOME}/proj/secrets.yaml",
    f"{HOME}/proj/id_rsa.bak", f"{HOME}/.julia/logs/repl_history.jl", f"{HOME}/proj/login.keychain-db", f"{HOME}/.git-credentials",
    f"{HOME}/proj/ID_RSA", f"{HOME}/proj/Secrets.txt",
])
def test_secret_names_are_refused_by_every_read_tool(tmp_path, path):
    c = cl(tmp_path)
    for call in (lambda: c.read_file(path), lambda: c.grep_file(path, "x"), lambda: c.tail_file(path),
                 lambda: c.read_parameter_table(path), lambda: c.list_files(path)):
        with pytest.raises(SlurmMcpError, match="looks like a secret"):
            call()
    assert c.runner.calls == []                                   # refused before any ssh call


@pytest.mark.parametrize("path", [f"{HOME}/proj/main.jl", f"{HOME}/sim/DF.csv", "/scratch/alice/run/slurm-1.out",
                                  f"{HOME}/proj/keyboard.jl", f"{HOME}/proj/monkey.txt", f"{HOME}/proj/environment.yml"])
def test_ordinary_files_are_not_blocked(path):
    assert sensitive_match(path) is None


def test_a_symlink_that_leads_to_a_secret_is_refused(tmp_path):
    c = cl(tmp_path, realpath=(f"{HOME}/.ssh/id_ed25519\n", 0), sed=("PRIVATE KEY", 0))
    with pytest.raises(SlurmMcpError, match="resolves to .*looks like a secret"):
        c.read_file("/scratch/alice/innocent.txt")
    assert not any(x.startswith("sed") for x in c.runner.remote_commands())


def test_a_symlink_that_leaves_the_roots_is_refused(tmp_path):
    c = cl(tmp_path, realpath=("/etc/shadow\n", 0))
    with pytest.raises(PathError, match="outside the configured roots"):
        c.read_file("/scratch/alice/link")
    with pytest.raises(SlurmMcpError, match="could not resolve"):
        cl(tmp_path, realpath=("", 1)).read_file("/scratch/alice/link")


def test_the_resolved_path_is_what_gets_read(tmp_path):
    c = cl(tmp_path, realpath=("/scratch/alice/real/log.out\n", 0), sed=("hello\n", 0))
    assert c.read_file("/scratch/alice/latest.out")["path"] == "/scratch/alice/real/log.out"
    assert "sed -n 1,200p /scratch/alice/real/log.out" in c.runner.remote_commands()


def test_secrets_are_not_listed_and_the_count_is_reported(tmp_path):
    listing = "f\tmain.jl\t5\t1.0\nf\t.env\t9\t1.0\nd\t.ssh\t4096\t1.0\nf\tsub/id_rsa\t9\t1.0\nf\tsub/data.csv\t3\t1.0\n"
    out = cl(tmp_path, find=(listing, 0)).list_files(HOME)
    assert [e["path"] for e in out["files"]] == ["main.jl", "sub/data.csv"] and out["hidden_secret_names"] == 3


def test_extra_sensitive_patterns_from_the_config_apply(tmp_path):
    cfg = make_config(tmp_path, remote_roots=(HOME,), sensitive_extra=("*.licence", "private"))
    c = Cluster(cfg, FakeRunner())
    for p in (f"{HOME}/a.licence", f"{HOME}/private/x.txt"):
        with pytest.raises(SlurmMcpError, match="looks like a secret"):
            c.read_file(p)


def test_fetch_never_downloads_secrets(tmp_path):
    listing = "f\ta.csv\t3\t1700000000.0\nf\t.env\t9\t1700000000.0\nf\tid_rsa\t9\t1700000000.0\n"
    c = cl(tmp_path, find=(listing, 0))
    out = c.fetch_results("/scratch/alice/run", dry_run=True)
    assert [f["path"] for f in out["to_download"]] == ["a.csv"] and out["hidden_secret_names"] == 2
    with pytest.raises(SlurmMcpError, match="looks like a secret"):
        c.fetch_results("/scratch/alice/.ssh")


# --- configuration ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("root", ["/", "/home", "/srv", "/etc", "/usr/local", "/var/log", "/bin"])
def test_broad_remote_roots_are_refused_in_the_config(tmp_path, root):
    base = dict(host="h.example.org", user="bob", local_dir=str(tmp_path))
    with pytest.raises(ConfigError, match="too broad"):
        Config.from_dict({**base, "remote_roots": [root]})
    assert broad_remote_root_problem(root) is not None


def test_normal_remote_roots_are_accepted(tmp_path):
    base = dict(host="h.example.org", user="bob", local_dir=str(tmp_path))
    cfg = Config.from_dict({**base, "remote_roots": ["/home/users/b/bob", "/srv/beegfs/scratch/users/b/bob", "/scratch/bob"]})
    assert len(cfg.remote_roots) == 3


@pytest.mark.parametrize("local", ["/", "~", "/etc", "/usr/local"])
def test_broad_local_roots_are_refused_in_the_config(tmp_path, local):
    base = dict(host="h.example.org", user="bob", local_dir=str(tmp_path), remote_roots=["/s/bob/x"])
    with pytest.raises(ConfigError, match="too broad"):
        Config.from_dict({**base, "local_roots": [local]})


def test_sensitive_extra_is_validated(tmp_path):
    base = dict(host="h.example.org", user="bob", local_dir=str(tmp_path), remote_roots=["/s/bob/x"])
    assert Config.from_dict({**base, "sensitive_extra": ["*.licence"]}).sensitive_extra == ("*.licence",)
    with pytest.raises(ConfigError):
        Config.from_dict({**base, "sensitive_extra": [""]})


# --- fetch_results ------------------------------------------------------------------------------------------------

def test_fetch_defaults_to_a_dry_run_and_downloads_only_when_asked(tmp_path):
    listing = "f\ta.csv\t3\t1700000000.0\n"
    c = cl(tmp_path, find=(listing, 0))
    out = c.fetch_results("/scratch/alice/run")
    assert out["dry_run"] is True and out["downloaded"] == [] and not any(x[0] == "scp" for x in c.runner.calls)
    import inspect

    assert inspect.signature(Cluster.fetch_results).parameters["dry_run"].default is True


def test_a_newer_local_file_is_never_replaced(tmp_path):
    import os

    listing = "f\ta.csv\t3\t1700000000.0\nf\tb.csv\t4\t1700000000.0\n"
    cfg = make_config(tmp_path, remote_roots=("/scratch/alice",))
    dest = cfg.local_dir / "scratch/alice/run"
    dest.mkdir(parents=True)
    (dest / "a.csv").write_text("edited locally, different size")
    os.utime(dest / "a.csv", (1800000000, 1800000000))                    # newer than the cluster copy
    c = Cluster(cfg, FakeRunner(ssh_replies={"find": (listing, 0)}, remote_files={"/scratch/alice/run/b.csv": (b"bbbb", 1700000000.0)}))
    out = c.fetch_results("/scratch/alice/run", dry_run=False)
    assert out["kept_newer_local_files"] == ["a.csv"] and out["downloaded"] == ["b.csv"]
    assert (dest / "a.csv").read_text() == "edited locally, different size"


def test_fetch_refuses_a_download_folder_that_resolves_outside_local_dir(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    cfg = make_config(tmp_path, remote_roots=("/scratch/alice",))
    (cfg.local_dir / "scratch").mkdir(parents=True)
    (cfg.local_dir / "scratch" / "alice").symlink_to(outside)               # a link planted inside the download folder
    c = Cluster(cfg, FakeRunner(ssh_replies={"find": ("f\ta.csv\t3\t1700000000.0\n", 0)}))
    with pytest.raises(SlurmMcpError, match="resolves outside the download folder"):
        c.fetch_results("/scratch/alice/run", dry_run=False)
    assert list(outside.iterdir()) == []
