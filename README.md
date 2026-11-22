# slurm-mcp

An MCP server that lets Claude (Claude Code, Claude Desktop) inspect jobs on a Slurm
cluster over SSH. It is read-only on the cluster. Job submission and cancellation are
planned as phase 2 and will require an explicit confirmation step.

I work on simulations that run on a university cluster (UNIGE Baobab) and wanted an
assistant that can answer "did my sweep finish, and what does the log say?" without me
pasting terminal output. The design follows the SSH layer of
[CommonUI](https://github.com/Lu-Dumoulin/CommonUI), a Julia tool I use to run and download parameter sweeps:
one shared SSH connection, and incremental downloads that compare modification times.

## Status

Phase 1 (read-only) is implemented and tested. Phase 2 (submit and cancel behind a
confirmation token) is not implemented yet.

## Tools

| Tool | What it does | Writes |
|---|---|---|
| `queue` | Your pending and running jobs (`squeue --me`) | nothing |
| `recent_jobs(days)` | Your jobs from the last 1 to 30 days (`sacct`) | nothing |
| `job_status(job_id)` | `sacct` rows plus whitelisted `scontrol` fields | nothing |
| `job_log(job_id, stream, lines)` | Tail of a job's stdout or stderr | nothing |
| `tail_file(path, lines)` | Tail of a file under a configured root | nothing |
| `list_files(directory, max_depth, pattern)` | Path, size and mtime of remote files | nothing |
| `read_parameter_table(path)` | A CSV such as `sim/DF.csv`, as rows | nothing |
| `fetch_results(directory, pattern, dry_run)` | Copies missing or newer files locally | local disk only |

## Safety model

- **No shell access.** Each tool maps to a fixed command template (`squeue`, `sacct`,
  `scontrol show job`, `tail`, `find`, `head`, `scp`). Arguments are shell-quoted. Job ids
  must match `digits` or `digits_arrayindex`.
- **Path confinement.** Every remote path must be absolute, use a conservative character
  set (no spaces, globs, quotes, `~` or `..`), and lie inside one of the `remote_roots`
  from your config. This also applies to log paths that Slurm reports for a job.
  Limitation: symlinks on the cluster are not resolved, so a symlink inside a root that
  points outside it would be followed. Keep roots free of such links.
- **Bounded output.** Tail length, listing size, table size and download volume are capped
  by config values.
- **Local writes are confined.** `fetch_results` writes below `local_dir` only, mirrors the
  remote path, writes to a `.part` file first, and refuses a download above
  `max_download_mb`. Use `dry_run=true` to preview.
- **Tool annotations.** Every tool declares `readOnlyHint`; clients that honour it can
  skip confirmation prompts for the read-only ones.
- **No credentials in the repo or the config.** Authentication is your normal `~/.ssh`
  setup. The server runs `ssh` with `BatchMode=yes`, so it can never sit on a password
  prompt.

## One shared connection

Clusters often throttle logins. All calls go through one OpenSSH `ControlMaster`
connection (`ControlPersist`, default 10 minutes). Open it once in a terminal, which is
also where a password or second factor is entered.

Run it in a normal terminal on your own computer (Terminal or iTerm on macOS), not
inside Claude and not on the cluster. The virtual environment must be active, or use
the full path `/path/to/slurm-mcp/.venv/bin/slurm-mcp login`:

```bash
slurm-mcp login
```

It asks for your password or key passphrase, then returns the prompt while the
connection stays open in the background.

`slurm-mcp check` reports whether the shared connection is alive. If it has expired, tools
return a message telling the model to ask you to run `slurm-mcp login`. This is macOS and
Linux only, since Windows OpenSSH does not support multiplexing.

## Install and configure

Python 3.11 or newer. The SDK is `mcp` 2.x (`MCPServer`, which replaced `FastMCP`).

On macOS or Linux, from the folder where you cloned the repository:

```bash
./install.sh
```

This creates a virtual environment, installs the package, and runs `slurm-mcp init`,
which asks for the login host, your username, the remote directories the tools may read
(`remote_roots`), the local download folder (`local_dir`) and an optional default
parameter table. It validates the answers and writes `~/.config/slurm-mcp/config.toml`.
It never overwrites an existing config without asking. On Windows use `install.bat`.

By hand instead:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
slurm-mcp init          # or copy config.example.toml to ~/.config/slurm-mcp/config.toml
```

The config location can also be set with `SLURM_MCP_CONFIG` or `--config`. Nothing
cluster-specific is hard-coded.

### Claude Code

```bash
claude mcp add slurm -- /path/to/slurm-mcp/.venv/bin/slurm-mcp
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "slurm": {
      "command": "/path/to/slurm-mcp/.venv/bin/slurm-mcp",
      "args": ["--config", "/Users/you/.config/slurm-mcp/config.toml"]
    }
  }
}
```

## Example session

Illustrative, not a recorded run.

> **You:** Is my sweep still running?
>
> **Claude** calls `queue`: job 4812345 `sweep_a`, RUNNING, 3:12:40 of 12:00:00, node cpu042.
>
> **You:** Show the end of its log.
>
> **Claude** calls `job_log(job_id="4812345", stream="out", lines=20)` and summarizes it.
>
> **You:** Get the new CSV files from the output folder.
>
> **Claude** calls `fetch_results(directory="/home/me/scratch/sweep_a", pattern="*.csv", dry_run=true)`,
> reports 14 new files (2.1 MB), then repeats without `dry_run`. A later call downloads
> only files that changed.

## Tests

```bash
pytest -q
```

No test needs a cluster or SSH keys. The SSH layer sits behind a `Runner` interface; the
suite uses a scripted `FakeRunner` (`tests/fakes.py`) and, for one end-to-end test, a stub
`ssh` script that the real server process calls over MCP stdio. Parsing, path validation
and sync planning are pure functions with their own tests. CI runs on Python 3.11 to 3.13.

What the tests do not cover: behaviour against a real Slurm installation. Output formats
of `squeue`, `sacct` and `scontrol` differ slightly between Slurm versions, and `find
-printf` requires GNU find on the cluster. Check against your own cluster before relying
on it.

## Layout

```
src/slurm_mcp/
  config.py    TOML config, validated
  paths.py     remote path validation
  runner.py    Runner protocol and subprocess implementation
  commands.py  pure ssh/scp argument vectors and fixed remote commands
  parsing.py   pure parsers for squeue, sacct, scontrol, find
  sync.py      pure incremental download planning
  cluster.py   read-only operations
  server.py    MCP tool definitions
  setup.py     interactive config creation (init)
  __main__.py  serve, login, check, init
tests/
```

## Roadmap

- Phase 2: `submit_dry_run` returns the exact `sbatch` command and a short-lived token;
  `submit` runs only with that token. `cancel` follows the same pattern. Only existing
  scripts under a root can be submitted.
- Compare live output formats on Baobab and record them in the tests.

## License

MIT.
