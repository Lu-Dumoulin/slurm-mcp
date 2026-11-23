"""The approval prompt must show what will happen: every write call repeats the review text of its plan."""
import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.errors import SlurmMcpError
from slurm_mcp.server import build_server

SHA = "a" * 64
QUEUE = "10|RUNNING|cpu|1:00|1:00:00|1|n1|t|sweep\n11|PENDING|cpu|0:00|1:00:00|1|JobHeldUser|t|held\n"
SCRIPT = "#!/bin/bash\n#SBATCH -n 1\nsrun ./run\n"
REPLIES = {"squeue": (QUEUE, 0), "scancel": ("", 0), "scontrol": ("", 0), "sha256sum": (f"{SHA}  /scratch/alice/run/job.sh\n", 0),
           "head -c": (SCRIPT, 0), "test -f": ("", 0), "sbatch": ("55\n", 0), "realpath": ("/scratch/alice/old/run1\n", 0),
           "test -e": ("", 0), "find": ("f\t10\t/scratch/alice/old/run1/a.dat\0d\t4096\t/scratch/alice/old/run1\0", 0), "rm -r": ("", 0)}


@pytest.fixture
def cl(tmp_path):
    return Cluster(make_config(tmp_path, enable_writes=True), FakeRunner(ssh_replies=dict(REPLIES)))


def test_submit_review_shows_the_command_and_the_full_script(cl):
    plan = cl.plan_submit("/scratch/alice/run/job.sh")
    text = plan["review_text"]
    assert "sbatch --parsable --chdir=/scratch/alice/run /scratch/alice/run/job.sh" in text
    assert "srun ./run" in text and SHA[:16] in text and "script text:" in text
    assert "review_text" in plan["next_step"]


def test_a_missing_or_changed_review_text_is_refused_and_keeps_the_token(cl):
    plan = cl.plan_submit("/scratch/alice/run/job.sh")
    for wrong in ("", "ok", plan["review_text"] + " ", plan["review_text"].replace("srun ./run", "srun ./other")):
        with pytest.raises(SlurmMcpError, match="review_text does not match"):
            cl.submit(plan["confirmation_token"], wrong)
    assert not any(c.startswith("sbatch") for c in cl.runner.remote_commands())
    assert cl.submit(plan["confirmation_token"], plan["review_text"])["job_id"] == "55"        # the token was not burnt


def test_every_write_call_needs_the_matching_review_text(cl):
    cancel = cl.plan_cancel("10")
    many = cl.plan_cancel_many(all_mine=True)
    hold = cl.plan_hold("11", False)
    delete = cl.plan_delete("/scratch/alice/old/run1")
    calls = [(cl.cancel, cancel), (cl.cancel_many, many), (lambda t, r: cl.apply_hold(t, False, r), hold), (cl.delete, delete)]
    for fn, plan in calls:
        with pytest.raises(SlurmMcpError, match="review_text does not match"):
            fn(plan["confirmation_token"], "something else")
    assert not any(c.startswith(("scancel", "scontrol hold", "scontrol release", "rm")) for c in cl.runner.remote_commands())
    for fn, plan in calls:
        fn(plan["confirmation_token"], plan["review_text"])
    assert "scancel 10" in cl.runner.remote_commands() and "rm -r -- /scratch/alice/old/run1" in cl.runner.remote_commands()


def test_cancel_and_hold_reviews_list_the_command_and_every_job(cl):
    many = cl.plan_cancel_many(all_mine=True)["review_text"]
    assert "command: scancel 10 11" in many and "  10  RUNNING  sweep" in many and "  11  PENDING  held" in many and "jobs (2)" in many
    assert "command: scontrol release 11" in cl.plan_hold("11", False)["review_text"]


def test_delete_review_shows_command_counts_and_entries(cl):
    text = cl.plan_delete("/scratch/alice/old/run1")["review_text"]
    assert "command: rm -r -- /scratch/alice/old/run1" in text and "1 file(s), 1 folder(s)" in text
    assert "  f  /scratch/alice/old/run1/a.dat  10 B" in text and "cannot be undone" in text


def test_upload_review_lists_every_file_with_its_action(tmp_path):
    root = tmp_path / "work"
    code = root / "proj"
    for name in ("main.jl", "sub/k.jl"):
        (code / name).parent.mkdir(parents=True, exist_ok=True)
        (code / name).write_text("x")
    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),))
    c = Cluster(cfg, FakeRunner(ssh_replies={"realpath": ("/scratch/alice/p\n", 0), "test -d": ("", 1), "mkdir": ("", 0)}))
    plan = c.plan_upload_code("/scratch/alice/p", str(code))
    text = plan["review_text"]
    assert "UPLOAD 2 file(s)" in text and f"from: {code.resolve()}" in text and "to: alice@login.example.org:/scratch/alice/p" in text
    assert "command: mkdir -p -- /scratch/alice/p /scratch/alice/p/sub" in text
    assert "  create    main.jl  1 B" in text and "  create    sub/k.jl  1 B" in text and "nothing is deleted" in text
    with pytest.raises(SlurmMcpError, match="review_text does not match"):
        c.upload_code(plan["confirmation_token"], review_text="UPLOAD 1 file(s)")
    assert c.upload_code(plan["confirmation_token"], review_text=text)["uploaded"] == 2


def test_long_file_lists_are_capped_with_a_count_but_the_hash_covers_everything():
    from slurm_mcp import review as rv

    files = [(f"f{i}.dat", i, "create", False) for i in range(450)]
    text = rv.upload("h:/d", "/src", None, files, 1000, "ab" * 32)
    assert text.count("\n  create") == 300 and "... and 150 more (all are covered by the plan hash)" in text


def test_an_existing_script_that_changes_before_submit_is_not_submitted(cl):
    plan = cl.plan_submit("/scratch/alice/run/job.sh")
    cl.runner.ssh_replies["sha256sum"] = (f"{'b' * 64}  /scratch/alice/run/job.sh\n", 0)       # edited after the review
    with pytest.raises(SlurmMcpError, match="changed since the dry run"):
        cl.submit(plan["confirmation_token"], plan["review_text"])
    assert not any(c.startswith("sbatch") for c in cl.runner.remote_commands())


def test_scripts_too_large_to_review_in_full_are_refused(cl):
    cl.runner.ssh_replies["head -c"] = ("x" * 20_001, 0)
    with pytest.raises(SlurmMcpError, match="too large to review"):
        cl.plan_submit("/scratch/alice/run/job.sh")


def test_the_generated_script_is_in_the_review_text(tmp_path):
    cfg = make_config(tmp_path, enable_writes=True, presets={})
    from slurm_mcp.config import Config

    cfg = Config.from_dict(dict(host="h.example.org", user="alice", remote_roots=["/scratch/alice"], local_dir=str(tmp_path),
                                enable_writes=True, presets={"debug": dict(partition="p", time="00:10:00", mem="1G")}))
    c = Cluster(cfg, FakeRunner(ssh_replies={"test -d": ("", 0), "squeue": ("", 0), "sbatch": ("9\n", 0), "mkdir": ("", 0)}))
    plan = c.plan_submit_preset("debug", False, ["echo", "hi"], "/scratch/alice", "j")
    assert "srun echo hi" in plan["review_text"] and "the script is written to that path first" in plan["review_text"]
    assert "sbatch --parsable --chdir=/scratch/alice" in plan["review_text"]
    assert c.submit(plan["confirmation_token"], plan["review_text"])["job_id"] == "9"


def test_the_queue_ceiling_counts_array_tasks_and_blocks_the_dry_run(tmp_path):
    cfg = make_config(tmp_path, enable_writes=True, max_queued_jobs=5)
    c = Cluster(cfg, FakeRunner(ssh_replies=dict(REPLIES, squeue=("20_[1-3%40]|PENDING|cpu|0:00|1:00|1|Resources|t|arr\n", 0))))
    with pytest.raises(SlurmMcpError, match="would make 6 queued jobs .*max_queued_jobs = 5"):
        c._queue_warnings(3)
    assert c._queue_warnings(2) == []                            # 3 queued tasks + 2 new = 5 is allowed
    big = c.plan_submit("/scratch/alice/run/job.sh")             # one more job: 3 + 1 = 4, allowed
    assert big["confirmation_token"]
    c.runner.ssh_replies["squeue"] = ("".join(f"{i}|RUNNING|cpu|1:00|1:00|1|n|t|j\n" for i in range(5)), 0)
    with pytest.raises(SlurmMcpError, match="max_queued_jobs = 5"):
        c.plan_submit("/scratch/alice/run/job.sh")


def test_server_tools_require_review_text_and_say_where_it_comes_from(tmp_path):
    c = Cluster(make_config(tmp_path, enable_writes=True), FakeRunner(ssh_replies=dict(REPLIES)))
    server = build_server(c)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in ("submit_job", "cancel_job", "cancel_jobs", "hold_job", "release_job", "delete"):
        assert "review_text" in tools[name].input_schema["required"], name
        assert tools[name].annotations.destructive_hint is True, name
        assert "review_text" in tools[name].description
    plan = c.plan_submit("/scratch/alice/run/job.sh")
    with pytest.raises(ToolError, match="review_text does not match"):
        asyncio.run(server.call_tool("submit_job", {"confirmation_token": plan["confirmation_token"], "review_text": "approve"}))
    ok = asyncio.run(server.call_tool("submit_job", {"confirmation_token": plan["confirmation_token"], "review_text": plan["review_text"]}))
    assert "55" in str(ok)


# --- large submissions: allowed up to max_queued_jobs, but flagged ---------------------------------------------------

def sweep_cluster(tmp_path, queued=0, warn=1000, limit=10000):
    from slurm_mcp.config import Config

    cfg = Config.from_dict(dict(host="h.example.org", user="alice", remote_roots=["/scratch/alice"], local_dir=str(tmp_path),
                                enable_writes=True, warn_queued_jobs=warn, max_queued_jobs=limit,
                                presets={"debug": dict(partition="p", time="00:10:00", mem="1G")}))
    rows = "".join(f"{i}|RUNNING|cpu|1:00|1:00|1|n|t|j\n" for i in range(queued))
    return Cluster(cfg, FakeRunner(ssh_replies={"test -d": ("", 0), "squeue": (rows, 0), "sbatch": ("9\n", 0), "mkdir": ("", 0)}))


def sweep(c, tasks):
    return c.plan_submit_preset("debug", False, ["echo", "{task_id}"], "/scratch/alice", "sweep", array=f"1-{tasks}")


def test_a_large_submission_is_allowed_but_warned_about_in_the_dry_run_and_the_approval_text(tmp_path):
    plan = sweep(sweep_cluster(tmp_path, queued=20), 1500)                 # 1520 queued jobs, above the warning, below the limit
    assert len(plan["warnings"]) == 1 and plan["warnings"][0].startswith("LARGE SUBMISSION: this adds 1500 job(s) or tasks; you would have 1520 jobs queued")
    assert "(you have 20 now; warning threshold 1000, hard limit 10000)" in plan["warnings"][0]
    assert "WARNING: LARGE SUBMISSION" in plan["review_text"] and plan["confirmation_token"]


def test_a_submission_up_to_the_warning_level_has_no_warning(tmp_path):
    plan = sweep(sweep_cluster(tmp_path, queued=0), 1000)
    assert not any("LARGE" in w for w in plan["warnings"]) and "WARNING" not in plan["review_text"]


def test_the_limit_still_applies_and_the_warning_level_is_configurable(tmp_path):
    with pytest.raises(SlurmMcpError, match="would make 10499 queued jobs .*max_queued_jobs = 10000"):
        sweep(sweep_cluster(tmp_path, queued=500), 9999)
    assert any("LARGE" in w for w in sweep(sweep_cluster(tmp_path, warn=50), 100)["warnings"])
    assert sweep(sweep_cluster(tmp_path, queued=2000, warn=5000), 1000)["warnings"] == []


def test_existing_script_submissions_are_flagged_when_the_queue_is_already_large(tmp_path):
    c = sweep_cluster(tmp_path, queued=1200)
    c.runner.ssh_replies.update({"test -f": ("", 0), "head -c": (SCRIPT, 0), "sha256sum": (f"{SHA}  /x\n", 0)})
    plan = c.plan_submit("/scratch/alice/run/job.sh")
    assert plan["warnings"] and "WARNING: LARGE SUBMISSION" in plan["review_text"]
