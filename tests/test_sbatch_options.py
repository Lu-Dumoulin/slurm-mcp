import asyncio

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import ConfigError, SlurmMcpError
from slurm_mcp.jobscript import build_script, resolve_spec, summarize
from slurm_mcp.presets import Preset
from slurm_mcp.sbatchopts import SbatchOptions, resolve_options
from slurm_mcp.server import build_server

GPUS = {"H100": "nvidia_h100_nvl"}


def opts(**kw):
    base = dict(allowed_qos=("long",), allow_exclusive=True, mail_user="me@example.org")
    base.update(kw)
    return resolve_options(**base)


def test_flags_and_header_for_each_option():
    o = opts(dependency="afterok:12:13", qos="long", exclusive=True, hold=True, mail_type=["END", "FAIL", "END"])
    assert o.flags() == ["--dependency=afterok:12:13", "--qos=long", "--exclusive", "--hold",
                         "--mail-type=END,FAIL", "--mail-user=me@example.org"]
    assert o.header()[0] == "#SBATCH --dependency=afterok:12:13"
    assert "dependency afterok:12:13" in o.describe() and "submitted on hold" in o.describe()
    assert SbatchOptions().flags() == [] and opts().flags() == []


@pytest.mark.parametrize("dep", ["afterok", "afterok:", "afterok:12;rm", "afterok:a", "12", "afterok:12 afterany:3", "singleton:1", ""])
def test_bad_dependencies_are_refused(dep):
    with pytest.raises(SlurmMcpError, match="dependency"):
        opts(dependency=dep)
    assert opts(dependency="singleton").dependency == "singleton"
    assert opts(dependency="afterany:1:2:3").dependency == "afterany:1:2:3"


def test_qos_exclusive_and_mail_are_gated():
    with pytest.raises(SlurmMcpError, match="not allowed here"):
        opts(qos="admin")
    with pytest.raises(SlurmMcpError, match="invalid qos"):
        opts(qos="long\n#SBATCH --wrap=id")
    with pytest.raises(SlurmMcpError, match="exclusive"):
        opts(exclusive=True, allow_exclusive=False)
    with pytest.raises(SlurmMcpError, match="invalid mail_type"):
        opts(mail_type=["END", "NONE; id"])
    with pytest.raises(SlurmMcpError, match="mail_user in the config"):
        opts(mail_type=["END"], mail_user=None)


def test_the_mail_address_can_never_come_from_the_caller(tmp_path):
    import inspect

    assert "mail_user" in inspect.signature(resolve_options).parameters      # only the config feeds it
    cluster = Cluster(make_config(tmp_path, enable_writes=True), FakeRunner())
    schema_props = {t.name: t for t in asyncio.run(build_server(cluster).list_tools())}
    assert "mail_user" not in schema_props["submit_preset_dry_run"].input_schema["properties"]
    assert "mail_user" not in schema_props["submit_job_dry_run"].input_schema["properties"]


def test_options_land_in_the_generated_script_header_and_summary():
    p = Preset.from_dict("debug", dict(partition="public-short-cpu", time="00:30:00", mem="4G", cpus=2, max_time="01:00:00"), GPUS)
    spec = resolve_spec(p, GPUS, gpu=False, command=["echo", "x"], workdir="/s/a", job_name="j",
                        options=opts(dependency="afterok:5", hold=True, mail_type=["FAIL"]))
    text = build_script(spec)
    header = [ln for ln in text.splitlines() if ln.startswith("#SBATCH")]
    assert header[-4:] == ["#SBATCH --dependency=afterok:5", "#SBATCH --hold", "#SBATCH --mail-type=FAIL",
                           "#SBATCH --mail-user=me@example.org"]
    assert "dependency afterok:5" in summarize(spec) and "submitted on hold" in summarize(spec)
    assert "#SBATCH --dependency" not in build_script(resolve_spec(p, GPUS, gpu=False, command=["x"], workdir="/s/a", job_name="j"))


# --- through the cluster -----------------------------------------------------------------------------------------

def cl(tmp_path, preset_extra=None, **cfg_over):
    preset = dict(partition="public-short-cpu", time="00:30:00", mem="4G", cpus=2, max_time="01:00:00")
    preset.update(preset_extra or {})
    cfg = Config.from_dict(dict(host="h.example.org", user="alice", remote_roots=["/scratch/alice"], local_dir=str(tmp_path),
                                enable_writes=True, gpu_types=GPUS, presets={"debug": preset}, **cfg_over))
    return Cluster(cfg, FakeRunner(ssh_replies={"test -d": ("", 0), "test -f": ("", 0), "head -c": ("#!/bin/bash\n", 0),
                                                "sbatch": ("77\n", 0), "mkdir": ("", 0), "sinfo": ("", 0), "squeue": ("", 0),
                                                "sha256sum": ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  /x\n", 0)}))


def dry(c, **kw):
    args = dict(preset="debug", gpu=False, command=["echo", "hi"], workdir="/scratch/alice", job_name="j")
    args.update(kw)
    return c.plan_submit_preset(**args)


def test_preset_dry_run_applies_the_presets_permissions(tmp_path):
    c = cl(tmp_path, {"allowed_qos": ["long"], "allow_exclusive": True}, mail_user="me@example.org")
    out = dry(c, dependency="afterok:5", qos="long", exclusive=True, hold=True, mail_type=["END"])
    for line in ("#SBATCH --dependency=afterok:5", "#SBATCH --qos=long", "#SBATCH --exclusive", "#SBATCH --hold",
                 "#SBATCH --mail-type=END", "#SBATCH --mail-user=me@example.org"):
        assert line in out["script"]
    assert "qos long" in out["summary"]
    plain = cl(tmp_path)
    for kw in ({"qos": "long"}, {"exclusive": True}, {"mail_type": ["END"]}, {"dependency": "afterok:5;x"}):
        with pytest.raises(SlurmMcpError):
            dry(plain, **kw)
    assert not any(x.startswith("sbatch") for x in plain.runner.remote_commands())


def test_existing_script_dry_run_adds_validated_flags_to_the_command(tmp_path):
    c = cl(tmp_path, allowed_qos=["long"], mail_user="me@example.org")
    out = c.plan_submit("/scratch/alice/run/job.sh", dependency="afterany:9", qos="long", hold=True, mail_type=["FAIL"])
    assert out["command"] == ("sbatch --parsable --chdir=/scratch/alice/run --dependency=afterany:9 --qos=long --hold "
                              "--mail-type=FAIL --mail-user=me@example.org /scratch/alice/run/job.sh")
    c.submit(out["confirmation_token"])
    assert out["command"] in c.runner.remote_commands()
    with pytest.raises(SlurmMcpError, match="not allowed here"):
        cl(tmp_path).plan_submit("/scratch/alice/run/job.sh", qos="long")


def test_config_validation_of_the_new_keys(tmp_path):
    base = dict(host="h.example.org", user="bob", remote_roots=["/s/bob"], local_dir=str(tmp_path))
    good = Config.from_dict({**base, "allowed_qos": ["long"], "mail_user": "me@example.org", "allow_exclusive": True})
    assert good.allowed_qos == ("long",) and good.mail_user == "me@example.org" and good.allow_exclusive is True
    for bad in ({"mail_user": "not an address"}, {"allowed_qos": ["a b"]}, {"allowed_qos": "long"}, {"allow_exclusive": "yes"}):
        with pytest.raises(ConfigError):
            Config.from_dict({**base, **bad})
    with pytest.raises(ConfigError, match="allowed_qos"):
        Preset.from_dict("x", dict(partition="p", time="00:10:00", mem="1G", allowed_qos=["a;b"]), GPUS)


# --- hold and release --------------------------------------------------------------------------------------------------

Q = ("30|PENDING|cpu|0:00|1:00:00|1|Priority|t|wait\n"
     "31|PENDING|cpu|0:00|1:00:00|1|JobHeldUser|t|held\n"
     "32|PENDING|cpu|0:00|1:00:00|1|JobHeldAdmin|t|adm\n"
     "33|RUNNING|cpu|1:00|1:00:00|1|n1|t|run\n")


@pytest.fixture
def hc(tmp_path):
    return Cluster(make_config(tmp_path, enable_writes=True), FakeRunner(ssh_replies={"squeue": (Q, 0), "scontrol": ("", 0)}))


def test_hold_a_pending_job_in_two_steps(hc):
    plan = hc.plan_hold("30", True)
    assert plan["command"] == "scontrol hold 30" and plan["jobs"][0]["job_id"] == "30"
    assert not any(c.startswith("scontrol") for c in hc.runner.remote_commands())      # the dry run changes nothing
    assert hc.apply_hold(plan["confirmation_token"], True)["held"] is True
    assert "scontrol hold 30" in hc.runner.remote_commands()
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        hc.apply_hold(plan["confirmation_token"], True)


def test_only_pending_jobs_can_be_held(hc):
    with pytest.raises(SlurmMcpError, match="only pending"):
        hc.plan_hold("33", True)
    with pytest.raises(SlurmMcpError, match="not one of your"):
        hc.plan_hold("999", True)


def test_only_user_holds_can_be_released(hc):
    assert hc.plan_hold("31", False)["command"] == "scontrol release 31"
    for jid in ("30", "32", "33"):
        with pytest.raises(SlurmMcpError, match="held by you"):
            hc.plan_hold(jid, False)


def test_hold_and_release_tokens_are_not_interchangeable_and_ids_are_validated(hc):
    hold = hc.plan_hold("30", True)["confirmation_token"]
    with pytest.raises(SlurmMcpError, match="not release"):
        hc.apply_hold(hold, False)
    with pytest.raises(SlurmMcpError):
        hc.plan_hold("30; scancel -u me", True)


def test_hold_tools_are_registered_and_need_writes(tmp_path, hc):
    tools = {t.name: t for t in asyncio.run(build_server(hc).list_tools())}
    assert {"hold_job", "hold_job_dry_run", "release_job", "release_job_dry_run"} <= set(tools)
    assert tools["hold_job"].annotations.read_only_hint is False and tools["hold_job"].annotations.destructive_hint is True and list(tools["release_job"].input_schema["properties"]) == ["confirmation_token", "review_text"]
    with pytest.raises(SlurmMcpError, match="disabled"):
        Cluster(make_config(tmp_path), FakeRunner()).plan_hold("30", True)
