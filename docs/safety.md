# Safety model

This document lists all safety rules of slurm-mcp. For a short summary, see the [README](../README.md#safety-summary).

Terms: a **root** is a remote folder that the tools can use (you list roots in `remote_roots`). An **upload folder** is a local folder that the agent can upload from.

**Warning:** The token does not prove that a person approved a change. A model can call both steps one after the other. The check by a person is the approval prompt of your client. Never set write tools to "always allow". Use the `ask` rules above.

- **Fixed commands only.** Each tool uses a fixed command template (`squeue`, `sacct`, `scontrol`, `sinfo`, `tail`, `sed`, `grep`, `find`, `head`, `df`, `du`, `scp` and others). The server quotes all arguments and checks them against strict patterns. A job id must be `digits` or `digits_arrayindex`. There is no arbitrary shell.
- **Path limits.** A remote path must be absolute and must use a safe character set (no spaces, globs, quotes, `~` or `..`). It must be inside a root. This also applies to log paths from Slurm. The config refuses `/`, top-level folders and system folders as roots.
- **Symlinks.** Reads, uploads and deletes resolve the path on the cluster, so a symlink cannot lead out of the roots. The server checks other paths (job working folders, `task_dir`, `du`) by name only. Keep your roots small. Keep them free of symlinks that point outside.
- **Limited output.** The config limits tail length, listing size, table size and download volume.
- **Changes are off by default.** The server does not register write tools unless `enable_writes = true`. The code also refuses them.
- **Two steps: token and review text.** A dry run stores the exact plan. It returns a random one-time token and a `review_text`. The token is valid for 5 minutes by default. The server keeps it in memory, so a restart makes it invalid.
- **Review text.** The `review_text` contains the exact command and the full script, file list, job list or entries. Lists stop at 300 lines with a count. The plan hash covers all entries.
- **Write call.** A write tool needs the token and the `review_text`. If `review_text` is different from the plan, the server refuses the call. The token stays valid.
- **Approval prompt.** Clients show the arguments of a tool in the approval prompt. You see what will happen. Write tools have the "destructive" annotation. Read-only tools have `readOnlyHint`.
- **Small write scope.** Submit means `sbatch --parsable` of one regular file inside a root, with the option whitelist below. The file can exist already or the server can generate it.
- **Existing scripts.** The server shows an existing script in full (up to 20 KB). It hashes the script at the dry run and again before `sbatch`.
- **Cancel, hold and release.** They use only ids from your own `squeue --me`. They never use `-u`. A plain id covers a whole array. Use `123_4` for one task. No other command is possible.
- **Delete has the most checks.** It works only strictly inside a root. It never deletes a root, a folder above a root or a folder that contains a root. It never works inside `.ssh` or `.gnupg`. It never works through a symlink, so the resolved path must equal the given path.
- **Delete token.** The token covers the exact content. If anything changes after the dry run, the server deletes nothing.
- **Secrets are not readable.** These tools refuse a path with a secret name: `read_file`, `grep_file`, `tail_file`, `job_log`, `read_parameter_table`, `list_files` and `fetch_results`. They do not list these names. Add your own patterns with `sensitive_extra`.
- **Secret names.** The list includes `.ssh`, `.gnupg`, `.aws`, `.netrc`, `.env*`, `id_rsa*` and other keys, `*.pem`, `*.key`, `credentials*`, `secrets*`, shell and REPL histories, and keychains.
- **Local writes.** `fetch_results` has `dry_run=true` by default. A real download writes below `local_dir` and copies the remote path. It uses a `.part` file first.
- **Download limits.** `fetch_results` refuses a download larger than `max_download_mb`. It never replaces a newer local file. It refuses a folder that resolves outside `local_dir`.
- **Queue limit and warning.** The server refuses a dry run above `max_queued_jobs` and warns above `warn_queued_jobs`. See [Queue limit and warning](jobs-and-presets.md#queue-limit-and-warning).
- **Protect the configuration.** The file `~/.config/slurm-mcp/config.toml` holds `enable_writes`, the roots and the presets. An agent can edit it with its file tools. The server tells the agent to propose each change first, with a warning about what it widens and a diff. The `ask` rules above make the client enforce this.
- **No credentials in the repository or the config.** Authentication uses your normal `~/.ssh` setup. `ssh` runs with `BatchMode=yes`, so it never waits at a password prompt.

## Keep the agent on slurm-mcp

The agent must use only these tools to reach the cluster. Then every action passes the checks in "Safety model". The server tells the agent this in its instructions. To make Claude Code enforce it, add these rules to `.claude/settings.json` (one project) or `~/.claude/settings.json` (all projects):

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

- Claude Code checks the rules in this order: deny, ask, allow. An ask rule asks you, even when an allow rule also matches.
- In auto mode, a classifier approves for you. Keep the ask rules in this mode too.
- `Bash(ssh *)` is the current syntax. `Bash(ssh:*)` is an older form with the same meaning.
- slurm-mcp itself still uses `ssh` and `scp`. This is correct, because it runs outside the Bash tool of the agent.
- **Warning:** A deny rule does not match a full path or `sh -c`. An `Edit(...)` rule does not cover a shell redirect such as `echo >> file`. For a hard guarantee, also restrict the sandbox of the agent.
- These rules work for one agent. Other agents have their own settings.
- You can add this line to `CLAUDE.md` or `AGENTS.md`: "Use the slurm MCP tools for everything on the cluster. Never run ssh, scp or rsync yourself."
