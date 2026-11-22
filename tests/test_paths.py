import pytest

from slurm_mcp.errors import PathError
from slurm_mcp.paths import is_safe_relative, validate_remote_path

ROOTS = ("/scratch/alice",)


def test_accepts_root_and_children():
    assert validate_remote_path("/scratch/alice", ROOTS) == "/scratch/alice"
    assert validate_remote_path("/scratch/alice/run1/out.log", ROOTS) == "/scratch/alice/run1/out.log"
    assert validate_remote_path("/scratch/alice//run1/./x", ROOTS) == "/scratch/alice/run1/x"


@pytest.mark.parametrize("bad", [
    "/scratch/alice/../bob", "/scratch/alicebob/x", "/etc/passwd", "relative/path",
    "~/x", "/scratch/alice/a b", "/scratch/alice/$(id)", "/scratch/alice/a;rm", "/scratch/alice/*",
    "/scratch/alice/x\n", "", "-rf",
])
def test_rejects(bad):
    with pytest.raises(PathError):
        validate_remote_path(bad, ROOTS)


def test_relative_names():
    assert is_safe_relative("a/b.csv")
    for bad in ("", "/abs", "../x", "a/../b", "a b", "a;b"):
        assert not is_safe_relative(bad)
