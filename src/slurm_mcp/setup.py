"""Interactive creation of the config file (`slurm-mcp init`). Cross-platform, stdlib only."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import commands
from .config import DEFAULT_CONFIG_PATH, Config
from .errors import ConfigError, RemoteError
from .parsing import parse_dir_links
from .paths import SAFE_PATH_RE
from .runner import SubprocessRunner

Probe = Callable[[str, str], str | None]


def _ask(prompt: str, input_fn: Callable[[str], str], default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    return input_fn(f"{prompt}{suffix}: ").strip() or (default or "")


def probe_cluster(host: str, user: str) -> str | None:
    """Log in once, interactively, and return the remote home directory (None on failure).

    Password and second-factor prompts reach the user's terminal. On macOS and Linux the
    ControlMaster options leave the shared connection open afterwards, so the first real
    tool call does not need another login.
    """
    cfg = Config(host=host, user=user, remote_roots=("/",), local_dir=Path("."))
    cfg.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = ["ssh", *commands.ssh_options(cfg, batch=False), commands.target(cfg), "pwd"]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, text=True, timeout=180, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    home = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return home if proc.returncode == 0 and SAFE_PATH_RE.fullmatch(home) else None


def _ask_connection(
    input_fn: Callable[[str], str], out: Callable[[str], None], probe: Probe,
    host: str | None = None, user: str | None = None,
) -> tuple[str, str, str | None]:
    """Ask host and username, optionally test them, and let the user correct them on failure."""
    while True:
        host = _ask("Cluster login host (e.g. login1.example.org)", input_fn, host)
        user = _ask("Your cluster username", input_fn, user)
        if _ask("Test the connection now? (Y/n)", input_fn, "y").lower() not in ("y", "yes"):
            return host, user, None
        out("Connecting (enter your password or second factor if asked)...")
        home = probe(host, user)
        if home:
            out(f"Connection works. Your home directory is {home}")
            return host, user, home
        out("Could not connect or read the home directory.")
        if _ask("Change the host or username and try again? (Y/n)", input_fn, "y").lower() not in ("y", "yes"):
            return host, user, None


def discover_links(host: str, user: str, home: str) -> list[tuple[str, str]]:
    """Directory symlinks in the home folder (such as ~/scratch), resolved to real paths."""
    cfg = Config(host=host, user=user, remote_roots=("/",), local_dir=Path("."))
    try:
        res = SubprocessRunner().run(commands.ssh_argv(cfg, commands.dir_links_cmd(home)), 30)
    except RemoteError:
        return []
    return parse_dir_links(res.stdout) if res.returncode == 0 else []


def render_toml(data: dict) -> str:
    lines = ["# Created by `slurm-mcp init`. No credentials belong in this file.", ""]
    for key, val in data.items():
        lines.append(f"{key} = {json.dumps(val)}")  # JSON strings and lists are valid TOML here
    return "\n".join(lines) + "\n"


def run_init(
    path: Path | None = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    probe: Probe = probe_cluster,
    discover: Callable[[str, str, str], list[tuple[str, str]]] = discover_links,
) -> Path:
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if path.exists() and _ask(f"{path} exists. Overwrite? (y/N)", input_fn).lower() not in ("y", "yes"):
        out("Left the existing config unchanged.")
        return path
    host = user = None
    while True:
        host, user, home = _ask_connection(input_fn, out, probe, host, user)
        suggested = [home] if home else []
        for name, target in (discover(host, user, home) if home else []):
            if target != home and _ask(f"Found ~/{name} -> {target}. Also allow it? (y/N)", input_fn).lower() in ("y", "yes"):
                suggested.append(target)
        out("\nThe tools will only be allowed to read inside these remote folders (and below them).")
        if suggested:
            out("Suggested folders:")
            for r in suggested:
                out(f"  {r}")
            answer = input_fn("Press Enter to keep them, or type your own list (full paths separated by commas): ").strip()
            roots_text = answer or ", ".join(suggested)
        else:
            roots_text = input_fn("Folders (full paths separated by commas, e.g. /home/bob/sims): ").strip()
        data = {
            "host": host,
            "user": user,
            "remote_roots": [r.strip() for r in roots_text.split(",") if r.strip()],
            "local_dir": _ask("Local folder for downloaded results", input_fn, "~/slurm-mcp-downloads"),
        }
        upload = _ask("Extra local folders the agent may upload code from, besides the folder it is "
                      "started in (comma-separated, Enter to skip, e.g. ~/Documents/GitHub)", input_fn)
        if upload:
            data["local_roots"] = [p.strip() for p in upload.split(",") if p.strip()]
        df = _ask("Default parameter table path on the cluster (optional, Enter to skip)", input_fn)
        if df:
            data["df_csv"] = df
        try:
            Config.from_dict(data)
            break
        except ConfigError as exc:
            out(f"Invalid: {exc}. Please try again.\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_toml(data))
    out(f"\nWrote {path}")
    out(f"Next: run `{commands.cli_name()} login` in a terminal, then register the server with your client (see README).")
    return path


if __name__ == "__main__":
    run_init()
    sys.exit(0)
