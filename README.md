# slurm-mcp

An MCP server that lets a coding agent inspect and, with your approval, run jobs on a Slurm
cluster over SSH.

## In 30 seconds

![A coding agent on your computer talks MCP to slurm-mcp, which runs on the same computer and reaches the Slurm cluster over one shared SSH connection](docs/overview.svg)

- **What it is.** A narrow, safe window onto your Slurm cluster for a coding agent. You write
  code on your laptop or workstation, then ask things like "why did job 123 fail?" or "run
  this on the debug preset", without pasting terminal output.
- **Where the MCP server stands.** On your own computer, next to the agent. The agent starts
  it and talks to it over MCP (stdio). It reaches the cluster through one shared SSH
  connection, so nothing is installed on the cluster and no password is stored.
- **Which agents.** Used with Claude Code and Claude Desktop. Any other MCP client that can
  launch a local stdio server should work, but I have not tested those.
- **Safety.** Read-only by default. Submitting or cancelling is switched off until you enable
  it, and then needs a dry run that shows the full script, a one-time token, and your approval.

I built this for my lab. We run simulations on a university cluster (UNIGE Baobab), and most
of us now write code with a coding agent. The agent can edit the code but could not see the
cluster, so we ended up pasting `squeue` output and logs back into the chat. This server lets
the agent answer "did my sweep finish, and what does the log say?" itself, and run a test job
when you approve it. Each person runs it on their own machine with their own SSH key and
config. The design follows the SSH layer of
[CommonUI](https://github.com/Lu-Dumoulin/CommonUI), a Julia tool I use to run and download parameter sweeps:
one shared SSH connection, and incremental downloads that compare modification times.

## Status

Phase 1 (read-only tools) and phase 2 (submit and cancel behind a confirmation token) are
implemented and covered by offline tests. Phase 1 has been run against a real cluster
(Baobab) for the queue, accounting history and directory listing. Phase 2 has not been run
against a real cluster yet.

## Tools

| Tool | What it does | Writes |
|---|---|---|
| `queue` | Your pending and running jobs (`squeue --me`) | nothing |
| `recent_jobs(days)` | Your jobs from the last 1 to 30 days (`sacct`) | nothing |
| `job_status(job_id)` | `sacct` rows plus whitelisted `scontrol` fields | nothing |
| `job_log(job_id, stream, lines)` | Tail of a job's stdout or stderr | nothing |
| `diagnose_job(job_id)` | Verdict (timeout, out of memory, exit code, pending reason, memory used versus requested) plus the end of stderr and stdout | nothing |
| `tail_file(path, lines)` | Tail of a file under a configured root | nothing |
| `list_files(directory, max_depth, pattern)` | Files and subdirectories with type, size and mtime | nothing |
| `read_parameter_table(path)` | A CSV such as `sim/DF.csv`, as rows | nothing |
| `fetch_results(directory, pattern, dry_run)` | Copies missing or newer files locally | local disk only |
| `list_presets` | Presets, their defaults and caps, the GPU types on offer | nothing |
| `submit_preset_dry_run(preset, gpu, command, ...)` | Generates a full sbatch script from a preset and returns it with a cost summary and a token | nothing |
| `submit_job_dry_run(script_path)` | Same idea for a script that already exists on the cluster | nothing |
| `submit_job(confirmation_token)` | Writes the reviewed script if there is one, then runs `sbatch` | submits a job |
| `cancel_job_dry_run(job_id)` | Shows which of your own active jobs `scancel` would hit, and a token | nothing |
| `cancel_job(confirmation_token)` | Runs the command from that dry run | cancels jobs |

The job tools only exist when `enable_writes = true` is set in the config. Without it
the server exposes only the read-only tools above (and `fetch_results`, which writes to
local disk only).

## Generated jobs and presets

Typical use: you write code on your laptop with a coding agent, then ask Claude to run a
quick test on the cluster, read why it failed, fix it and run the real job. For that,
Claude does not write arbitrary batch files. It picks a **preset** from your config and the
server renders the script, in the way CommonUI does.

A preset fixes partition, time, CPUs, memory and, for GPU presets, the number of GPUs, and
carries **hard caps** (`max_time`, `max_cpus`, `max_mem`, `max_gpus`). Claude may override a
value only up to its cap. `config.example.toml` has five presets for Baobab, checked with
read-only `sinfo`: `smoke` (15 min on `debug-cpu`, the fastest start), `debug` (30 min on
`public-short-cpu`), `standard`, `gpu_debug` (30 min on `shared-gpu`, since Baobab has no
debug GPU partition) and `gpu`. The GPU list, the
partition list, the 12 h default time, the 3000 MB default memory and the `%40` array
throttle are taken from the CommonUI notebook. CommonUI preselects A100-40Gb and H100; the
example preselects all three types, see the availability check below.

- **GPU is never assumed.** `gpu` is a required parameter with no default, and the tool
  description tells Claude to ask you if you have not said. If you want a GPU, Claude asks
  which type from your configured list (`list_presets` returns it). The dry run echoes the
  choice, for example `GPU: yes, 1 x A100-40Gb|H100`.
- **GPU availability is checked.** The dry run asks `sinfo` (read-only) which node features
  each chosen partition has. A partition list such as `private-kruse-gpu,shared-gpu` with an
  OR constraint is fine when only some partitions have every type: Slurm uses whichever
  partition has a matching node, and the dry run lists a warning such as "A100-80Gb is not
  available on private-kruse-gpu". If no chosen partition has any chosen type, the job could
  never start, so the dry run refuses with an error.
- **The command is free-form but quoted.** It is a list of arguments, for example
  `["julia", "--optimize=3", "main.jl"]`, written as one quoted `srun` line, never passed
  through a shell string. `{task_id}` inside an argument becomes the array task id.
- **Everything else is validated.** Job name, partition, time, memory, array spec, module
  names and GPU types must match strict patterns or the config's lists, so nothing can add an
  `#SBATCH` line or a second command. Arrays are limited to `max_array_size` tasks and always
  get a `%N` concurrency limit.
- **You review the whole script.** The dry run returns the full script text, its SHA-256, a
  cost summary (tasks, CPUs, time, memory, partition, GPU) and the sbatch command. The token
  is tied to that exact script: `submit_job` writes those bytes to `jobs_dir` and runs sbatch
  on them, and nothing else.

Honest limit: a job that runs your code is code running on the cluster, whatever the
wrapper. The controls are that you see the full script before approving, the resource
caps, the confinement of the working directory to your roots, and the client's approval
prompt on `submit_job`. Getting code onto the cluster is not handled by this server yet
(use git or rsync).

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
  skip confirmation prompts for the read-only ones. `submit_job` and `cancel_job` are not
  read-only, and `cancel_job` is marked destructive, so a client asks you before running them.
- **Writes are off by default.** Submit and cancel are not even registered unless
  `enable_writes = true`. The cluster-facing code refuses them as well, in case a client
  calls a tool that was not listed.
- **Two steps with a token.** A dry run validates the request, stores the exact remote
  command and returns a random single-use token that expires (default 5 minutes). The real
  call takes only the token, not the parameters, so what runs is exactly what was shown.
  Tokens live in memory, so restarting the server invalidates them.
- **Narrow write scope.** Submission is `sbatch --parsable` of one existing regular file
  inside a configured root, run from that file's directory, with no extra options.
  Cancellation is `scancel` of a validated job id, and only if that job is in your own
  `squeue --me` listing. Cancelling a plain id cancels a whole job array; use `123_4` for
  one task. There is no way to pass any other command.
- **What the token does not prove.** It guarantees that the command executed equals the one
  reviewed. It does not prove that a person approved it: a model could call both steps in a
  row. The human check is your client's approval prompt on `submit_job` and `cancel_job`,
  so keep that prompt on (do not set those tools to always-allow). The submitted script
  itself is not inspected beyond a preview of its first 2000 bytes.
- **No credentials in the repo or the config.** Authentication is your normal `~/.ssh`
  setup. The server runs `ssh` with `BatchMode=yes`, so it can never sit on a password
  prompt.

## Install and configure

Python 3.11 or newer. The SDK is `mcp` 2.x (`MCPServer`, which replaced `FastMCP`).

On macOS or Linux, from the folder where you cloned the repository:

```bash
./install.sh
```

This creates a virtual environment, installs the package, and runs `slurm-mcp init`,
which asks for the login host and your username, then offers to test the
connection. If it works, `init` reports your remote home directory and proposes it as the
default for `remote_roots`. It also looks for directory symlinks in your home folder
(on many clusters `~/scratch` is one), shows the real target of each, and asks whether to
allow it as an additional root. Roots should be real paths, since symlinks are not
resolved at run time. It asks for the remote directories the tools may read
(`remote_roots`), the local download folder (`local_dir`) and an optional default
parameter table. It validates the answers and writes `~/.config/slurm-mcp/config.toml`.
The test login also opens the shared SSH connection on macOS and Linux (see below). It never overwrites an existing config without asking. On Windows use `install.bat`.

By hand instead:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
slurm-mcp init          # or copy config.example.toml to ~/.config/slurm-mcp/config.toml
```

The config location can also be set with `SLURM_MCP_CONFIG` or `--config`. Nothing
cluster-specific is hard-coded. Two optional switches, both in `config.example.toml`:

- `enable_writes = false` (default). Set it to `true` to expose submit and cancel.
- `auto_connect = true` (default). See the next section.

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

## One shared connection

Clusters often throttle logins. All calls go through one OpenSSH `ControlMaster`
connection (`ControlPersist`, default 10 minutes). Open it once in a terminal, which is
also where a password or second factor is entered.

Run it in a normal terminal on your own computer (Terminal or iTerm on macOS), not
inside Claude and not on the cluster, from any folder. `./install.sh` links the command
into `~/.local/bin`, which must be on your `PATH` (the script tells you if it is not). Without
that link, use the full path `/path/to/slurm-mcp/.venv/bin/slurm-mcp login`:

```bash
slurm-mcp login
```

It asks for your password or key passphrase, then returns the prompt while the
connection stays open in the background.

`slurm-mcp check` reports whether the shared connection is alive.

**Automatic connection.** With `auto_connect = true` (the default) a tool call that finds no
open connection tries to open one by itself, without any prompt. This works when your key is
available non-interactively (an unlocked key, `ssh-agent`, or the macOS keychain with
`UseKeychain yes`). If a passphrase, password or second factor is needed, the call fails with
a message telling you to run `slurm-mcp login`, which the model cannot do for you. With
`auto_connect = false` the server only ever uses a connection you opened yourself. This is macOS and
Linux only, since Windows OpenSSH does not support multiplexing.

## Check that everything works

```bash
slurm-mcp doctor
```

reads only. It checks the config, the SSH connection (without a prompt), `squeue`, that each
root exists, that every preset's partition exists, that the chosen GPU types exist on the
GPU presets' partitions, and where generated scripts will go. Each line is `OK`, `WARN` or
`FAIL` with the reason.

```bash
slurm-mcp doctor --smoke
```

also runs a real test job. It generates the `smoke` preset script (15 minutes on
`debug-cpu`, command `hostname; date; echo smoke-ok`), prints it in full, and asks you to type
`y` before it submits anything. It then follows the job until it finishes, prints the
diagnosis and the program output, and reports pass or fail. Use `--preset debug` to test
another preset. If the job is still queued after 5 minutes it stops waiting and tells you
the job id; the job stays in the queue.

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
  presets.py   presets, caps and unit parsing
  jobscript.py pure sbatch script generation
  diagnose.py  pure reading of job accounting data
  doctor.py    setup check and optional smoke test (slurm-mcp doctor)
  confirm.py   single-use confirmation tokens for writes
  cluster.py   cluster operations (read-only, plus submit and cancel when enabled)
  server.py    MCP tool definitions
  setup.py     interactive config creation (init)
  __main__.py  serve, login, check, init
tests/
```

## Roadmap

- Run submit and cancel once against a real cluster with a trivial test script.
- Upload of code files with the same dry run and token pattern.
- One array task per row of a parameter table such as `DF.csv`, as in CommonUI.
- Record real `squeue`, `sacct` and `scontrol` output from a job in the tests.
- Optional `sbatch` options (partition, time) from a short whitelist, if needed.

## License

MIT.
