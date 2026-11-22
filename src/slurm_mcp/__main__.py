"""Entry point: `slurm-mcp` (serve over stdio), `slurm-mcp login`, `slurm-mcp check`."""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import commands
from .cluster import Cluster
from .config import load_config
from .errors import SlurmMcpError
from .runner import SubprocessRunner
from .server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slurm-mcp", description=__doc__)
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "login", "check"])
    parser.add_argument("--config", help="path to config.toml (default: $SLURM_MCP_CONFIG or ~/.config/slurm-mcp/config.toml)")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except SlurmMcpError as exc:
        print(f"slurm-mcp: {exc}", file=sys.stderr)
        return 2
    cfg.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if args.command == "login":
        return subprocess.call(commands.login_argv(cfg))
    if args.command == "check":
        return subprocess.call(commands.check_argv(cfg))
    build_server(Cluster(cfg, SubprocessRunner())).run()  # stdio; stdout carries the protocol
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
