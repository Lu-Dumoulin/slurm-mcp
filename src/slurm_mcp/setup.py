"""Interactive creation of the config file (`slurm-mcp init`). Cross-platform, stdlib only."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Config
from .errors import ConfigError


def _ask(prompt: str, input_fn: Callable[[str], str], default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    return input_fn(f"{prompt}{suffix}: ").strip() or (default or "")


def render_toml(data: dict) -> str:
    lines = ["# Created by `slurm-mcp init`. No credentials belong in this file.", ""]
    for key, val in data.items():
        lines.append(f"{key} = {json.dumps(val)}")  # JSON strings and lists are valid TOML here
    return "\n".join(lines) + "\n"


def run_init(
    path: Path | None = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> Path:
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if path.exists() and _ask(f"{path} exists. Overwrite? (y/N)", input_fn).lower() not in ("y", "yes"):
        out("Left the existing config unchanged.")
        return path
    while True:
        data = {
            "host": _ask("Cluster login host (e.g. login1.example.org)", input_fn),
            "user": _ask("Your cluster username", input_fn),
            "remote_roots": [r.strip() for r in _ask(
                "Remote directories the tools may read, absolute, comma-separated", input_fn).split(",") if r.strip()],
            "local_dir": _ask("Local folder for downloaded results", input_fn, "~/slurm-mcp-downloads"),
        }
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
    out("Next: run `slurm-mcp login` in a terminal, then register the server with your client (see README).")
    return path


if __name__ == "__main__":
    run_init()
    sys.exit(0)
