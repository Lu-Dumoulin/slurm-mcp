"""Starts the real server as a subprocess and talks MCP to it over stdio.
`ssh` is replaced by a stub script on PATH, so still no cluster is needed."""

import asyncio
import os
import stat
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")

STUB = """#!/bin/sh
for a; do last="$a"; done
case "$last" in
  squeue*) echo "7|RUNNING|cpu|1:00|1:00:00|1|n1|2026-01-01T00:00:00|demo" ;;
  *) echo "stub: unexpected: $last" >&2; exit 1 ;;
esac
"""


def test_server_over_stdio(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(STUB)
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    conf = tmp_path / "config.toml"
    conf.write_text(
        'host="h.example.org"\nuser="bob"\nremote_roots=["/s/bob"]\n'
        f'local_dir="{tmp_path}/dl"\ncontrol_dir="{tmp_path}/cm"\n'
    )
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "slurm_mcp", "--config", str(conf)], env=env)

    async def scenario():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            ok = await session.call_tool("queue", {})
            bad = await session.call_tool("tail_file", {"path": "/etc/passwd"})
            return names, ok, bad

    names, ok, bad = asyncio.run(scenario())
    assert "queue" in names
    assert not ok.is_error and "RUNNING" in ok.content[0].text
    assert bad.is_error and "outside the configured roots" in bad.content[0].text
