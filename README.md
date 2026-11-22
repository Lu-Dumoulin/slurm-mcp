# slurm-mcp

An MCP server that lets a coding agent inspect and, with your approval, run jobs on a Slurm cluster over SSH.

## In 30 seconds

![A coding agent on your computer talks MCP to slurm-mcp, which runs on the same computer and reaches the Slurm cluster over one shared SSH connection](docs/overview.svg)

- **What it is.** A narrow, safe window onto your cluster for a coding agent. Write code on your laptop, then ask "why did job 123 fail?" or "run this on the debug preset" without pasting terminal output.
- **Where it runs.** On your computer, next to the agent, which starts it and talks to it over MCP (stdio). It reaches the cluster through one shared SSH connection. Nothing is installed on the cluster and no password is stored.
- **Which agents.** Used with Claude Code and Claude Desktop. Other MCP clients that can launch a local stdio server should work but are untested.
- **Safety.** Read-only by default. Submit and cancel are off until you enable them, then need a dry run showing the full script, a one-time token and your approval.

I built this for my lab. We run simulations on a university cluster (UNIGE Baobab) and most of us write code with a coding agent. The agent could not see the cluster, so we pasted `squeue` output and logs into the chat. Each person runs the server on their own machine with their own SSH key and config. The design follows the SSH layer of [CommonUI](https://github.com/Lu-Dumoulin/CommonUI), a Julia tool for running and downloading parameter sweeps: one shared SSH connection, and incremental downloads that compare modification times.

**Status.** Everything is implemented and covered by offline tests. On Baobab I have run the read-only tools (queue, accounting history, directory listing) and `slurm-mcp doctor --smoke`, which generated, submitted and followed a real job from the `smoke` preset (it completed and the diagnosis matched). Cancel has not run against a real cluster yet.

## Install

Python 3.11 or newer, `mcp` 2.x. On macOS or Linux, from the cloned folder:

```bash
./install.sh
```

It creates a virtual environment, installs the package, links `slurm-mcp` into `~/.local/bin` (which must be on your `PATH`), and runs `slurm-mcp init`. On Windows use `install.bat`.

`init` asks for the login host and username, then offers to test the connection (this also opens the shared SSH connection on macOS and Linux). If it works, it proposes your home directory as the default for `remote_roots` and offers directory symlinks found there (on many clusters `~/scratch` is one) as extra roots, shown by real path. It also asks for `local_dir` and an optional default parameter table, validates the answers and writes `~/.config/slurm-mcp/config.toml`. It never overwrites an existing config without asking.

By hand:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
slurm-mcp init          # or copy config.example.toml to ~/.config/slurm-mcp/config.toml
```

The config path can also be set with `SLURM_MCP_CONFIG` or `--config`. Optional switches (see `config.example.toml`): `enable_writes` (default `false`) exposes submit and cancel, and `auto_connect` (default `true`, see below).

### Register with your agent

Claude Code:

```bash
claude mcp add slurm -- /path/to/slurm-mcp/.venv/bin/slurm-mcp
```

Claude Desktop, in `claude_desktop_config.json`:

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

### Shared SSH connection

Clusters often throttle logins, so all calls share one OpenSSH `ControlMaster` connection (`ControlPersist`, default 10 minutes). macOS and Linux only; Windows OpenSSH has no multiplexing.

With `auto_connect = true`, a call that finds no open connection opens one without a prompt. This works when your key is available non-interactively (an unlocked key, `ssh-agent`, or the macOS keychain with `UseKeychain yes`). If a passphrase, password or second factor is needed, the call fails with a message to run `slurm-mcp login`, which the model cannot do for you. With `auto_connect = false` the server only uses a connection you opened yourself.

To open it manually, run this in a terminal on your own computer (not inside the agent, not on the cluster), from any folder:

```bash
slurm-mcp login      # asks for password or passphrase, then returns the prompt
slurm-mcp check      # is the connection alive?
```

If the command is not on your `PATH`, use `/path/to/slurm-mcp/.venv/bin/slurm-mcp`.

### Check that it works

```bash
slurm-mcp doctor
```

Read-only. Checks the config, the SSH connection (no prompt), `squeue`, that each root and each preset partition exists, that the GPU types exist on the GPU presets' partitions, and where generated scripts go. Each line is `OK`, `WARN` or `FAIL` with the reason.

```bash
slurm-mcp doctor --smoke
```

Also runs a real tiny job. It prints the full `smoke` preset script (15 minutes on `debug-cpu`, command `hostname; date; echo smoke-ok`) and asks you to type `y` before submitting. It then follows the job, prints the diagnosis and output, and reports pass or fail. `--preset debug` tests another preset. After 5 minutes in the queue it stops waiting and prints the job id; the job stays queued.

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
| `list_presets` | Presets, defaults, caps, GPU types on offer | nothing |
| `submit_preset_dry_run(preset, gpu, command, ...)` | Generates the full sbatch script from a preset, with cost summary and token | nothing |
| `submit_job_dry_run(script_path)` | Same for a script already on the cluster | nothing |
| `submit_job(confirmation_token)` | Writes the reviewed script if there is one, then runs `sbatch` | submits a job |
| `cancel_job_dry_run(job_id)` | Shows which of your active jobs `scancel` would hit, and a token | nothing |
| `cancel_job(confirmation_token)` | Runs the command from that dry run | cancels jobs |

The last six tools exist only when `enable_writes = true`. Without it the server exposes the read-only tools and `fetch_results`.

## Generated jobs and presets

Typical use: write code on your laptop with an agent, ask for a quick test on the cluster, read why it failed, fix it, run the real job. The agent does not write arbitrary batch files. It picks a **preset** from your config and the server renders the script, as CommonUI does.

A preset fixes partition, time, CPUs, memory and (for GPU presets) the number of GPUs, with **hard caps** (`max_time`, `max_cpus`, `max_mem`, `max_gpus`). Overrides are refused above a cap. `config.example.toml` has five Baobab presets, checked with read-only `sinfo`: `smoke` (15 min on `debug-cpu`, fastest start), `debug` (30 min on `public-short-cpu`), `standard`, `gpu_debug` (30 min on `shared-gpu`; Baobab has no debug GPU partition) and `gpu`. The GPU list, partition list, 12 h default time, 3000 MB default memory and `%40` array throttle come from the CommonUI notebook. CommonUI preselects A100-40Gb and H100; the example preselects all three types.

- **GPU is never assumed.** `gpu` is a required parameter with no default, and the tool description tells the agent to ask you. If you want a GPU it asks which type from your configured list. The dry run echoes the choice, for example `GPU: yes, 1 x A100-40Gb|H100`.
- **GPU availability is checked.** The dry run asks `sinfo` which node features each chosen partition has. With a list such as `private-kruse-gpu,shared-gpu` and an OR constraint, a type missing on one partition only gives a warning (for example "A100-80Gb is not available on private-kruse-gpu"), because Slurm uses whichever partition has a matching node. If no chosen partition has any chosen type, the job could never start and the dry run refuses.
- **The command is free-form but quoted.** A list of arguments, for example `["julia", "--optimize=3", "main.jl"]`, written as one quoted `srun` line, never a shell string. `{task_id}` in an argument becomes the array task id.
- **Everything else is validated.** Job name, partition, time, memory, array spec, module names and GPU types must match strict patterns or the config's lists, so nothing can add an `#SBATCH` line or a second command. Arrays are limited to `max_array_size` tasks and always get a `%N` concurrency limit.
- **You review the whole script.** The dry run returns the script text, its SHA-256, a cost summary (tasks, CPUs, time, memory, partition, GPU) and the sbatch command. The token is tied to that exact script: `submit_job` writes those bytes to `jobs_dir` and runs sbatch on them.

Limit: a job that runs your code is code on the cluster, whatever the wrapper. The controls are your review of the full script, the resource caps, the working directory confined to your roots, and the client's approval prompt on `submit_job`. Getting code onto the cluster is not handled yet (use git or rsync).

## Safety model

- **No shell access.** Each tool maps to a fixed command template (`squeue`, `sacct`, `scontrol show job`, `tail`, `find`, `head`, `scp`). Arguments are shell-quoted. Job ids must be `digits` or `digits_arrayindex`.
- **Path confinement.** Remote paths must be absolute, use a conservative character set (no spaces, globs, quotes, `~` or `..`) and lie inside a configured `remote_root`. This includes log paths that Slurm reports. Symlinks on the cluster are not resolved, so a symlink inside a root that points outside it would be followed: keep roots free of such links.
- **Bounded output.** Tail length, listing size, table size and download volume are capped by config.
- **Local writes are confined.** `fetch_results` writes below `local_dir`, mirrors the remote path, uses a `.part` file first, and refuses downloads above `max_download_mb`. Use `dry_run=true` to preview.
- **Tool annotations.** Every tool declares `readOnlyHint`, so clients can skip prompts for the read-only ones. `submit_job` and `cancel_job` are not read-only and `cancel_job` is destructive, so clients ask you first.
- **Writes are off by default.** Submit and cancel are not registered without `enable_writes = true`, and the cluster-facing code refuses them too.
- **Two steps with a token.** A dry run stores the exact remote command and returns a random single-use token that expires (default 5 minutes). The real call takes only the token, so what runs is what was shown. Tokens live in memory; restarting the server invalidates them.
- **Narrow write scope.** Submission is `sbatch --parsable` of one regular file inside a root (existing, or generated from a preset), run from its directory or the job's working directory, with no extra options. Cancellation is `scancel` of a validated job id that is in your own `squeue --me`. Cancelling a plain id cancels a whole array; use `123_4` for one task. No other command can be passed.
- **What the token does not prove.** It proves the executed command equals the reviewed one, not that a person approved it: a model could call both steps in a row. The human check is your client's approval prompt, so do not set `submit_job` or `cancel_job` to always-allow. A script that already exists on the cluster is only previewed (first 2000 bytes).
- **No credentials in the repo or config.** Authentication is your normal `~/.ssh` setup. `ssh` runs with `BatchMode=yes`, so it never waits at a password prompt.

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
> **Claude** calls `fetch_results(directory="/home/me/scratch/sweep_a", pattern="*.csv", dry_run=true)`, reports 14 new files (2.1 MB), then repeats without `dry_run`. A later call downloads only changed files.

## Tests

```bash
pytest -q
```

No test needs a cluster or SSH keys. The SSH layer sits behind a `Runner` interface; tests use a scripted `FakeRunner` (`tests/fakes.py`) and, for one end-to-end test, a stub `ssh` that the real server process calls over MCP stdio. Parsing, path validation, sync planning, script generation and diagnosis are pure functions with their own tests, including golden tests of the generated scripts. CI runs Python 3.11 to 3.13.

Not covered: a real Slurm installation. `squeue`, `sacct` and `scontrol` formats differ slightly between Slurm versions, and `find -printf` needs GNU find on the cluster. Check against your own cluster before relying on it.

## Layout

```
src/slurm_mcp/
  config.py    TOML config, validated
  paths.py     remote path validation
  runner.py    Runner protocol and subprocess implementation
  commands.py  pure ssh/scp argument vectors and fixed remote commands
  parsing.py   pure parsers for squeue, sacct, scontrol, sinfo, find
  sync.py      pure incremental download planning
  presets.py   presets, caps and unit parsing
  jobscript.py pure sbatch script generation
  diagnose.py  pure reading of job accounting data
  doctor.py    setup check and smoke test (slurm-mcp doctor)
  confirm.py   single-use confirmation tokens
  cluster.py   cluster operations (read-only, plus submit and cancel when enabled)
  server.py    MCP tool definitions
  setup.py     interactive config creation (init)
  __main__.py  serve, login, check, init, doctor
tests/
docs/overview.svg
```

## Roadmap

- Run cancel once against a real cluster.
- Upload of code files with the same dry run and token pattern.
- One array task per row of a parameter table such as `DF.csv`, as in CommonUI.
- Record real `squeue`, `sacct` and `scontrol` output from a job in the tests.
- Optional `sbatch` options from a short whitelist, if needed.

## License

MIT.
