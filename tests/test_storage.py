import asyncio

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import ConfigError, PathError, RemoteError
from slurm_mcp.parsing import parse_beegfs_quota, parse_df, parse_du
from slurm_mcp.runner import Result
from slurm_mcp.server import build_server

DF = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
      "beegfs_nodev   1000000000 400000000 600000000      40% /srv/beegfs\n")


def test_parsers():
    row = parse_df(DF)
    assert row == {"filesystem": "beegfs_nodev", "total_bytes": 1_000_000_000 * 1024, "used_bytes": 400_000_000 * 1024,
                   "available_bytes": 600_000_000 * 1024, "use_percent": "40%", "mounted_on": "/srv/beegfs"}
    assert parse_df("") is None and parse_df("junk\nmore junk\n") is None
    assert parse_du("2048\t/scratch/alice/run\n") == 2048 * 1024 and parse_du("du: error") is None


def test_storage_info_reports_each_root(cfg):
    cl = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0)}))
    out = cl.storage_info()
    assert out["filesystems"][0]["root"] == "/scratch/alice" and out["filesystems"][0]["use_percent"] == "40%"
    assert cl.runner.remote_commands() == ["df -P -k /scratch/alice"] and "du" not in out and "quota" not in out


def test_a_failing_df_is_reported_without_hiding_the_rest(cfg):
    out = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": ("", 1)})).storage_info()
    assert out["filesystems"][0]["error"] == "df failed"


def test_du_size_of_a_path_inside_the_roots(cfg):
    cl = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0), "du -s": ("4096\t/scratch/alice/run\n", 0)}))
    out = cl.storage_info("/scratch/alice/run")
    assert out["du"] == {"path": "/scratch/alice/run", "bytes": 4096 * 1024}
    assert "du -s -k -x -P /scratch/alice/run" in cl.runner.remote_commands()


def test_du_outside_the_roots_is_refused_before_any_ssh(cfg):
    cl = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0)}))
    with pytest.raises(PathError):
        cl.storage_info("/etc")
    assert not any(c.startswith("du") for c in cl.runner.remote_commands())


def test_du_uses_its_own_time_limit_and_reports_a_timeout(cfg):
    seen = []

    class Slow(FakeRunner):
        def run(self, argv, timeout):
            if argv[0] == "ssh" and argv[-1].startswith("du "):
                seen.append(timeout)
                raise RemoteError(f"command timed out after {timeout:g}s")
            return super().run(argv, timeout)

    out = Cluster(cfg, Slow(ssh_replies={"df -P -k": (DF, 0)})).storage_info("/scratch/alice/run")
    assert seen == [cfg.du_timeout_s] and "too big to measure" in out["du"]["error"]


def test_configured_quota_commands_are_run_as_fixed_commands(tmp_path):
    cfg = make_config(tmp_path, quota_commands=(("beegfs-ctl", "--getquota", "--uid", "1000"),))
    cl = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0), "beegfs-ctl": ("quota table\n", 0)}))
    out = cl.storage_info()
    assert out["quota"] == [{"command": "beegfs-ctl --getquota --uid 1000", "exit_code": 0, "output": "quota table\n"}]
    assert "beegfs-ctl --getquota --uid 1000" in cl.runner.remote_commands()


def test_quota_commands_config_validation(tmp_path):
    base = dict(host="h.example.org", user="bob", remote_roots=["/s/bob"], local_dir=str(tmp_path))
    assert Config.from_dict({**base, "quota_commands": [["quota", "-s"]]}).quota_commands == (("quota", "-s"),)
    for bad in ("quota", [["quota", 1]], [[]], [["quota", ""]]):
        with pytest.raises(ConfigError):
            Config.from_dict({**base, "quota_commands": bad})


def test_tool_is_read_only(cfg):
    tools = {t.name: t for t in asyncio.run(build_server(Cluster(cfg, FakeRunner())).list_tools())}
    assert tools["storage_info"].annotations.read_only_hint


QUOTA = """home dir: /home/sagon
scratch dir: /srv/beegfs/scratch/users/s/sagon

        user/group                 ||           size          ||    chunk files
storage     |   name        |  id  ||    used    |    hard    ||  used   |  hard
----------------------------|------||------------|------------||---------|---------
home        |          sagon|240477||  530.46 GiB| 1024.00 GiB||  1158225|unlimited
scratch     |          sagon|240477||    2.74 TiB|   unlimited||   436030| 10000000
"""


def test_baobab_quota_table_is_parsed_into_used_and_hard_limits():
    home, scratch = parse_beegfs_quota(QUOTA)
    assert home["storage"] == "home" and home["name"] == "sagon" and home["id"] == 240477
    assert home["size_used_bytes"] == int(530.46 * 1024**3) and home["size_hard_bytes"] == 1024 * 1024**3
    assert home["files_used_count"] == 1158225 and home["files_unlimited"] is True and home["size_unlimited"] is False
    assert scratch["size_hard_bytes"] is None and scratch["size_unlimited"] is True
    assert scratch["size_used_bytes"] == int(2.74 * 1024**4) and scratch["files_hard_count"] == 10_000_000


def test_quota_output_that_is_not_that_table_stays_raw():
    assert parse_beegfs_quota("") is None and parse_beegfs_quota("no quota set\n") is None


def test_storage_info_adds_the_parsed_quota_next_to_the_raw_output(tmp_path):
    cfg = make_config(tmp_path, quota_commands=(("beegfs-get-quota-home-scratch.sh",),))
    cl = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0), "beegfs-get-quota": (QUOTA, 0)}))
    q = cl.storage_info()["quota"][0]
    assert q["output"] == QUOTA and [r["storage"] for r in q["parsed"]] == ["home", "scratch"]
    assert "beegfs-get-quota-home-scratch.sh" in cl.runner.remote_commands()
    odd = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0), "beegfs-get-quota": ("weird output\n", 0)}))
    assert "parsed" not in odd.storage_info()["quota"][0]


def test_example_config_asks_only_for_your_own_quota():
    import tomllib
    from pathlib import Path

    cfg = Config.from_dict(tomllib.loads((Path(__file__).resolve().parent.parent / "config.example.toml").read_text()))
    assert cfg.quota_commands == (("beegfs-get-quota-home-scratch.sh", "-u", "{user}"),)
    assert not any(a in ("-g", "--group") for c in cfg.quota_commands for a in c)


def test_user_placeholder_is_replaced_by_your_own_username(tmp_path):
    cfg = make_config(tmp_path, quota_commands=(("beegfs-get-quota-home-scratch.sh", "-u", "{user}"),))
    cl = Cluster(cfg, FakeRunner(ssh_replies={"df -P -k": (DF, 0), "beegfs-get-quota": (QUOTA, 0)}))
    q = cl.storage_info()["quota"][0]
    assert q["command"] == "beegfs-get-quota-home-scratch.sh -u alice" and [r["storage"] for r in q["parsed"]] == ["home", "scratch"]
    assert "beegfs-get-quota-home-scratch.sh -u alice" in cl.runner.remote_commands()


@pytest.mark.parametrize("command", [
    ["quota.sh", "-u", "bob"], ["quota.sh", "-u"], ["quota.sh", "--user", "root"], ["quota.sh", "--uid", "0"],
    ["quota.sh", "-g", "{user}"], ["quota.sh", "--group", "hpc_users"], ["quota.sh", "-u", "{user}", "-g", "x"],
    ["quota.sh", "-u", "{USER}"], ["quota.sh", "-u", "{uid}"], ["quota.sh", "{home}"], ["quota.sh", "--name={user}{x}"],
])
def test_quota_commands_cannot_ask_for_other_users_or_groups(tmp_path, command):
    base = dict(host="h.example.org", user="bob", remote_roots=["/s/bob/x"], local_dir=str(tmp_path))
    with pytest.raises(ConfigError):
        Config.from_dict({**base, "quota_commands": [command]})


def test_user_placeholder_forms_that_are_accepted(tmp_path):
    base = dict(host="h.example.org", user="bob", remote_roots=["/s/bob/x"], local_dir=str(tmp_path))
    cfg = Config.from_dict({**base, "quota_commands": [["quota.sh", "-u", "{user}"], ["quota.sh", "--uid", "{user}"], ["quota.sh"]]})
    assert len(cfg.quota_commands) == 3
