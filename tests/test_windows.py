"""Windows: no shared SSH connection, drive letters, junctions, folder names. Simulated on any system."""
import subprocess
import sys
from pathlib import PureWindowsPath

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp import commands as c
from slurm_mcp import config as config_module
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.doctor import run_doctor
from slurm_mcp.errors import ConfigError
from slurm_mcp.runner import SubprocessRunner
from slurm_mcp.uploads import broad_folder_problem, is_link_or_junction

CONTROL = ("ControlMaster", "ControlPath", "ControlPersist")


def cfg_with(tmp_path, mode="auto", **over):
    return make_config(tmp_path, ssh_multiplexing=mode, **over)


def joined(argv):
    return " ".join(argv)


# --- ssh_multiplexing ---------------------------------------------------------------------------------------

def test_default_is_auto_and_follows_the_platform(tmp_path, monkeypatch):
    cfg = cfg_with(tmp_path)
    assert cfg.ssh_multiplexing == "auto"
    monkeypatch.setattr(config_module, "platform_supports_multiplexing", lambda: False)    # Windows
    assert cfg.use_shared_connection is False
    monkeypatch.setattr(config_module, "platform_supports_multiplexing", lambda: True)     # macOS and Linux
    assert cfg.use_shared_connection is True


def test_the_platform_check_is_false_only_on_windows(monkeypatch):
    monkeypatch.setattr(config_module.os, "name", "nt")
    assert config_module.platform_supports_multiplexing() is False
    monkeypatch.setattr(config_module.os, "name", "posix")
    assert config_module.platform_supports_multiplexing() is True


def test_on_and_off_override_the_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "platform_supports_multiplexing", lambda: False)
    assert cfg_with(tmp_path, "on").use_shared_connection is True
    monkeypatch.setattr(config_module, "platform_supports_multiplexing", lambda: True)
    assert cfg_with(tmp_path, "off").use_shared_connection is False


def test_config_validates_the_value(tmp_path):
    base = dict(host="h.example.org", user="bob", remote_roots=["/s/bob/x"], local_dir=str(tmp_path))
    for ok in ("auto", "on", "off"):
        assert Config.from_dict({**base, "ssh_multiplexing": ok}).ssh_multiplexing == ok
    for bad in ("yes", "ON", True, ""):
        with pytest.raises(ConfigError, match="ssh_multiplexing"):
            Config.from_dict({**base, "ssh_multiplexing": bad})


def test_no_control_options_reach_ssh_or_scp_when_off(tmp_path):
    cfg = cfg_with(tmp_path, "off", extra_ssh_options=("-o", "ServerAliveInterval=30"))
    for argv in (c.ssh_argv(cfg, ["squeue"]), c.scp_download_argv(cfg, "/r/f", "f"), c.scp_upload_argv(cfg, "f", "/r/f"),
                 c.login_argv(cfg), c.check_argv(cfg)):
        assert not any(name in joined(argv) for name in CONTROL), argv
        assert "ConnectTimeout=15" in joined(argv)
    assert "ServerAliveInterval=30" in joined(c.ssh_argv(cfg, ["squeue"]))       # other options stay
    assert "BatchMode=yes" in joined(c.ssh_argv(cfg, ["squeue"]))


def test_control_options_are_there_when_on(tmp_path):
    cfg = cfg_with(tmp_path, "on")
    for argv in (c.ssh_argv(cfg, ["squeue"]), c.scp_download_argv(cfg, "/r/f", "f")):
        assert all(name in joined(argv) for name in CONTROL)


def test_login_and_check_do_not_use_the_shared_connection_when_off(tmp_path):
    cfg = cfg_with(tmp_path, "off")
    login, check = c.login_argv(cfg), c.check_argv(cfg)
    assert "-fN" not in login and "-O" not in check
    assert login[-2:] == ["alice@login.example.org", "true"] and check[-2:] == ["alice@login.example.org", "true"]
    assert "BatchMode" not in joined(login)                    # login may ask for a password or passphrase
    assert "BatchMode=yes" in joined(check)
    on = cfg_with(tmp_path, "on")
    assert "-fN" in c.login_argv(on) and c.check_argv(on)[-3:-1] == ["-O", "check"]


def test_auto_connect_off_needs_no_master_when_there_is_none(tmp_path):
    cfg = cfg_with(tmp_path, "off", auto_connect=False)
    cl = Cluster(cfg, FakeRunner(ssh_replies={"squeue": ("", 0)}))
    cl.queue()                                                  # does not fail with "no open SSH connection"
    assert not any("-O" in call for call in cl.runner.calls)


def test_auth_failure_message_explains_the_key_must_work_without_a_prompt(tmp_path):
    from slurm_mcp.errors import RemoteError
    from slurm_mcp.runner import Result

    class Denied(FakeRunner):
        def run(self, argv, timeout):
            return Result(tuple(argv), 255, "", "Permission denied (publickey).")

    with pytest.raises(RemoteError, match="own connection here"):
        Cluster(cfg_with(tmp_path, "off"), Denied()).queue()


def test_doctor_says_the_shared_connection_is_off(tmp_path):
    lines = []
    run_doctor(cfg_with(tmp_path, "off"), FakeRunner(ssh_replies={"true": ("", 0), "squeue": ("", 0), "test -d": ("", 0)}),
               lines.append, lambda p: "n")
    text = "\n".join(lines)
    assert "[  OK] ssh connection: works without a prompt. The shared connection is off (ssh_multiplexing = off)" in text


def test_login_and_check_print_a_clear_message_and_create_no_socket_folder(tmp_path, monkeypatch, capsys):
    from slurm_mcp import __main__ as entry

    conf = tmp_path / "config.toml"
    control = tmp_path / "sockets"
    conf.write_text(f'host="h.example.org"\nuser="bob"\nremote_roots=["/s/bob/x"]\nlocal_dir="{tmp_path.as_posix()}"\n'
                    f'ssh_multiplexing="off"\ncontrol_dir="{control.as_posix()}"\n')
    seen = []
    monkeypatch.setattr(entry.subprocess, "call", lambda argv: seen.append(argv) or 0)
    for command in ("login", "check"):
        assert entry.main([command, "--config", str(conf)]) == 0
    err = capsys.readouterr().err
    assert err.count("Shared SSH connections are off on this system") == 2 and "ssh-add" in err
    assert not control.exists() and all("-fN" not in a and "-O" not in a for a in seen)


def test_the_socket_folder_is_still_made_when_the_shared_connection_is_on(tmp_path, monkeypatch):
    from slurm_mcp import __main__ as entry

    conf = tmp_path / "config.toml"
    control = tmp_path / "sockets"
    conf.write_text(f'host="h.example.org"\nuser="bob"\nremote_roots=["/s/bob/x"]\nlocal_dir="{tmp_path.as_posix()}"\n'
                    f'ssh_multiplexing="on"\ncontrol_dir="{control.as_posix()}"\n')
    monkeypatch.setattr(entry.subprocess, "call", lambda argv: 0)
    entry.main(["check", "--config", str(conf)])
    assert control.is_dir()


# --- other Windows details --------------------------------------------------------------------------------------

def test_ssh_output_is_read_as_utf_8(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "Δ ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = SubprocessRunner().run(["ssh", "x"], 5)
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace" and out.stdout == "Δ ok\n"


@pytest.mark.parametrize("folder,problem", [
    ("C:/", "root"), ("D:/", "root"), ("C:/Users", "home folder or above"), ("C:/Users/ludo", "home folder or above"),
    ("C:/Windows", "system folder"), ("C:/Windows/System32", "system folder"), ("C:/Program Files", "system folder"),
    ("C:/ProgramData", "system folder"), ("C:/Users/ludo/Documents", "general folder"),
    ("C:/Users/ludo/Downloads", "general folder"), ("C:/Users/ludo/OneDrive", "general folder"),
    ("C:/Users/ludo/DESKTOP", "general folder"),
])
def test_broad_windows_folders_are_refused(folder, problem):
    found = broad_folder_problem(PureWindowsPath(folder), PureWindowsPath("C:/Users/ludo"))
    assert found and problem in found


@pytest.mark.parametrize("folder", ["D:/Data/ActiveDewetting", "C:/Users/ludo/code/proj", "D:/Data", "C:/Users/ludo/Documents/GitHub"])
def test_normal_windows_project_folders_are_accepted(folder):
    assert broad_folder_problem(PureWindowsPath(folder), PureWindowsPath("C:/Users/ludo")) is None


def test_a_junction_is_treated_like_a_symlink(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_link_or_junction(plain) is False
    junction = tmp_path / "junction"
    junction.mkdir()
    real_lstat = __import__("os").lstat

    class Attrs:
        st_file_attributes = 0x410                           # directory + reparse point

    monkeypatch.setattr("slurm_mcp.uploads.os.lstat", lambda p: Attrs() if str(p) == str(junction) else real_lstat(p))
    monkeypatch.setattr(type(junction), "is_junction", lambda self: False, raising=False)   # Python 3.11 has no such method
    assert is_link_or_junction(junction) is True


def test_the_scan_skips_a_junction_and_does_not_enter_it(tmp_path, monkeypatch):
    from slurm_mcp.uploads import DEFAULT_EXCLUDE, scan_local

    root = tmp_path / "proj"
    (root / "linked").mkdir(parents=True)
    (root / "linked" / "secret_data.txt").write_text("outside the project")
    (root / "main.jl").write_text("x")
    monkeypatch.setattr("slurm_mcp.uploads.is_link_or_junction", lambda p: p.name == "linked")
    scan = scan_local(root, DEFAULT_EXCLUDE, 100)
    assert [f.rel for f in scan.files] == ["main.jl"] and scan.symlinks == 1


def test_remote_paths_stay_posix_even_for_windows_local_paths(tmp_path):
    from slurm_mcp.paths import validate_remote_path

    assert validate_remote_path("/home/u/code/x.jl", ("/home/u",)) == "/home/u/code/x.jl"
    for bad in ("D:\\Data\\x", "D:/Data/x", "\\home\\u\\x"):
        with pytest.raises(Exception):
            validate_remote_path(bad, ("/home/u",))


def test_install_bat_checks_python_and_uses_the_venv_python_for_pip():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "install.bat").read_text()
    assert "sys.version_info < (3, 11)" in text and r".venv\Scripts\python -m pip install" in text
    assert r"slurm-mcp.exe" in text and "slurm-mcp.exe\" check" in text
