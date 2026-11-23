"""Windows line endings (CRLF): a generated script must reach the cluster with LF only."""
import hashlib
import pathlib

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp import commands as c
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import SlurmMcpError
from slurm_mcp.runner import Result
from slurm_mcp.uploads import crlf_files, script_bytes, LocalFile


def pcluster(tmp_path, runner=None):
    cfg = Config.from_dict(dict(host="h.example.org", user="alice", remote_roots=["/scratch/alice"], local_dir=str(tmp_path),
                                enable_writes=True, presets={"debug": dict(partition="p", time="00:10:00", mem="1G")}))
    base = {"test -d": ("", 0), "squeue": ("", 0), "sbatch": ("9\n", 0), "mkdir": ("", 0)}
    return Cluster(cfg, runner or FakeRunner(ssh_replies=base))


def dry(cl):
    return cl.plan_submit_preset("debug", False, ["echo", "hi"], "/scratch/alice", "j")


@pytest.fixture
def windows_text_mode(monkeypatch):
    """Path.write_text on Windows turns every "\\n" into "\\r\\n". Simulate that on any system."""
    original = pathlib.Path.write_text

    def windows_write_text(self, data, encoding=None, errors=None, newline=None):
        return original(self, data.replace("\n", "\r\n"), encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(pathlib.Path, "write_text", windows_write_text)


def test_the_simulation_really_changes_text_mode_writes(tmp_path, windows_text_mode):
    f = tmp_path / "x"
    f.write_text("a\nb\n")
    assert f.read_bytes() == b"a\r\nb\r\n"          # the bug this test guards against


def test_the_generated_script_reaches_the_cluster_with_lf_only(tmp_path, windows_text_mode):
    cl = pcluster(tmp_path)
    plan = dry(cl)
    cl.submit(plan["confirmation_token"], plan["review_text"])
    sent = cl.runner.uploads[plan["script_will_be_written_to"]]
    assert "\r" not in sent and sent == plan["script"]
    assert hashlib.sha256(sent.encode()).hexdigest() == plan["script_sha256"]    # the hash covers exactly these bytes
    assert any(x.startswith("sbatch") for x in cl.runner.remote_commands())


def test_a_script_changed_on_the_way_is_not_submitted(tmp_path):
    class Corrupting(FakeRunner):
        def _scp(self, argv):
            res = super()._scp(argv)
            for k, v in list(self.uploads.items()):
                self.uploads[k] = v.replace("\n", "\r\n")        # whatever changes the file in transit
            return res

    cl = pcluster(tmp_path, Corrupting(ssh_replies={"test -d": ("", 0), "squeue": ("", 0), "sbatch": ("9\n", 0), "mkdir": ("", 0)}))
    plan = dry(cl)
    with pytest.raises(SlurmMcpError, match="not the reviewed script, nothing was submitted"):
        cl.submit(plan["confirmation_token"], plan["review_text"])
    assert not any(x.startswith("sbatch") for x in cl.runner.remote_commands())


def test_the_dry_run_refuses_a_generated_script_with_a_carriage_return(tmp_path, monkeypatch):
    monkeypatch.setattr("slurm_mcp.cluster.build_script", lambda spec: "#!/bin/bash\r\necho hi\r\n")
    cl = pcluster(tmp_path)
    with pytest.raises(SlurmMcpError, match="carriage return"):
        dry(cl)
    assert cl.confirmations._pending == {}                    # no token was issued


def test_script_bytes_are_utf_8_with_lf():
    assert script_bytes("#!/bin/bash\necho Δ\n") == b"#!/bin/bash\necho \xce\x94\n"


def test_the_grep_command_looks_for_a_carriage_return_without_a_shell_trick():
    assert c.grep_cr_cmd("/s/a b.sh") == ["grep", "-c", "-P", "\\r", "--", "/s/a b.sh"]


# --- an existing script on the cluster ----------------------------------------------------------------------------

def existing(tmp_path, grep):
    cfg = make_config(tmp_path, enable_writes=True)
    replies = {"test -f": ("", 0), "head -c": ("#!/bin/bash\necho hi\n", 0), "squeue": ("", 0), "sbatch": ("5\n", 0),
               "sha256sum": ("a" * 64 + "  /x\n", 0)}
    replies = {**grep, **replies}                                  # the grep reply comes first
    return Cluster(cfg, FakeRunner(ssh_replies=replies))


def test_an_existing_script_with_crlf_is_refused_with_the_fix(tmp_path):
    cl = existing(tmp_path, {"grep -c -P": ("12\n", 0)})
    with pytest.raises(SlurmMcpError) as err:
        cl.plan_submit("/scratch/alice/run/job.sh")
    message = str(err.value)
    assert "Windows line endings (CRLF)" in message and "sbatch refuses" in message
    assert "dos2unix" in message and "core.autocrlf" in message
    assert not any(x.startswith("sbatch") for x in cl.runner.remote_commands())


@pytest.mark.parametrize("reply", [("0\n", 1), ("", 1), ("", 2)])       # none, none, grep without -P (unknown)
def test_a_clean_or_unknown_script_goes_on(tmp_path, reply):
    plan = existing(tmp_path, {"grep -c -P": reply}).plan_submit("/scratch/alice/run/job.sh")
    assert plan["confirmation_token"]


def test_a_lost_connection_is_not_hidden_by_the_check(tmp_path):
    with pytest.raises(SlurmMcpError, match="SSH connection failed"):
        existing(tmp_path, {"grep -c -P": ("", 255), "true": ("", 255)}).plan_submit("/scratch/alice/run/job.sh")


# --- upload: warn, never convert ---------------------------------------------------------------------------------------

def test_crlf_files_found_by_extension(tmp_path):
    files = []
    for name, data in (("run.sh", b"a\r\nb\n"), ("main.jl", b"x\r\n"), ("t.csv", b"a,b\r\n1,2\r\n"), ("ok.py", b"a\nb\n"),
                       ("notes.txt", b"a\r\n"), ("RUN2.SH", b"#!/bin/sh\r\n")):
        p = tmp_path / name
        p.write_bytes(data)
        files.append(LocalFile(name, str(p), len(data), 1.0, 1))
    assert crlf_files(files) == ["run.sh", "main.jl", "t.csv", "RUN2.SH"]


def test_the_upload_dry_run_warns_about_crlf_and_the_upload_keeps_the_bytes(tmp_path):
    root = tmp_path / "work"
    code = root / "proj"
    code.mkdir(parents=True)
    (code / "run.sh").write_bytes(b"#!/bin/bash\r\necho hi\r\n")
    (code / "main.jl").write_bytes(b"println(1)\n")
    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),))
    cl = Cluster(cfg, FakeRunner(ssh_replies={"realpath": ("/scratch/alice/p\n", 0), "test -d": ("", 1), "mkdir": ("", 0)}))
    plan = cl.plan_upload_code("/scratch/alice/p", str(code))
    warning = next(w for w in plan["warnings"] if "line endings" in w)
    assert "1 file(s) have Windows line endings (CRLF): run.sh" in warning and "does not convert them" in warning
    assert "core.autocrlf" in warning
    assert "WARNING: 1 file(s) have Windows line endings" in plan["review_text"]       # the approval prompt shows it
    cl.upload_code(plan["confirmation_token"], review_text=plan["review_text"])
    assert cl.runner.uploads["/scratch/alice/p/run.sh"] == "#!/bin/bash\r\necho hi\r\n"   # sent as it is


def test_no_warning_for_lf_files(tmp_path):
    root = tmp_path / "work"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "run.sh").write_bytes(b"#!/bin/bash\n")
    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),))
    cl = Cluster(cfg, FakeRunner(ssh_replies={"realpath": ("/scratch/alice/p\n", 0), "test -d": ("", 1)}))
    plan = cl.plan_upload_code("/scratch/alice/p", str(root / "proj"))
    assert not any("line endings" in w for w in plan["warnings"]) and "WARNING" not in plan["review_text"]
