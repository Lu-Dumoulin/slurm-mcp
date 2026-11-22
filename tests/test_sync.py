from pathlib import Path

from slurm_mcp.sync import RemoteFile, plan_sync, scan_local


def rf(rel, size=10, mtime=1000.0):
    return RemoteFile(rel, size, mtime)


def test_missing_size_newer_uptodate():
    remote = [rf("new.csv"), rf("size.csv", 20), rf("newer.csv", 10, 2000.0), rf("same.csv")]
    local = {"size.csv": (10, 1000.0), "newer.csv": (10, 1000.0), "same.csv": (10, 1000.0)}
    plan = plan_sync(remote, local)
    assert {p.file.rel: p.reason for p in plan.download} == {
        "new.csv": "missing", "size.csv": "size", "newer.csv": "newer"}
    assert plan.up_to_date == ("same.csv",)
    assert plan.total_bytes == 10 + 20 + 10


def test_mtime_tolerance():
    plan = plan_sync([rf("a", 10, 1001.5)], {"a": (10, 1000.0)})
    assert plan.download == ()


def test_local_newer_is_kept():
    assert plan_sync([rf("a", 10, 1000.0)], {"a": (10, 5000.0)}).download == ()


def test_pattern_matches_path_or_basename():
    remote = [rf("x/a.csv"), rf("x/b.txt"), rf("c.csv")]
    plan = plan_sync(remote, {}, "*.csv")
    assert [p.file.rel for p in plan.download] == ["c.csv", "x/a.csv"]


def test_scan_local_ignores_part_files(tmp_path: Path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.csv").write_text("hi")
    (tmp_path / "d" / "b.csv.part").write_text("partial")
    assert list(scan_local(tmp_path)) == ["d/a.csv"]
    assert scan_local(tmp_path / "missing") == {}
