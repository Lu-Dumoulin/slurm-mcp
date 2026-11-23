# slurm-mcp

An MCP server that lets a coding agent inspect and, with your approval, run jobs on a Slurm cluster over SSH.

## In 30 seconds

![A coding agent on your computer talks MCP to slurm-mcp, which runs on the same computer and reaches the Slurm cluster over one shared SSH connection](docs/overview.svg)

- **What it is.** A narrow, safe window onto your cluster for a coding agent. Write code on your laptop, then ask "why did job 123 fail?" or "run this on the debug preset" without pasting terminal output.
- **Where it runs.** On your computer, started by the agent over MCP (stdio). It reaches the cluster through one shared SSH connection. Nothing is installed on the cluster and no password is stored.
- **Which agents.** Used with Claude Code and Claude Desktop. Other MCP clients that start local stdio servers should work but are untested.
- **Safety.** Read-only by default. Changes (submit, cancel, upload, delete) are off until you enable them, then need a dry run, a one-time token and your approval.

I built this mainly for my lab, the Kruse group at the University of Geneva. We run simulations on the UNIGE Baobab cluster and most of us write code with a coding agent. The agent could not see the cluster, so we pasted `squeue` output and logs into the chat. Each person runs the server on their own machine with their own SSH key and config. The design follows the SSH layer of [CommonUI](https://github.com/Lu-Dumoulin/CommonUI), a Julia tool for parameter sweeps.

**Status.** Covered by offline tests. Run on Baobab so far: the read-only tools, `slurm-mcp doctor --smoke` (a real job from the `smoke` preset, followed to the end), and cancelling a pending job. Upload, sweeps from a table, delete, hold and the other newer tools have offline tests only.

## Install

Python 3.11 or newer, `mcp` 2.x. On macOS or Linux, from the cloned folder:

```bash
./install.sh
```

It creates a virtual environment, installs the package, links `slurm-mcp` into `~/.local/bin` (put it on your `PATH`) and runs `slurm-mcp init`. On Windows use `install.bat`.

`init` asks for the login host and username, offers to test the connection (this also opens the shared SSH connection), and proposes your home directory and any directory symlinks in it (on many clusters `~/scratch` is one) as `remote_roots`. It then asks for `local_dir` and an optional parameter table, validates the answers and writes `~/.config/slurm-mcp/config.toml`. It never overwrites an existing config without asking.

By hand:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
slurm-mcp init          # or copy config.example.toml to ~/.config/slurm-mcp/config.toml
```

Set the config path with `SLURM_MCP_CONFIG` or `--config`. Main options (see `config.example.toml`): `enable_writes` (default `false`) turns on changes, `local_roots` adds upload folders, `auto_connect` (default `true`).

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

### Keep the agent on slurm-mcp

The agent should reach the cluster only through these tools, so every action passes the checks in "Safety model". The server says so in its instructions. To enforce it in Claude Code, add rules to `.claude/settings.json` (one project) or `~/.claude/settings.json` (all projects):

```json
{
  "permissions": {
    "deny": ["Bash(ssh *)", "Bash(scp *)", "Bash(rsync *)", "Bash(sftp *)"],
    "ask": [
      "mcp__slurm__submit_job", "mcp__slurm__upload_code", "mcp__slurm__delete",
      "mcp__slurm__cancel_job", "mcp__slurm__cancel_jobs", "mcp__slurm__hold_job", "mcp__slurm__release_job",
      "Edit(~/.config/slurm-mcp/**)",
      "Edit(~/Library/Application Support/Claude/claude_desktop_config.json)"
    ]
  }
}
```

- Rules are checked deny, ask, then allow; an ask rule prompts even when an allow rule matches. In auto mode a classifier approves for you, so keep the ask rules there too.
- `Bash(ssh *)` is the current syntax (`Bash(ssh:*)` is an older equivalent). slurm-mcp itself still uses `ssh` and `scp`: it runs outside the agent's Bash tool.
- A deny rule does not match a full path or `sh -c`, and `Edit(...)` rules do not cover shell redirects such as `echo >> file`. For a hard guarantee, also restrict the agent's sandbox.
- Rules are per agent; other agents have their own settings.
- Optional line for `CLAUDE.md` or `AGENTS.md`: "Use the slurm MCP tools for everything on the cluster; never run ssh, scp or rsync yourself."

### Shared SSH connection

Clusters often throttle logins, so all calls share one OpenSSH `ControlMaster` connection (`ControlPersist`, default 10 minutes). macOS and Linux only: Windows OpenSSH has no multiplexing.

With `auto_connect = true`, a call with no open connection opens one without a prompt if your key is available non-interactively (unlocked key, `ssh-agent`, or macOS keychain with `UseKeychain yes`). If a passphrase, password or second factor is needed, the call fails and tells you to run `slurm-mcp login`, which the model cannot do for you. With `auto_connect = false` only a connection you opened yourself is used.

Open it by hand in a terminal on your computer (not in the agent, not on the cluster), from any folder:

```bash
slurm-mcp login      # asks for password or passphrase, then returns the prompt
slurm-mcp check      # is the connection alive?
```

If `slurm-mcp` is not on your `PATH`, use `/path/to/slurm-mcp/.venv/bin/slurm-mcp`.

### Check that it works

```bash
slurm-mcp doctor
```

Read-only. Checks the config, the SSH connection (no prompt), `squeue`, each root and preset partition, GPU types, upload folders and the jobs folder. Each line is `OK`, `WARN` or `FAIL` with the reason.

```bash
slurm-mcp doctor --smoke
```

Also runs a real tiny job: it prints the full `smoke` script (15 minutes on `debug-cpu`), asks you to type `y`, follows the job, prints the diagnosis and output, and reports pass or fail. `--preset debug` tests another preset. After 5 minutes in the queue it stops waiting and prints the job id; the job stays queued.

## Example session

Recorded on Baobab, 22 November 2026. Output is shortened. `<user>` replaces the username and `<u>` is its first letter (home folders are `/home/users/<u>/<user>`).

> **You:** Run a smoke test on Baobab.
>
> **Claude** calls `list_presets`, then `submit_preset_dry_run(preset="smoke", gpu=false, command=["bash", "-c", "hostname; date; echo smoke-ok"], workdir="/home/users/<u>/<user>/slurm-mcp-jobs", job_name="slurm-mcp-smoke")`. Nothing is submitted yet. It shows you the summary, the full script and the command:
>
> ```
> 1 task(s), 1 CPU(s) each, up to 00:15:00 each, 2G each, partition debug-cpu, GPU: no
>
> #!/bin/bash
> #SBATCH --job-name=slurm-mcp-smoke
> #SBATCH --partition=debug-cpu
> #SBATCH --time=00:15:00
> #SBATCH --cpus-per-task=1
> #SBATCH --mem=2G
> #SBATCH --output=%x-%j.out
>
> cd /home/users/<u>/<user>/slurm-mcp-jobs || exit 1
> srun bash -c 'hostname; date; echo smoke-ok'
> ```
>
> **You:** yes
>
> **Claude** calls `submit_job` with the token from the dry run: job 14501969 is submitted. Then `diagnose_job("14501969")`. A few seconds later the job is done:
>
> ```
> state: COMPLETED
> completed with exit code 0
> memory was over-requested: peak 6 MB of 2048 MB; you could ask for less
> stdout: cpu001.baobab
>         Sun Nov 22 01:32:41 PM CET 2026
>         smoke-ok
> stderr: merged into the stdout log (no separate --error file)
> ```

Today `submit_job` also takes the dry run's `review_text` (see "Safety model").

## Tools

| Tool | What it does | Confirmation |
|---|---|---|
| `check_setup` | The checks of `slurm-mcp doctor`, without the smoke test | no |
| `cluster_info` | Partitions (time limits, nodes, CPUs, memory, GPUs) and Slurm limits such as `MaxArraySize` | no |
| `queue` | Your pending and running jobs | no |
| `recent_jobs(days)` | Your jobs from the last 1 to 30 days | no |
| `job_status(job_id)` | `sacct` rows and whitelisted `scontrol` fields | no |
| `job_log(job_id, stream, lines)` | Tail of a job's stdout or stderr | no |
| `diagnose_job(job_id)` | Verdict (timeout, out of memory, exit code, pending reason, memory used vs requested) and the end of the logs | no |
| `read_file(path, start_line, max_lines)` | Up to 1000 lines of a text file | no |
| `grep_file(path, pattern, regex, ignore_case, context, max_matches)` | Search one file (fixed string by default) | no |
| `tail_file(path, lines)` | Tail of a file | no |
| `list_files(directory, max_depth, pattern)` | Files and folders with type, size, mtime | no |
| `read_parameter_table(path)` | A CSV such as `sim/DF.csv`, as rows | no |
| `storage_info(path)` | `df` of the roots, `du` of a folder (time limit), quota commands (Baobab: home and scratch quota, parsed) | no |
| `list_presets` | Presets, defaults, caps, GPU types | no |
| `compare_code(remote_dir, local_path)` | Local folder against cluster folder (sizes, as in CommonUI) | no |
| `fetch_results(directory, pattern, dry_run)` | Download missing or newer files to `local_dir`; `dry_run` defaults to true | local disk only |
| `submit_preset_dry_run(preset, gpu, command, ...)` | Generate the full sbatch script from a preset | no (returns token) |
| `submit_job_dry_run(script_path, ...)` | Same for a script already on the cluster | no (returns token) |
| `submit_job(token, review_text)` | Write the reviewed script if generated, then `sbatch` | yes |
| `upload_code_dry_run(remote_dir, local_path)` | Which local files would be copied, large files, warnings | no (returns token) |
| `upload_code(token, review_text, confirm_large_files, verify)` | Copy those files; same-path files are overwritten, nothing is deleted | yes |
| `cancel_job_dry_run(job_id)`, `cancel_jobs_dry_run(job_ids, name, state, all_mine)` | List the jobs `scancel` would hit (one or several of your own) | no (returns token) |
| `cancel_job(token, review_text)`, `cancel_jobs(token, review_text)` | Cancel exactly those jobs | yes |
| `hold_job_dry_run(job_id)`, `release_job_dry_run(job_id)` | Show the pending job to hold, or the job you held to release | no (returns token) |
| `hold_job(token, review_text)`, `release_job(token, review_text)` | `scontrol hold` or `scontrol release` | yes |
| `delete_dry_run(path)` | What a recursive delete would remove: counts, size, largest files | no (returns token) |
| `delete(token, review_text)` | Delete it (cannot be undone) | yes |

Changes, their dry runs and `list_presets` exist only with `enable_writes = true`. The upload tools and `compare_code` also need an allowed local folder (see "Code upload").

## Safety model

- **Fixed commands only.** Each tool maps to a fixed command template (`squeue`, `sacct`, `scontrol`, `sinfo`, `tail`, `sed`, `grep`, `find`, `head`, `df`, `du`, `scp`, ...). Arguments are shell-quoted and checked against strict patterns (job ids are `digits` or `digits_arrayindex`). There is no arbitrary shell.
- **Path confinement.** Remote paths must be absolute, use a conservative character set (no spaces, globs, quotes, `~` or `..`) and lie inside a `remote_roots` entry, including log paths that Slurm reports. The config refuses `/`, top-level folders and system folders as roots. Reads, uploads and deletes also resolve the path on the cluster, so a symlink cannot lead out of the roots. Other paths (job working folders, `task_dir`, `du`) are checked by name only: keep roots free of symlinks that point outside, and as narrow as you reasonably can.
- **Bounded output.** Tail length, listing size, table size and download volume are capped by config.
- **Writes off by default.** Write tools are not registered without `enable_writes = true`, and the code refuses them too.
- **Two steps: token and review text.** A dry run stores the exact plan and returns a random single-use token (5 minutes by default, in memory, so a restart invalidates it) and a `review_text`: the exact command and the full script, file list, job list or entries (lists are cut at 300 lines with a count; the plan hash covers all). A write tool needs both, and is refused if `review_text` differs from the plan (the token stays valid). Clients show tool arguments in the approval prompt, so you see what is about to happen. Write tools are annotated destructive, read-only tools `readOnlyHint`.
- **What the token does not prove.** It proves that what runs equals what was reviewed, not that a person approved it: a model could call both steps in a row. The human check is your client's approval prompt, so never set write tools to always-allow (see the `ask` rules).
- **Narrow write scope.** Submit is `sbatch --parsable` of one regular file inside a root (existing or generated), plus the option whitelist below. An existing script is shown in full (up to 20 KB) and hashed at the dry run and again before `sbatch`. Cancel, hold and release use only ids from your own `squeue --me`, never `-u`; a plain id covers a whole array, `123_4` one task. No other command can be passed.
- **Delete is the most guarded write.** Only strictly inside `remote_roots`: never a root, a folder above or containing one, or anything inside `.ssh` or `.gnupg`, and never through a symlink (the resolved path must equal the given one). The token is tied to the exact content; if anything changed after the dry run, nothing is deleted.
- **Secrets are not readable.** `read_file`, `grep_file`, `tail_file`, `job_log`, `read_parameter_table`, `list_files` and `fetch_results` refuse paths with a component that looks like a secret (`.ssh`, `.gnupg`, `.aws`, `.netrc`, `.env*`, `id_rsa*` and other keys, `*.pem`, `*.key`, `credentials*`, `secrets*`, shell and REPL histories, keychains, ...) and do not list them. Add patterns with `sensitive_extra`.
- **Local writes.** `fetch_results` defaults to `dry_run=true`. Real downloads write below `local_dir` mirroring the remote path, use a `.part` file first, refuse more than `max_download_mb`, never replace a newer local file, and refuse a folder that resolves outside `local_dir`.
- **Queue warning and limit.** Nothing is refused below `max_queued_jobs` (default 10000, array tasks counted), since big scans are normal. A dry run that would bring your queue above `warn_queued_jobs` (default 1000) adds a warning, in the result and at the top of `review_text`: "LARGE SUBMISSION: this adds N job(s) or tasks; you would have M jobs queued (you have K now; warning threshold 1000, hard limit 10000)". Above the limit the dry run is refused.
- **Protect the configuration.** `enable_writes`, the roots and the presets live in `~/.config/slurm-mcp/config.toml`, which an agent could edit with its file tools. The server tells the agent to propose any change first, with a warning about what it widens and a diff; the `ask` rules above make the client enforce it.
- **No credentials in the repo or config.** Authentication is your normal `~/.ssh` setup, and `ssh` runs with `BatchMode=yes`, so it never waits at a password prompt.

## Generated jobs and presets

Typical use: write code with an agent, run a quick test on the cluster, read why it failed, fix it, run the real job. The agent does not write batch files: it picks a **preset** from your config and the server renders the script, as CommonUI does.

A preset fixes partition, time, CPUs, memory and (for GPU presets) the number of GPUs, with **hard caps**: `max_time`, `max_cpus`, `max_mem`, `max_gpus` (GPU presets use `max_cpus_per_gpu` instead of `max_cpus`, so 2 GPUs allow twice the CPUs). Overrides above a cap are refused. `config.example.toml` has Baobab presets, checked with read-only `sinfo`: `smoke` (15 min on `debug-cpu`, fastest start), `debug` (30 min on `public-short-cpu`), `standard`, `gpu_debug` (30 min on `shared-gpu`; Baobab has no debug GPU partition), `julia_sim` and `gpu`. The GPU list, partitions, 12 h default time, 3000 MB default memory and `%40` array throttle come from the CommonUI notebook (which preselects A100-40Gb and H100; the example preselects all three). The example presets use the lab's own partition (`private-kruse-gpu`): on another cluster adapt partitions, times and GPU types.

- **GPU is never assumed.** `gpu` is a required parameter with no default, and the agent is told to ask you. If you want a GPU it asks which type from your list. The dry run echoes the choice, for example `GPU: yes, 1 x A100-40Gb|H100`.
- **GPU availability is checked.** The dry run asks `sinfo` which GPU features each chosen partition has. A type missing on one partition only gives a warning ("A100-80Gb is not available on private-kruse-gpu"), because Slurm uses whichever partition has a matching node. If no chosen partition has any chosen type, the job could never start and the dry run refuses.
- **The command is free-form but quoted.** A list of arguments, for example `["julia", "--optimize=3", "main.jl"]`, written as one quoted `srun` line, never a shell string. `{task_id}` in an argument becomes the array task id.
- **Everything else is validated.** Job name, partition, time, memory, array spec, module names and GPU types must match strict patterns or the config's lists, so nothing can add an `#SBATCH` line or a second command. Arrays are limited to `max_array_size` tasks (default 10000, Baobab's Slurm limit, so indices must stay below it) and always get a `%N` concurrency limit.
- **You review the whole script.** The dry run returns the script text, its SHA-256, a cost summary (tasks, CPUs, time, memory, partition, GPU) and the `sbatch` command. The token is tied to that exact script: `submit_job` writes those bytes to `jobs_dir` and runs `sbatch` on them.

A job that runs your code is still code on the cluster. The controls are your review of the full script, the resource caps, the working folder inside your roots, and the client's approval prompt.

### Parameter sweeps from a table

For runs like ActiveDewetting, where task N of a job array reads row N of a `DF.csv`, `submit_preset_dry_run` takes three optional arguments:

- `array_from_table`: path of a CSV on the cluster. The array becomes `1-N`, one task per data row (the header is not counted). The row count comes from one fixed `awk` call; the array limit and index rule apply.
- `env`: variables to export, for example `{"path_to_data": "/srv/.../Data/proj/{task_id}/"}`. Names are validated (no `PATH`, `LD_*`, `HOME`, `SLURM_*` and similar), values are quoted, `{task_id}` becomes the task id.
- `task_dir`: a folder template such as `/srv/.../Data/proj/{task_id}` inside your roots. Each task creates it with `mkdir -p` and enters it before running the command.

Presets can carry default `modules` and `env`. The `julia_sim` preset does (`module load Julia`, `use_gpu=true`), and with `command=["julia", "--optimize=3", "<code>/main.jl"]` the script matches what the CommonUI notebook writes for ActiveDewetting.

### Extra sbatch options

Both submit dry runs accept a short whitelist: `dependency` (`afterok:123`, `afterany:1:2`, `singleton`, ...), `qos`, `exclusive`, `hold` (submit held) and `mail_type`. `qos` is limited to `allowed_qos` (per preset, and in the config for existing scripts), `exclusive` needs `allow_exclusive`, and mail goes only to `mail_user` in the config, never to an address from the agent. Everything is validated before it reaches the script or the command line.

## Code upload

`upload_code_dry_run(remote_dir, local_path=None)` then `upload_code(token, review_text, ...)`, like the CommonUI notebook (which copies a local `sim/` folder into `~/Code/<project>/` with `scp`, without excludes or deletion), plus a review step. The contents of a folder go into `remote_dir` (a single file goes in as `remote_dir/<name>`), and the destination is created if needed.

- **Which local folder.** By default the folder the server was started in (the agent's working folder), which is also the default `local_path`. Claude Desktop starts servers in `/` (checked on a running install), which is refused, so there you list folders in `local_roots`. Claude Code is meant to start them in the project folder (not checked), and `CLAUDE_PROJECT_DIR` is honoured if set. Never used: `/`, your home folder or above, system folders, and general folders such as `~/Documents`. The dry run says which rule allowed the source (`allowed_because`), and `slurm-mcp doctor` lists the folders. `upload_from_cwd = false` turns the default off.
- **Destination.** Inside `remote_roots`, resolved on the cluster with `realpath`.
- **Incremental, no deletion.** Only missing or newer files are sent, so a second upload of the same code sends nothing. Remote files are never deleted (CommonUI does not either). Files that would be replaced are listed as overwrites, with a warning if the remote copy is newer.
- **Large files.** No size limit, since folders may hold initial conditions. Files above `large_file_mb` (default 10) are listed with their sizes, and `upload_code` refuses until called with `confirm_large_files` naming exactly those paths (the token stays valid, so the agent asks you and calls again). Only the number of files is limited (`max_upload_files`, 5000).
- **Never sent.** `.git`, virtual environments, `__pycache__`, the secrets listed in "Safety model" (`.env` files, SSH and cloud keys, `*.pem`, `*.key`, ...), and the folders `out`, `output`, `outputs` and `results`. `data` folders are sent. Symlinks and files with spaces or unusual characters in their names are skipped. Add patterns with `upload_exclude`.
- **No partial files.** Each file is copied to `.<name>.slurm-mcp-part` and renamed into place once it arrived, so a failed copy leaves the old file intact. A failed file is reported and its temporary file removed (the only remote deletion this server does); the others continue.
- **Reviewed means sent.** The token is tied to the exact file list (`plan_sha256`). If a local file changes or becomes a symlink after the dry run, nothing is uploaded.
- **Transport.** One `scp -p` per file over the shared connection, which keeps modification times. No `rsync`, as in CommonUI, for portability (macOS ships a limited `openrsync`).

`compare_code(remote_dir, local_path=None)` is read-only, like CommonUI's check. It lists files only local, only on the cluster, with a different size, or newer on one side (same size), and counts the matches. Sizes decide, times only label same-size files, and files only on the cluster do not count against "identical". `upload_code(..., verify=true)` runs the same size check right after the upload (off by default).

## Tests

```bash
pip install -e ".[dev]"   # once, installs pytest
pytest -q
```

No test needs a cluster or SSH keys. The SSH layer sits behind a `Runner` interface; tests use a scripted `FakeRunner` (`tests/fakes.py`) and, for one end-to-end test, a stub `ssh` that the real server process calls over MCP stdio. Parsing, path validation, sync planning, script generation and diagnosis are pure functions with their own tests, including golden tests of the generated scripts. CI runs Python 3.11 to 3.13.

Not covered: a real Slurm installation. `squeue`, `sacct` and `scontrol` formats differ slightly between Slurm versions, and `find -printf` needs GNU find on the cluster. Check against your own cluster before relying on it.

## Layout

```
src/slurm_mcp/
  config.py    TOML config, validated
  paths.py     remote path validation, secret names
  runner.py    Runner protocol and subprocess implementation
  commands.py  pure ssh/scp argument vectors and fixed remote commands
  parsing.py   pure parsers for squeue, sacct, scontrol, sinfo, find
  sync.py      pure incremental download planning (also used for uploads)
  uploads.py   local scan, exclusions and allowlist for code upload
  presets.py   presets, caps and unit parsing
  sbatchopts.py validated sbatch option whitelist
  jobscript.py pure sbatch script generation
  review.py    review text shown in the approval prompt
  diagnose.py  pure reading of job accounting data
  doctor.py    setup check and smoke test (slurm-mcp doctor)
  confirm.py   single-use confirmation tokens
  cluster.py   cluster operations (read-only, plus changes when enabled)
  server.py    MCP tool definitions
  setup.py     interactive config creation (init)
  __main__.py  serve, login, check, init, doctor
tests/
docs/overview.svg
```

## Acknowledgements

The code, tests and documentation were written by Claude (Anthropic), an AI assistant, under the author's instruction. The author chose the scope and the safety rules, directed the work step by step and tested it on the Baobab cluster.

## License

MIT.
