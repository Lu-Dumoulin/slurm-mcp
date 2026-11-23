import asyncio

import pytest

from fakes import FakeRunner
from slurm_mcp.cluster import Cluster
from slurm_mcp.errors import PathError, SlurmMcpError
from slurm_mcp.runner import Result
from slurm_mcp.server import build_server

LOG = "/scratch/alice/run/slurm-1.out"


def cluster(cfg, **replies):
    return Cluster(cfg, FakeRunner(ssh_replies=replies))


def cmds(cl):
    """The commands a tool ran, without the realpath call that resolves the path first."""
    return [c for c in cl.runner.remote_commands() if not c.startswith("realpath")]


def test_read_file_uses_a_fixed_sed_range(cfg):
    cl = cluster(cfg, sed=("line 11\nline 12\n", 0))
    out = cl.read_file(LOG, start_line=11, max_lines=2)
    assert out["text"] == "line 11\nline 12\n" and out["lines_returned"] == 2 and out["end_of_file"] is False
    assert cmds(cl) == [f"sed -n 11,12p {LOG}"]


def test_read_file_detects_the_end_and_clamps_the_range(cfg):
    cl = cluster(cfg, sed=("only\n", 0))
    out = cl.read_file(LOG, start_line=-5, max_lines=10_000)
    assert out["end_of_file"] is True and out["start_line"] == 1
    assert cmds(cl) == [f"sed -n 1,1000p {LOG}"]


def test_read_file_truncates_big_output_and_skips_binary(cfg):
    big = cluster(cfg, sed=("x" * 300_000, 0)).read_file(LOG)
    assert big["truncated"] is True and len(big["text"]) == 200_000
    assert "binary" in cluster(cfg, sed=("ab\0cd", 0)).read_file(LOG)["note"]


def test_read_file_confinement_and_errors(cfg):
    cl = cluster(cfg)
    with pytest.raises(PathError):
        cl.read_file("/etc/shadow")
    assert cl.runner.calls == []
    with pytest.raises(SlurmMcpError, match="cannot read"):
        cluster(cfg, sed=("", 1)).read_file(LOG)


def test_grep_defaults_to_a_fixed_string_and_quotes_the_pattern(cfg):
    cl = cluster(cfg, grep=("12:error: out of memory\n40:error: again\n", 0))
    out = cl.grep_file(LOG, "error: $(id) 'x'")
    assert out["matches"] == 2 and out["regex"] is False and out["limit_reached"] is False
    assert cmds(cl) == [f"grep -n -I -m 100 -F -e 'error: $(id) '\"'\"'x'\"'\"'' -- {LOG}"]


def test_grep_options_regex_context_and_limits(cfg):
    cl = cluster(cfg, grep=("1:a\n", 0))
    cl.grep_file(LOG, "a.*b", regex=True, ignore_case=True, context=99, max_matches=9999)
    assert cmds(cl) == [f"grep -n -I -m 500 -C 10 -i -E -e 'a.*b' -- {LOG}"]


def test_a_pattern_that_looks_like_an_option_cannot_be_one(cfg):
    cl = cluster(cfg, grep=("", 1))
    cl.grep_file(LOG, "-r --include=x")
    assert cmds(cl)[0].endswith(f"-e '-r --include=x' -- {LOG}")


def test_grep_no_match_is_not_an_error_but_real_errors_are(cfg):
    assert cluster(cfg, grep=("", 1)).grep_file(LOG, "zzz")["matches"] == 0
    with pytest.raises(SlurmMcpError, match="cannot search"):
        cluster(cfg, grep=("", 2)).grep_file(LOG, "x")


@pytest.mark.parametrize("bad", ["", "a\nb", "x" * 501, "a\0b"])
def test_bad_patterns_are_refused_before_any_ssh(cfg, bad):
    cl = cluster(cfg)
    with pytest.raises(SlurmMcpError, match="pattern"):
        cl.grep_file(LOG, bad)
    assert cl.runner.calls == []


def test_context_lines_are_not_counted_as_matches_and_limit_is_flagged(cfg):
    text = "3-before\n4:hit\n5-after\n--\n9:hit\n"
    out = cluster(cfg, grep=(text, 0)).grep_file(LOG, "hit", context=1, max_matches=2)
    assert out["matches"] == 2 and out["limit_reached"] is True


def test_connection_failures_are_reported_as_such(cfg):
    class Down(FakeRunner):
        def run(self, argv, timeout):
            return Result(tuple(argv), 255, "", "Permission denied (publickey).")

    with pytest.raises(SlurmMcpError, match="ssh-add"):
        Cluster(cfg, Down()).read_file(LOG)


def test_tools_are_read_only(cfg):
    tools = {t.name: t for t in asyncio.run(build_server(Cluster(cfg, FakeRunner())).list_tools())}
    assert tools["read_file"].annotations.read_only_hint and tools["grep_file"].annotations.read_only_hint
