import asyncio

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.errors import PathError, SlurmMcpError
from slurm_mcp.server import build_server

ROOT = "/scratch/alice"
TARGET = f"{ROOT}/old_runs/run1"
def rec(kind, size, name):
    return f"{kind}\t{size}\t{name}\0"


LISTING = (rec("d", 4096, TARGET) + rec("f", 1000, f"{TARGET}/a.dat") + rec("f", 5000000, f"{TARGET}/big.dat")
           + rec("d", 4096, f"{TARGET}/sub") + rec("f", 200, f"{TARGET}/sub/b.dat") + rec("l", 12, f"{TARGET}/link"))


def dcluster(tmp_path, listing=LISTING, resolved=TARGET, **over):
    cfg = make_config(tmp_path, enable_writes=True, **over)
    return Cluster(cfg, FakeRunner(ssh_replies={"realpath": (resolved + "\n", 0), "test -e": ("", 0),
                                                "find": (listing, 0), "rm -r": ("", 0)}))


def rm_calls(cl):
    return [c for c in cl.runner.remote_commands() if c.startswith("rm")]


def test_dry_run_lists_what_would_go_and_deletes_nothing(tmp_path):
    cl = dcluster(tmp_path)
    out = cl.plan_delete(TARGET)
    assert (out["files"], out["directories"], out["symbolic_links"], out["total_bytes"]) == (3, 2, 1, 5_001_200)
    assert out["largest_files"][0] == {"path": f"{TARGET}/big.dat", "size": 5_000_000}
    assert out["command"] == f"rm -r -- {TARGET}" and out["confirmation_token"] and out["plan_sha256"]
    assert any("cannot be recovered" in w for w in out["warnings"])
    assert rm_calls(cl) == []


def test_delete_runs_exactly_the_reviewed_command_once(tmp_path):
    cl = dcluster(tmp_path)
    token = cl.plan_delete(TARGET)["confirmation_token"]
    assert cl.delete(token)["deleted"] == TARGET
    assert rm_calls(cl) == [f"rm -r -- {TARGET}"]
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        cl.delete(token)


def test_content_changes_after_the_dry_run_stop_the_delete(tmp_path):
    cl = dcluster(tmp_path)
    token = cl.plan_delete(TARGET)["confirmation_token"]
    cl.runner.ssh_replies["find"] = (LISTING + rec("f", 7, f"{TARGET}/new.dat"), 0)       # a file appeared meanwhile
    with pytest.raises(SlurmMcpError, match="contents changed"):
        cl.delete(token)
    assert rm_calls(cl) == []


@pytest.mark.parametrize("path,match", [
    (ROOT, "configured root"),
    (ROOT + "/", "configured root"),
    ("/scratch", "outside the configured roots"),      # above a root: not inside any root
    ("/", "outside the configured roots"),
    (f"{ROOT}/.ssh/keys", "protected"),
    (f"{ROOT}/x/.ssh", "protected"),
])
def test_roots_their_parents_and_protected_folders_are_never_deleted(tmp_path, path, match):
    cl = dcluster(tmp_path)
    with pytest.raises(SlurmMcpError, match=match):
        cl.plan_delete(path)
    assert cl.runner.calls == []


def test_paths_outside_the_roots_and_odd_paths_are_refused_before_any_ssh(tmp_path):
    cl = dcluster(tmp_path)
    for bad in ("/etc/passwd", f"{ROOT}/../bob", f"{ROOT}/a b", f"{ROOT}/*", "relative", "~"):
        with pytest.raises((PathError, SlurmMcpError)):
            cl.plan_delete(bad)
    assert cl.runner.calls == []


def test_symlinks_are_refused_by_real_path(tmp_path):
    out_of_roots = dcluster(tmp_path, resolved="/etc")                  # the path is a link leading out of the roots
    with pytest.raises(PathError, match="outside the configured roots"):
        out_of_roots.plan_delete(TARGET)
    inside = dcluster(tmp_path, resolved=f"{ROOT}/elsewhere")            # a link that stays inside is still refused
    with pytest.raises(SlurmMcpError, match="symbolic link is involved"):
        inside.plan_delete(TARGET)
    unresolved = dcluster(tmp_path)
    unresolved.runner.ssh_replies["realpath"] = ("", 1)
    with pytest.raises(SlurmMcpError, match="could not resolve"):
        unresolved.plan_delete(TARGET)


def test_missing_target_and_huge_trees(tmp_path):
    missing = dcluster(tmp_path)
    missing.runner.ssh_replies["test -e"] = ("", 1)
    with pytest.raises(SlurmMcpError, match="does not exist"):
        missing.plan_delete(TARGET)
    big = dcluster(tmp_path, listing="".join(rec("f", 1, f"{TARGET}/f{i}") for i in range(100_001)))
    with pytest.raises(SlurmMcpError, match="delete smaller parts"):
        big.plan_delete(TARGET)


def test_the_guards_run_again_at_execution(tmp_path):
    cl = dcluster(tmp_path)
    token = cl.plan_delete(TARGET)["confirmation_token"]
    cl.runner.ssh_replies["realpath"] = ("/etc\n", 0)                    # the folder was swapped for a link meanwhile
    with pytest.raises(PathError):
        cl.delete(token)
    assert rm_calls(cl) == []


def test_other_tokens_cannot_delete_and_delete_needs_writes(tmp_path):
    cl = dcluster(tmp_path)
    with pytest.raises(SlurmMcpError, match="not delete"):
        cl.delete(cl.plan_cancel_many.__self__.confirmations.issue("cancel", ["scancel", "1"], "x"))
    off = Cluster(make_config(tmp_path), FakeRunner())
    with pytest.raises(SlurmMcpError, match="disabled"):
        off.plan_delete(TARGET)


def test_tools_are_registered_and_annotated(tmp_path):
    tools = {t.name: t for t in asyncio.run(build_server(dcluster(tmp_path)).list_tools())}
    assert tools["delete"].annotations.destructive_hint is True and tools["delete_dry_run"].annotations.read_only_hint is True
    assert list(tools["delete"].input_schema["properties"]) == ["confirmation_token", "review_text"]


def test_a_folder_that_contains_another_root_is_refused(tmp_path):
    cfg = make_config(tmp_path, enable_writes=True, remote_roots=("/h/user", "/h/user/scratch/x"))
    cl = Cluster(cfg, FakeRunner())
    with pytest.raises(SlurmMcpError, match="contains one"):
        cl.plan_delete("/h/user/scratch")
    assert cl.runner.calls == []


def test_a_file_name_with_a_newline_cannot_hide_or_fake_an_entry(tmp_path):
    tricky = f"{TARGET}/a\nf\t1\t{TARGET}/fake"                      # one real file whose name looks like two records
    cl = dcluster(tmp_path, listing=rec("d", 4096, TARGET) + rec("f", 10, tricky))
    out = cl.plan_delete(TARGET)
    assert out["files"] == 1 and out["total_bytes"] == 10
    assert any("\n" in e["path"] for e in out["first_entries"])           # shown as the one entry it is
    assert "command: rm -r -- " + TARGET in out["review_text"]
