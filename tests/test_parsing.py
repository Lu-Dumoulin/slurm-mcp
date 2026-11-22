from slurm_mcp.parsing import parse_find, parse_sacct, parse_scontrol, parse_squeue


def test_squeue_name_may_contain_separator():
    text = "123|RUNNING|cpu|0:10|1:00:00|1|node01|2026-01-01T10:00:00|my|odd|name\n\n"
    [row] = parse_squeue(text)
    assert row["job_id"] == "123" and row["state"] == "RUNNING"
    assert row["name"] == "my|odd|name"


def test_squeue_empty_and_malformed():
    assert parse_squeue("") == []
    assert parse_squeue("garbage\n") == []


def test_sacct_rows():
    text = ("42|COMPLETED|0:0|00:01:00|2026-01-01T10:00:00|2026-01-01T10:01:00|cpu|n1|4|8G||job\n"
            "42.batch|COMPLETED|0:0|00:01:00|2026-01-01T10:00:00|2026-01-01T10:01:00||n1|4||1200K|batch\n")
    rows = parse_sacct(text)
    assert [r["jobid"] for r in rows] == ["42", "42.batch"]
    assert rows[1]["maxrss"] == "1200K"


def test_scontrol_oneline():
    line = "JobId=42 JobName=my job JobState=RUNNING Reason=None StdOut=/scratch/a/slurm-42.out NumCPUs=4"
    d = parse_scontrol(line)
    assert d["JobName"] == "my job"
    assert d["StdOut"] == "/scratch/a/slurm-42.out"
    assert d["NumCPUs"] == "4"


def test_find_skips_bad_lines():
    files = parse_find("f\ta.csv\t10\t1700000000.5\nbroken line\nf\tb/c.txt\tNaN\t1\nd\tsub\t4096\t5.0\nx\tweird\t1\t1\n")
    assert [(f.kind, f.rel, f.size, f.mtime) for f in files] == [
        ("f", "a.csv", 10, 1700000000.5), ("d", "sub", 4096, 5.0)]


def test_parse_dir_links():
    from slurm_mcp.parsing import parse_dir_links

    text = "scratch\n/srv/beegfs/scratch/users/b/bob\nweird name\n/srv/x\nevil\n/srv/../etc\nodd\n/srv/a b\n"
    assert parse_dir_links(text) == [("scratch", "/srv/beegfs/scratch/users/b/bob")]
    assert parse_dir_links("") == []


def test_parse_partition_features():
    from slurm_mcp.parsing import parse_partition_features

    text = "shared-gpu*|a,b\nshared-gpu*|c\nprivate|x\nbroken\n"
    assert parse_partition_features(text) == {"shared-gpu": {"a", "b", "c"}, "private": {"x"}}
