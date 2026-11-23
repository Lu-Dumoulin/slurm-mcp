# slurm-mcp

An MCP server. It lets a coding agent look at a Slurm cluster over SSH. With your approval, it can also run jobs.

## In 30 seconds

![A coding agent on your computer talks MCP to slurm-mcp, which runs on the same computer and reaches the Slurm cluster over one shared SSH connection](docs/overview.svg)

- **What it is.** A narrow and safe window onto your cluster for a coding agent. You write code on your laptop. Then you ask "why did job 123 fail?" or "run this on the debug preset". You do not paste terminal output.
- **Where it runs.** On your computer. The agent starts it over MCP (stdio). It uses one shared SSH connection to the cluster. Nothing is installed on the cluster. No password is stored.
- **Which agents.** It works with Claude Code and Claude Desktop. We did not test other MCP clients. They must support local stdio servers.
- **Safety.** Read-only by default. Changes (submit, cancel, upload, delete) are off until you turn them on. Then each change needs a dry run, a one-time token and your approval.

I built this mainly for my lab, the Kruse group at the University of Geneva. We run simulations on the UNIGE Baobab cluster and most of us write code with a coding agent. The agent could not see the cluster, so we pasted `squeue` output and logs into the chat. Each person runs the server on their own machine with their own SSH key and config. The design follows the SSH layer of [CommonUI](https://github.com/Lu-Dumoulin/CommonUI), a Julia tool for parameter sweeps.

**Status.** Offline tests cover all tools. On Baobab we ran only these tools so far: the read-only tools, `slurm-mcp doctor --smoke` and the cancel of a pending job. The smoke test ran a real job from the `smoke` preset to the end. The tools for upload, sweeps, delete and hold have offline tests only.

## Install

Terms: a **root** is a remote folder that the tools can use (you list roots in `remote_roots`). An **upload folder** is a local folder that the agent can upload from.

1. Install Python 3.11 or newer.
2. Clone the repository and open its folder.
3. On macOS or Linux, run `./install.sh`. On Windows, run `install.bat` and read the [Windows notes](docs/configuration.md#windows).
4. Answer the questions of `slurm-mcp init`. The script starts it.
5. If `~/.local/bin` is not in your `PATH`, add it. The script links `slurm-mcp` into this folder.
6. Register the server with your agent (see below).
7. Run `slurm-mcp doctor` to check the setup.

For details, see [Configuration](docs/configuration.md). It also explains the shared SSH connection and `slurm-mcp login`.

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

## Example session

Recorded on Baobab, 22 November 2026. The output is shortened. `<user>` replaces the username. `<u>` is the first letter of `<user>` (home folders are `/home/users/<u>/<user>`).

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

Today, `submit_job` also needs the `review_text` of the dry run (see "Safety model").

## Tools

| Group | Tools | Confirmation |
|---|---|---|
| Show the cluster and your jobs | `check_setup`, `cluster_info`, `queue`, `recent_jobs`, `job_status`, `job_log`, `diagnose_job`, `list_presets`, `storage_info` | no |
| Read files and compare folders | `read_file`, `grep_file`, `tail_file`, `list_files`, `read_parameter_table`, `compare_code` | no |
| Download results | `fetch_results` (`dry_run` is true by default) | local disk only |
| Dry runs | `submit_preset_dry_run`, `submit_job_dry_run`, `upload_code_dry_run`, `cancel_job_dry_run`, `cancel_jobs_dry_run`, `hold_job_dry_run`, `release_job_dry_run`, `delete_dry_run` | no (each gives a token and a `review_text`) |
| Changes | `submit_job`, `upload_code`, `cancel_job`, `cancel_jobs`, `hold_job`, `release_job`, `delete` | yes (token and `review_text`) |

Changes, dry runs and `list_presets` exist only when `enable_writes = true`. The upload tools and `compare_code` also need an upload folder. See [Tools](docs/tools.md) for the arguments and the description of each tool.

## Safety summary

**Warning:** The token does not prove that a person approved a change. The approval prompt of your client is the check by a person. Never set write tools to "always allow". Use the `ask` rules in [Safety model](docs/safety.md).

- Read-only by default. Write tools exist only when `enable_writes = true`.
- Each tool runs a fixed command. There is no arbitrary shell. The server checks every argument.
- A tool works only inside the roots. The server resolves symlinks and refuses secret names such as `.ssh` and `.env*`.
- Each change has two steps: a dry run, then a call with the token and the `review_text` of the dry run. The approval prompt shows the exact command, script, files or jobs.
- Delete has the most checks. Upload never deletes. Large files and large queues need extra confirmation or give a warning.
- The agent must use only these tools. Claude Code rules can enforce this.

For all rules, see [Safety model](docs/safety.md).

## More documentation

- [Tools](docs/tools.md): the arguments and the description of each tool.
- [Safety model](docs/safety.md): all safety rules and the Claude Code rules.
- [Generated jobs and presets](docs/jobs-and-presets.md): presets, generated scripts, sweeps from a table, sbatch options and the queue limit.
- [Code upload](docs/upload.md): upload, compare and verify.
- [Configuration](docs/configuration.md): config keys, the SSH connection and the doctor.
- [Development](docs/development.md): tests and code layout.

## Tests

1. Run `pip install -e ".[dev]"` once. This installs pytest.
2. Run `pytest -q`.

No test needs a cluster or SSH keys. For details and for the limits of the tests, see [Development](docs/development.md).

## Acknowledgements

The code, tests and documentation were written by Claude (Anthropic), an AI assistant, under the author's instruction. The author chose the scope and the safety rules, directed the work step by step and tested it on the Baobab cluster.

## License

MIT.
