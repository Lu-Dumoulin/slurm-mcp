"""Entry point: `slurm-mcp` serves over stdio; also `login`, `check`, `init` and `doctor`."""

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
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "login", "check", "init", "doctor"])
    parser.add_argument("--config", help="path to config.toml (default: $SLURM_MCP_CONFIG or ~/.config/slurm-mcp/config.toml)")
    parser.add_argument("--smoke", action="store_true", help="doctor: also submit a tiny test job (asks first)")
    parser.add_argument("--preset", default="smoke", help="doctor --smoke: preset to use (default: smoke)")
    args = parser.parse_args(argv)
    if args.command == "init":
        from pathlib import Path
        from .setup import run_init

        run_init(Path(args.config) if args.config else None)
        return 0
    try:
        cfg = load_config(args.config)
    except SlurmMcpError as exc:
        print(f"slurm-mcp: {exc}", file=sys.stderr)
        return 2
    if cfg.use_shared_connection:
        cfg.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.control_dir.chmod(0o700)    # also fixes a folder created earlier with looser permissions

    if args.command == "doctor":
        from .doctor import run_doctor

        return run_doctor(cfg, SubprocessRunner(), smoke=args.smoke, preset=args.preset)
    if args.command in ("login", "check") and not cfg.use_shared_connection:
        print("Shared SSH connections are off on this system (ssh_multiplexing = "
              f"{cfg.ssh_multiplexing}). Each call opens its own connection, so nothing stays open.\n"
              "Testing one connection now: it must log in without a prompt (use ssh-agent and ssh-add).",
              file=sys.stderr)
    if args.command == "login":
        return subprocess.call(commands.login_argv(cfg))
    if args.command == "check":
        return subprocess.call(commands.check_argv(cfg))
    build_server(Cluster(cfg, SubprocessRunner())).run()  # stdio; stdout carries the protocol
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
