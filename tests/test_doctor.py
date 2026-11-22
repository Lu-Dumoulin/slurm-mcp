from fakes import FakeRunner, make_config
from slurm_mcp.config import Config
from slurm_mcp.doctor import run_doctor
from slurm_mcp.runner import Result

GPUS = {"H100": "nvidia_h100_nvl"}
PRESETS = {
    "smoke": dict(partition="debug-cpu", time="00:15:00", mem="2G", cpus=1),
    "gpu": dict(partition="shared-gpu", time="00:30:00", mem="3000", gpu=True, gpu_types=["H100"]),
}


def cfg_for(tmp_path, **over):
    d = dict(host="h.example.org", user="alice", remote_roots=["/scratch/alice"], local_dir=str(tmp_path / "dl"),
             enable_writes=True, gpu_types=GPUS, presets=PRESETS)
    d.update(over)
    return Config.from_dict(d)


GOOD = {"true": ("", 0), "squeue": ("", 0), "test -d": ("", 0), "sinfo": ("debug-cpu*|x\nshared-gpu|nvidia_h100_nvl\n", 0)}


class Lines(list):
    def __call__(self, text):
        self.append(text)


def test_read_only_checks_pass(tmp_path):
    out = Lines()
    code = run_doctor(cfg_for(tmp_path), FakeRunner(ssh_replies=dict(GOOD)), out, lambda p: "n")
    text = "\n".join(out)
    assert code == 0 and "[  OK] ssh connection" in text and "All checks passed" in text
    assert "--smoke" in text  # tells the user how to run the real test


def test_connection_failure_is_reported_with_the_fix(tmp_path):
    out = Lines()
    runner = FakeRunner(ssh_replies={"true": ("", 255)})
    assert run_doctor(cfg_for(tmp_path), runner, out, lambda p: "n") == 1
    text = "\n".join(out)
    assert "[FAIL] ssh connection" in text and "login" in text and "Remote checks skipped" in text


def test_missing_root_and_unknown_partition(tmp_path):
    out = Lines()
    replies = {**GOOD, "test -d": ("", 1), "sinfo": ("other|x\n", 0)}
    code = run_doctor(cfg_for(tmp_path), FakeRunner(ssh_replies=replies), out, lambda p: "n")
    text = "\n".join(out)
    assert code == 1 and "[FAIL] root /scratch/alice" in text
    assert "[WARN] preset smoke: partition(s) not found" in text


def test_gpu_preset_with_unavailable_type_fails(tmp_path):
    out = Lines()
    replies = {**GOOD, "sinfo": ("debug-cpu|x\nshared-gpu|nvidia_other\n", 0)}
    assert run_doctor(cfg_for(tmp_path), FakeRunner(ssh_replies=replies), out, lambda p: "n") == 1
    assert any("[FAIL] preset gpu" in line for line in out)


class Jobs(FakeRunner):
    """sacct answers PENDING first, then the final state."""

    def __init__(self, replies, final="COMPLETED"):
        super().__init__(ssh_replies=replies)
        self.final, self.polls = final, 0

    def run(self, argv, timeout):
        if argv[0] == "ssh" and argv[-1].startswith("sacct"):
            self.polls += 1
            state = "PENDING" if self.polls == 1 else self.final
            row = f"99|{state}|0:0|00:00:03|2026-01-01T10:00:00|Unknown|debug-cpu|n1|1|2G||slurm-mcp-smoke\n"
            return Result(tuple(argv), 0, row, "")
        return super().run(argv, timeout)


SMOKE_REPLIES = {**GOOD, "mkdir": ("", 0), "sbatch": ("99\n", 0),
                 "scontrol": ("JobId=99 JobState=COMPLETED StdOut=/scratch/alice/slurm-mcp-jobs/o.out "
                              "StdErr=/scratch/alice/slurm-mcp-jobs/e.err\n", 0),
                 "o.out": ("node042\nSun Nov 22\nsmoke-ok\n", 0), "e.err": ("", 0)}


def test_smoke_submits_only_after_a_typed_yes(tmp_path):
    out, asked = Lines(), []
    runner = Jobs(dict(SMOKE_REPLIES))

    def answer(prompt):
        asked.append(prompt)
        return "y"

    code = run_doctor(cfg_for(tmp_path), runner, out, answer, smoke=True, sleep=lambda s: None)
    text = "\n".join(out)
    assert code == 0 and "[  OK] smoke test passed" in text and "smoke-ok" in text
    assert "#SBATCH --partition=debug-cpu" in text and "state: PENDING" in text and "state: COMPLETED" in text
    assert any("Submit this job now" in p for p in asked)
    assert any(c.startswith("sbatch") for c in runner.remote_commands())


def test_smoke_does_nothing_when_the_user_says_no(tmp_path):
    out = Lines()
    runner = Jobs(dict(SMOKE_REPLIES))
    code = run_doctor(cfg_for(tmp_path), runner, out, lambda p: "n", smoke=True, sleep=lambda s: None)
    assert code == 0 and "Not submitted" in "\n".join(out)
    assert not any(c.startswith("sbatch") for c in runner.remote_commands())
    assert runner.uploads == {}


def test_smoke_reports_failure_and_timeout(tmp_path):
    out = Lines()
    failed = Jobs(dict(SMOKE_REPLIES), final="FAILED")
    assert run_doctor(cfg_for(tmp_path), failed, out, lambda p: "y", smoke=True, sleep=lambda s: None) == 1
    assert "[FAIL] smoke test" in "\n".join(out)

    out = Lines()
    stuck = Jobs(dict(SMOKE_REPLIES), final="PENDING")
    code = run_doctor(cfg_for(tmp_path), stuck, out, lambda p: "y", smoke=True, sleep=lambda s: None,
                      poll_s=10, wait_s=30)
    assert code == 0 and "stays in the queue" in "\n".join(out)


def test_smoke_needs_writes_and_the_preset(tmp_path):
    out = Lines()
    code = run_doctor(cfg_for(tmp_path, enable_writes=False), FakeRunner(ssh_replies=dict(GOOD)), out,
                      lambda p: "y", smoke=True)
    assert code == 1 and "needs enable_writes" in "\n".join(out)


def test_smoke_asks_before_creating_the_jobs_folder(tmp_path):
    out, asked = Lines(), []
    replies = dict(SMOKE_REPLIES)

    class NoFolder(Jobs):
        def run(self, argv, timeout):
            if argv[0] == "ssh" and argv[-1] == "test -d /scratch/alice/slurm-mcp-jobs" and "mkdir" not in " ".join(self.remote_commands()):
                self.calls.append(list(argv))
                return Result(tuple(argv), 1, "", "")
            return super().run(argv, timeout)

    run_doctor(cfg_for(tmp_path), NoFolder(replies), out, lambda p: asked.append(p) or "n", smoke=True, sleep=lambda s: None)
    assert any("Create the folder" in p for p in asked)
    assert "nothing was created" in "\n".join(out)
