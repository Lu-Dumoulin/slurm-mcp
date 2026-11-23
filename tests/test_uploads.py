import asyncio
import os
from pathlib import Path

import pytest

from fakes import FakeRunner, make_config
from slurm_mcp.cluster import Cluster
from slurm_mcp.config import Config
from slurm_mcp.errors import ConfigError, PathError, SlurmMcpError
from slurm_mcp.runner import Result
from slurm_mcp.server import build_server
from slurm_mcp.uploads import (
    DEFAULT_EXCLUDE, UploadRoot, compare_trees, effective_roots, plan_hash, project_dir_problem,
    resolve_local_path, scan_local,
)


def write(path: Path, text: str = "x", mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "work"
    code = root / "proj"
    write(code / "main.jl", "println(1)", 1_700_000_000)
    write(code / "sub" / "kernels.jl", "k", 1_700_000_000)
    write(code / "DF.csv", "a,b\n1,2\n", 1_700_000_000)
    write(code / "data" / "ic.dat", "initial condition")           # data folders are NOT excluded
    write(code / ".git" / "config", "secret-ish")
    write(code / ".venv" / "lib.py")
    write(code / "__pycache__" / "x.pyc")
    write(code / "out" / "result.dat")
    write(code / ".env", "TOKEN=1")
    write(code / "id_ed25519", "key")
    write(code / "my file.jl")                 # space in the name
    (code / "link.jl").symlink_to(code / "main.jl")
    (code / "linkdir").symlink_to(code / "sub")
    return root, code


def roots_for(*paths: Path):
    return tuple(UploadRoot(p.resolve(), "test rule") for p in paths)


# --- which local folders are allowed ----------------------------------------------------------------------

def test_the_agents_working_folder_is_the_default_root(tmp_path):
    home = tmp_path / "home"
    project = home / "code" / "ActiveDewetting"
    project.mkdir(parents=True)
    roots, notes = effective_roots([], cwd=project, home=home, env={})
    assert [r.path for r in roots] == [project.resolve()] and notes == []
    assert "started in" in roots[0].rule


@pytest.mark.parametrize("where", ["/", "HOME", "above_home", "Downloads", "Documents", "/usr/local", "/etc"])
def test_too_broad_working_folders_are_refused(tmp_path, where):
    home = tmp_path / "home" / "me"
    (home / "Downloads").mkdir(parents=True)
    (home / "Documents").mkdir(parents=True)
    cwd = {"HOME": home, "above_home": home.parent, "Downloads": home / "Downloads",
           "Documents": home / "Documents"}.get(where, Path(where))
    roots, notes = effective_roots([], cwd=cwd, home=home, env={})
    assert roots == [] and len(notes) == 1 and "not used as the upload folder" in notes[0]


def test_config_roots_apply_when_the_working_folder_is_refused(tmp_path):
    home = tmp_path / "home"
    extra = home / "Documents" / "GitHub"
    extra.mkdir(parents=True)
    roots, notes = effective_roots([extra.resolve()], cwd=Path("/"), home=home, env={})
    assert [r.path for r in roots] == [extra.resolve()] and "local_roots" in roots[0].rule and "filesystem root" in notes[0]


def test_working_folder_and_config_roots_combine_without_duplicates(tmp_path):
    home = tmp_path / "home"
    project, other = home / "proj", home / "other"
    project.mkdir(parents=True), other.mkdir(parents=True)
    roots, _ = effective_roots([project.resolve(), other.resolve()], cwd=project, home=home, env={})
    assert [r.path for r in roots] == [project.resolve(), other.resolve()]


def test_claude_project_dir_is_used_when_the_client_sets_it(tmp_path):
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    roots, _ = effective_roots([], cwd=Path("/"), home=home, env={"CLAUDE_PROJECT_DIR": str(project)})
    assert [r.path for r in roots] == [project.resolve()] and "CLAUDE_PROJECT_DIR" in roots[0].rule


def test_working_folder_can_be_turned_off(tmp_path):
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    assert effective_roots([], from_cwd=False, cwd=project, home=home, env={}) == ([], [])


def test_project_dir_problem_accepts_a_normal_project(tmp_path):
    home = tmp_path / "home"
    (home / "work" / "proj").mkdir(parents=True)
    assert project_dir_problem((home / "work" / "proj").resolve(), home.resolve()) is None
    assert project_dir_problem(tmp_path / "missing", home) == "not an existing absolute folder"


# --- local path confinement ----------------------------------------------------------------------------------

def test_local_path_must_be_inside_a_root(tree, tmp_path):
    root, code = tree
    roots = roots_for(root)
    assert resolve_local_path(str(code), roots)[0] == code.resolve()
    with pytest.raises(SlurmMcpError, match="outside"):
        resolve_local_path(str(tmp_path), roots)
    with pytest.raises(SlurmMcpError, match="outside"):
        resolve_local_path(str(code / ".." / ".."), roots)
    with pytest.raises(SlurmMcpError, match="absolute"):
        resolve_local_path("proj", roots)
    with pytest.raises(SlurmMcpError, match="does not exist"):
        resolve_local_path(str(root / "nope"), roots)
    with pytest.raises(SlurmMcpError, match="not set up"):
        resolve_local_path(str(code), ())


def test_local_path_defaults_to_the_single_allowed_folder(tree):
    root, code = tree
    real, rule = resolve_local_path(None, roots_for(code))
    assert real == code.resolve() and rule.rule == "test rule"
    with pytest.raises(SlurmMcpError, match="give local_path"):
        resolve_local_path(None, roots_for(code, root))


def test_symlink_that_leaves_the_root_is_refused(tree, tmp_path):
    root, _ = tree
    outside = tmp_path / "elsewhere"
    write(outside / "secret.txt")
    (root / "escape").symlink_to(outside)
    with pytest.raises(SlurmMcpError, match="outside"):
        resolve_local_path(str(root / "escape"), roots_for(root))


# --- what gets scanned -------------------------------------------------------------------------------------------

def test_scan_excludes_secrets_clutter_links_and_unsafe_names_but_keeps_data(tree):
    _, code = tree
    scan = scan_local(code, DEFAULT_EXCLUDE, 100)
    assert sorted(f.rel for f in scan.files) == ["DF.csv", "data/ic.dat", "main.jl", "sub/kernels.jl"]
    assert scan.excluded >= 6                      # .git .venv __pycache__ out .env id_ed25519
    assert scan.symlinks == 2                      # file link and directory link, never followed
    assert scan.unsafe_names == 1                  # "my file.jl"


def test_scan_single_file_and_excluded_single_file(tree):
    _, code = tree
    assert [f.rel for f in scan_local(code / "main.jl", DEFAULT_EXCLUDE, 10).files] == ["main.jl"]
    with pytest.raises(SlurmMcpError, match="never uploaded"):
        scan_local(code / ".env", DEFAULT_EXCLUDE, 10)


def test_scan_has_a_file_count_limit_but_no_size_limit(tree):
    _, code = tree
    with pytest.raises(SlurmMcpError, match="more than 2 files"):
        scan_local(code, DEFAULT_EXCLUDE, 2)
    big = write(code / "huge.bin", "y" * 2_000_000)
    assert big.name in [f.rel for f in scan_local(code, DEFAULT_EXCLUDE, 100).files]


def test_extra_excludes_apply_to_names_and_paths(tree):
    _, code = tree
    scan = scan_local(code, DEFAULT_EXCLUDE + ("sub", "*.csv", "data"), 100)
    assert [f.rel for f in scan.files] == ["main.jl"]


def test_plan_hash_changes_with_any_part():
    base = plan_hash("/a", "/r", [("f", 1, 2, "missing")])
    assert base == plan_hash("/a", "/r", [("f", 1, 2, "missing")])
    for other in (plan_hash("/b", "/r", [("f", 1, 2, "missing")]), plan_hash("/a", "/s", [("f", 1, 2, "missing")]),
                  plan_hash("/a", "/r", [("f", 1, 3, "missing")]), plan_hash("/a", "/r", [("g", 1, 2, "missing")])):
        assert other != base


# --- config ------------------------------------------------------------------------------------------------------------

def test_config_upload_keys(tmp_path):
    base = dict(host="h.example.org", user="bob", remote_roots=["/s/bob"], local_dir=str(tmp_path))
    cfg = Config.from_dict({**base, "local_roots": [str(tmp_path)], "upload_exclude": ["*.h5"]})
    assert cfg.local_roots == (tmp_path.resolve(),) and cfg.upload_exclude == ("*.h5",)
    plain = Config.from_dict(base)
    assert plain.local_roots == () and plain.upload_from_cwd is True and plain.large_file_mb == 10
    assert not hasattr(plain, "max_upload_mb") and not hasattr(plain, "max_upload_file_mb")   # no size limits
    for bad in ({"local_roots": "x"}, {"local_roots": ["relative/dir"]}, {"upload_exclude": [""]}):
        with pytest.raises(ConfigError):
            Config.from_dict({**base, **bad})


# --- the two-step flow ------------------------------------------------------------------------------------------------------

REMOTE = "/scratch/alice/Code/proj"
BASE_REPLIES = {"realpath": (REMOTE + "\n", 0), "test -d": ("", 1), "mkdir": ("", 0), "find": ("", 0)}


def ucluster(tmp_path, root, existing="", dir_exists=False, **over):
    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),), **over)
    replies = dict(BASE_REPLIES, find=(existing, 0))
    if dir_exists:
        replies["test -d"] = ("", 0)
    return Cluster(cfg, FakeRunner(ssh_replies=replies))


def test_dry_run_reports_everything_and_changes_nothing(tree, tmp_path):
    root, code = tree
    cl = ucluster(tmp_path, root)
    out = cl.plan_upload_code(REMOTE, str(code))
    assert out["to"] == f"alice@login.example.org:{REMOTE}" and out["deletes_remote_files"] is False
    assert out["allowed_because"] == "listed in local_roots in the config"
    assert sorted(f["path"] for f in out["files"]) == ["DF.csv", "data/ic.dat", "main.jl", "sub/kernels.jl"]
    assert all(f["action"] == "create" for f in out["files"])
    assert out["confirmation_token"] and out["plan_sha256"] and out["files_to_send"] == 4
    assert out["large_files"] == [] and out["needs_large_file_confirmation"] is False
    assert any("symbolic link" in w for w in out["warnings"])
    assert not any(c.startswith(("mkdir", "mv", "rm")) for c in cl.runner.remote_commands())
    assert cl.runner.uploads == {}


def test_local_path_defaults_to_the_working_folder(tree, tmp_path):
    _, code = tree
    cfg = make_config(tmp_path, enable_writes=True, local_roots=(code.resolve(),))
    cl = Cluster(cfg, FakeRunner(ssh_replies=dict(BASE_REPLIES)))
    assert cl.plan_upload_code(REMOTE)["from"] == str(code.resolve())


def test_upload_copies_aside_then_renames_and_never_deletes(tree, tmp_path):
    root, code = tree
    cl = ucluster(tmp_path, root)
    plan = cl.plan_upload_code(REMOTE, str(code))
    res = cl.upload_code(plan["confirmation_token"])
    assert res["failed"] == [] and res["uploaded"] == plan["files_to_send"] == 4
    scp = [c for c in cl.runner.calls if c[0] == "scp"]
    assert all("-p" in c for c in scp)
    assert all(c[-1].endswith(".slurm-mcp-part") for c in scp)                       # copied under a temporary name
    cmds = cl.runner.remote_commands()
    assert f"mv -f -- {REMOTE}/.main.jl.slurm-mcp-part {REMOTE}/main.jl" in cmds     # then renamed into place
    assert set(cl.runner.uploads) == {f"{REMOTE}/DF.csv", f"{REMOTE}/main.jl", f"{REMOTE}/sub/kernels.jl",
                                      f"{REMOTE}/data/ic.dat"}                       # no temp names left
    assert f"mkdir -p -- {REMOTE} {REMOTE}/data {REMOTE}/sub" in cmds
    assert not any(c.startswith("rm") for c in cmds)                                 # nothing was deleted
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        cl.upload_code(plan["confirmation_token"])


def test_a_failed_copy_leaves_the_old_file_intact_and_removes_only_the_temp_file(tree, tmp_path):
    root, code = tree

    class Failing(FakeRunner):
        def _scp(self, argv):
            if ".main.jl.slurm-mcp-part" in argv[-1]:
                return Result(tuple(argv), 1, "", "No space left on device")
            return super()._scp(argv)

    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),))
    cl = Cluster(cfg, Failing(ssh_replies=dict(BASE_REPLIES)))
    cl.runner.uploads[f"{REMOTE}/main.jl"] = "OLD VERSION"
    res = cl.upload_code(cl.plan_upload_code(REMOTE, str(code))["confirmation_token"])
    assert cl.runner.uploads[f"{REMOTE}/main.jl"] == "OLD VERSION"
    assert res["uploaded"] == 3 and res["failed"][0]["path"] == f"{REMOTE}/main.jl"
    removals = [c for c in cl.runner.remote_commands() if c.startswith("rm")]
    assert removals == [f"rm -f -- {REMOTE}/.main.jl.slurm-mcp-part"]


def test_a_failed_rename_is_reported_and_cleaned_up(tree, tmp_path):
    root, code = tree

    class NoMove(FakeRunner):
        def run(self, argv, timeout):
            if argv[0] == "ssh" and argv[-1].startswith("mv -f"):
                self.calls.append(list(argv))
                return Result(tuple(argv), 1, "", "Permission denied")
            return super().run(argv, timeout)

    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),))
    cl = Cluster(cfg, NoMove(ssh_replies=dict(BASE_REPLIES)))
    res = cl.upload_code(cl.plan_upload_code(REMOTE, str(code))["confirmation_token"])
    assert res["uploaded"] == 0 and len(res["failed"]) == 4
    assert len([c for c in cl.runner.remote_commands() if c.startswith("rm -f")]) == 4


def test_only_missing_or_newer_files_are_sent_and_overwrites_are_flagged(tree, tmp_path):
    root, code = tree
    existing = (f"f\tmain.jl\t{(code / 'main.jl').stat().st_size}\t1700000000.0\n"          # same: skipped
                "f\tDF.csv\t3\t1700000000.0\n"                                               # size differs: overwrite
                "f\tsub/kernels.jl\t1\t1600000000.0\n")                                      # same size, local newer
    out = ucluster(tmp_path, root, existing, dir_exists=True).plan_upload_code(REMOTE, str(code))
    actions = {f["path"]: f["action"] for f in out["files"]}
    assert "main.jl" not in actions and actions["DF.csv"] == "overwrite" and actions["sub/kernels.jl"] == "overwrite"
    assert actions["data/ic.dat"] == "create" and out["files_up_to_date"] == 1
    assert any("2 existing remote file(s) would be overwritten" in w for w in out["warnings"])


def test_nothing_to_upload_gives_no_token(tree, tmp_path):
    root, code = tree
    size = (code / "main.jl").stat().st_size
    cl = ucluster(tmp_path, root, f"f\tmain.jl\t{size}\t1700000000.0\n", dir_exists=True)
    out = cl.plan_upload_code(REMOTE, str(code / "main.jl"))
    assert out["confirmation_token"] is None and out["files_to_send"] == 0 and "nothing to upload" in out["warnings"][0]


def test_a_file_changed_after_the_dry_run_is_not_uploaded(tree, tmp_path):
    root, code = tree
    cl = ucluster(tmp_path, root)
    plan = cl.plan_upload_code(REMOTE, str(code))
    (code / "main.jl").write_text("changed after review")
    with pytest.raises(SlurmMcpError, match="changed since the dry run"):
        cl.upload_code(plan["confirmation_token"])
    assert cl.runner.uploads == {} and not any(c.startswith("mkdir") for c in cl.runner.remote_commands())


def test_remote_destination_is_checked_by_real_path(tree, tmp_path):
    root, code = tree
    cl = ucluster(tmp_path, root)
    with pytest.raises(PathError):
        cl.plan_upload_code("/etc/cron.d", str(code))                        # lexically outside the roots
    cl.runner.ssh_replies["realpath"] = ("/etc/cron.d\n", 0)                 # a symlink inside the roots leading out
    with pytest.raises(PathError, match="outside the configured roots"):
        cl.plan_upload_code("/scratch/alice/link", str(code))
    cl.runner.ssh_replies["realpath"] = ("", 1)
    with pytest.raises(SlurmMcpError, match="could not resolve"):
        cl.plan_upload_code(REMOTE, str(code))


def test_local_source_outside_the_allowlist_is_refused_before_any_ssh(tree, tmp_path):
    root, _ = tree
    other = tmp_path / "private"
    write(other / "a.txt")
    cl = ucluster(tmp_path, root)
    with pytest.raises(SlurmMcpError, match="outside the allowed local folders"):
        cl.plan_upload_code(REMOTE, str(other))
    assert cl.runner.calls == []


def test_upload_needs_writes_and_an_allowed_folder(tree, tmp_path):
    root, code = tree
    off = Cluster(make_config(tmp_path, local_roots=(root.resolve(),)), FakeRunner())
    with pytest.raises(SlurmMcpError, match="disabled"):
        off.plan_upload_code(REMOTE, str(code))
    none = Cluster(make_config(tmp_path, enable_writes=True), FakeRunner())
    with pytest.raises(SlurmMcpError, match="not set up"):
        none.plan_upload_code(REMOTE, str(code))


# --- large files need an explicit confirmation ---------------------------------------------------------------------------

@pytest.fixture
def with_large(tree):
    root, code = tree
    write(code / "ic_big.dat", "y" * 1_200_000, 1_700_000_000)
    write(code / "sub" / "ic_big2.dat", "z" * 1_300_000, 1_700_000_000)
    return root, code


def test_large_files_are_listed_and_need_confirmation(with_large, tmp_path):
    root, code = with_large
    cl = ucluster(tmp_path, root, large_file_mb=1)
    plan = cl.plan_upload_code(REMOTE, str(code))
    assert plan["needs_large_file_confirmation"] is True
    assert [f["path"] for f in plan["large_files"]] == ["ic_big.dat", "sub/ic_big2.dat"]
    assert plan["large_files"][0]["size"] == 1_200_000
    assert any("2 file(s) are larger than 1 MB" in w for w in plan["warnings"]) and "confirm_large_files" in plan["next_step"]

    with pytest.raises(SlurmMcpError, match="larger than 1 MB"):                       # nothing given
        cl.upload_code(plan["confirmation_token"])
    with pytest.raises(SlurmMcpError, match="confirm_large_files"):                    # only part of the list
        cl.upload_code(plan["confirmation_token"], ["ic_big.dat"])
    with pytest.raises(SlurmMcpError, match="confirm_large_files"):                    # a different list
        cl.upload_code(plan["confirmation_token"], ["ic_big.dat", "sub/other.dat"])
    assert cl.runner.uploads == {}                                                     # nothing sent, token still valid

    res = cl.upload_code(plan["confirmation_token"], ["sub/ic_big2.dat", "ic_big.dat"])   # order does not matter
    assert res["failed"] == [] and f"{REMOTE}/ic_big.dat" in cl.runner.uploads


def test_no_confirmation_is_needed_without_large_files(tree, tmp_path):
    root, code = tree
    cl = ucluster(tmp_path, root, large_file_mb=1)
    assert cl.upload_code(cl.plan_upload_code(REMOTE, str(code))["confirmation_token"])["uploaded"] == 4


def test_large_file_limit_is_a_threshold_not_a_cap(with_large, tmp_path):
    root, code = with_large
    plan = ucluster(tmp_path, root, large_file_mb=10).plan_upload_code(REMOTE, str(code))
    assert plan["large_files"] == [] and plan["files_to_send"] == 6      # 1.2 MB files are not "large" at 10 MB


def test_partial_failure_is_reported(tree, tmp_path):
    class Flaky(FakeRunner):
        def _scp(self, argv):
            if ".DF.csv." in argv[-1]:
                return Result(tuple(argv), 1, "", "quota exceeded")
            return super()._scp(argv)

    root, code = tree
    cfg = make_config(tmp_path, enable_writes=True, local_roots=(root.resolve(),))
    cl = Cluster(cfg, Flaky(ssh_replies=dict(BASE_REPLIES)))
    res = cl.upload_code(cl.plan_upload_code(REMOTE, str(code))["confirmation_token"])
    assert res["uploaded"] == 3 and len(res["failed"]) == 1 and res["failed"][0]["path"].endswith("DF.csv")


# --- comparing local and cluster -------------------------------------------------------------------------------------------

def test_compare_trees_uses_sizes_and_labels_same_size_files_by_time():
    local = [("same", 5, 1000.0), ("size", 5, 1000.0), ("lnew", 5, 2000.0), ("rnew", 5, 1000.0), ("only_l", 1, 1.0)]
    remote = {"same": (5, 1001.0), "size": (6, 1000.0), "lnew": (5, 1000.0), "rnew": (5, 2000.0), "only_r": (1, 1.0)}
    c = compare_trees(local, remote)
    assert c.matching == ("same",) and c.size_differs == ("size",) and c.local_newer == ("lnew",)
    assert c.remote_newer == ("rnew",) and c.only_local == ("only_l",) and c.only_remote == ("only_r",)
    assert not c.identical
    assert compare_trees([("a", 1, 1.0)], {"a": (1, 1.0), "extra": (9, 9.0)}).identical   # extras on the cluster are fine


def test_compare_code_reports_each_category(tree, tmp_path):
    root, code = tree
    existing = (f"f\tmain.jl\t{(code / 'main.jl').stat().st_size}\t1700000000.0\n"
                "f\tDF.csv\t3\t1700000000.0\n"
                "f\tsub/kernels.jl\t1\t1600000000.0\n"
                "f\toutput.h5\t100\t1.0\n")
    out = ucluster(tmp_path, root, existing, dir_exists=True).compare_code(REMOTE, str(code))
    assert out["identical"] is False and out["matching"] == 1
    assert out["size_differs"]["files"] == ["DF.csv"] and out["local_newer"]["files"] == ["sub/kernels.jl"]
    assert out["only_local"]["files"] == ["data/ic.dat"] and out["only_remote"]["files"] == ["output.h5"]
    assert out["remote_folder_exists"] is True


def test_compare_code_on_a_missing_remote_folder_and_without_writes(tree, tmp_path):
    root, code = tree
    cfg = make_config(tmp_path, local_roots=(root.resolve(),))              # writes disabled: comparing is read-only
    cl = Cluster(cfg, FakeRunner(ssh_replies=dict(BASE_REPLIES)))
    out = cl.compare_code(REMOTE, str(code))
    assert out["remote_folder_exists"] is False and out["identical"] is False and out["only_local"]["count"] == 4
    assert not any(c.startswith(("mkdir", "mv", "rm")) for c in cl.runner.remote_commands())


def test_verify_after_upload(tree, tmp_path):
    root, code = tree
    sizes = {r: (code / r).stat().st_size for r in ("DF.csv", "data/ic.dat", "main.jl", "sub/kernels.jl")}
    good = "".join(f"f\t{r}\t{s}\t1700000000.0\n" for r, s in sizes.items())
    cl = ucluster(tmp_path, root, good)
    plan = cl.plan_upload_code(REMOTE, str(code))
    cl.runner.ssh_replies["test -d"] = ("", 0)                                   # exists after the upload
    result = cl.upload_code(plan["confirmation_token"], verify=True)
    assert result["verification"] == {"checked": 4, "all_match": True, "missing_on_cluster": [], "size_differs": []}

    bad = good.replace(f"f\tmain.jl\t{sizes['main.jl']}", "f\tmain.jl\t1").replace("f\tDF.csv", "f\tDF.csv.bak")
    cl2 = ucluster(tmp_path, root, bad)
    plan2 = cl2.plan_upload_code(REMOTE, str(code))
    cl2.runner.ssh_replies["test -d"] = ("", 0)
    v = cl2.upload_code(plan2["confirmation_token"], verify=True)["verification"]
    assert v["all_match"] is False and v["size_differs"] == ["main.jl"] and v["missing_on_cluster"] == ["DF.csv"]


def test_verification_is_off_by_default(tree, tmp_path):
    root, code = tree
    cl = ucluster(tmp_path, root)
    assert "verification" not in cl.upload_code(cl.plan_upload_code(REMOTE, str(code))["confirmation_token"])


# --- tool registration ----------------------------------------------------------------------------------------------------------

def test_tools_are_registered_only_when_they_apply(tree, tmp_path):
    root, _ = tree
    names = lambda c: {t.name: t for t in asyncio.run(build_server(c).list_tools())}     # noqa: E731
    nothing = names(Cluster(make_config(tmp_path, enable_writes=True), FakeRunner()))
    assert not {"upload_code", "upload_code_dry_run", "compare_code"} & set(nothing)
    read_only = names(Cluster(make_config(tmp_path, local_roots=(root.resolve(),)), FakeRunner()))
    assert "compare_code" in read_only and "upload_code" not in read_only
    assert read_only["compare_code"].annotations.read_only_hint is True
    on = names(ucluster(tmp_path, root))
    assert on["upload_code"].annotations.destructive_hint is True and on["upload_code"].annotations.read_only_hint is False
    assert on["upload_code_dry_run"].annotations.read_only_hint is True
    assert list(on["upload_code"].input_schema["properties"]) == ["confirmation_token", "review_text", "confirm_large_files", "verify"]
    assert on["upload_code"].input_schema["required"] == ["confirmation_token", "review_text"]
    assert "local_path" not in on["upload_code_dry_run"].input_schema["required"]
