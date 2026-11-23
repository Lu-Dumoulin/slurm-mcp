# Configuration

See also: [Safety model](safety.md), [Generated jobs and presets](jobs-and-presets.md), [Code upload](upload.md).

## Config file

The config file is `~/.config/slurm-mcp/config.toml`. It is a TOML file. It has no credentials.

- To use another file, set `SLURM_MCP_CONFIG` or use `--config`.
- `slurm-mcp init` writes the file. It never overwrites an existing config without asking.
- `config.example.toml` shows all keys with comments. It has Baobab presets.

To install by hand, use these commands:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
slurm-mcp init          # or copy config.example.toml to ~/.config/slurm-mcp/config.toml
```

## What `init` does

`install.sh` creates a virtual environment and installs the package (it needs `mcp` 2.x). It links `slurm-mcp` into `~/.local/bin`. Then it runs `slurm-mcp init`. `init` asks these questions:

- The login host and your username.
- Whether to test the connection. The test also opens the shared SSH connection.
- Which roots to use. It proposes your home folder and the folder symlinks in it (on many clusters, `~/scratch` is a symlink).
- The `local_dir` for downloads, and an optional parameter table.
- Which extra upload folders to use (`local_roots`).

`init` checks your answers and writes the config file.

## Config keys

Required keys:

| Key | Meaning |
|---|---|
| `host` | The login host of the cluster. |
| `user` | Your username on the cluster. |
| `remote_roots` | The roots: remote folders that the tools can use. The config refuses `/`, top-level folders and system folders. |
| `local_dir` | The local folder for downloads. |

Optional keys (the default is in the second column):

| Key | Default | Meaning |
|---|---|---|
| `enable_writes` | `false` | Turns on all changes (submit, cancel, hold, release, upload, delete). |
| `ssh_multiplexing` | `auto` | `auto` uses a shared SSH connection, except on Windows. `on` and `off` force the choice. See [Windows](#windows). |
| `auto_connect` | `true` | Open the shared SSH connection without a prompt when no connection is open. It has no effect when the shared connection is off. |
| `df_csv` | none | The default parameter table for `read_parameter_table`. |
| `control_dir` | `~/.ssh/slurm-mcp-cm` | The folder for the `ControlMaster` socket. |
| `control_persist` | `10m` | How long the shared connection stays open after the last use. |
| `connect_timeout` | `15` | The SSH connection timeout, in seconds. |
| `command_timeout` | `60` | The timeout of one remote command, in seconds. |
| `extra_ssh_options` | `[]` | Extra options for `ssh`. |
| `max_tail_lines` | `500` | The maximum number of lines for `tail_file` and `job_log`. |
| `max_list_entries` | `1000` | The maximum number of entries that `list_files` returns. |
| `max_download_mb` | `500` | The maximum size of one `fetch_results` download. |
| `du_timeout_s` | `120` | The time limit of `du` in `storage_info`, in seconds. |
| `quota_commands` | `[]` | Fixed commands whose output `storage_info` returns. Baobab: `[["beegfs-get-quota-home-scratch.sh"]]`. Never add `-u` or `-g`. |
| `confirmation_ttl_s` | `300` | How long a token is valid, in seconds. |
| `jobs_dir` | first root + `/slurm-mcp-jobs` | The folder for generated scripts. It must be inside a root. |
| `max_array_size` | `10000` | The maximum number of array tasks. Indices must stay below it. |
| `array_concurrency` | `40` | The `%N` limit for concurrent tasks of an array. |
| `warn_queued_jobs` | `1000` | A dry run above this number of queued jobs adds a warning. |
| `max_queued_jobs` | `10000` | The server refuses a dry run above this number of queued jobs. |
| `allowed_qos` | `[]` | The `qos` names that the agent can use with existing scripts. |
| `allow_exclusive` | `false` | Allows `exclusive` for existing scripts. |
| `mail_user` | none | The only address for job mail. |
| `local_roots` | `[]` | Extra upload folders, besides the working folder of the agent. |
| `upload_from_cwd` | `true` | Use the folder where the server started as an upload folder. |
| `upload_exclude` | `[]` | Extra name patterns that the server never uploads. |
| `large_file_mb` | `10` | Files above this size need a separate confirmation to upload. |
| `max_upload_files` | `5000` | The maximum number of files in one upload. |
| `sensitive_extra` | `[]` | Extra name patterns that the server never reads, lists or downloads. |
| `gpu_types` | none | A table of GPU names and Slurm features. See [Generated jobs and presets](jobs-and-presets.md). |
| `presets` | none | The presets. See [Generated jobs and presets](jobs-and-presets.md). |

## Shared SSH connection

Clusters often limit logins. Therefore all calls share one OpenSSH `ControlMaster` connection (`ControlPersist`, default 10 minutes). This works on macOS and Linux. Windows OpenSSH has no multiplexing.

If `auto_connect = true` and no connection is open, the server opens one without a prompt. This needs a key that works without input: an unlocked key, `ssh-agent`, or the macOS keychain with `UseKeychain yes`. If the cluster needs a passphrase, a password or a second factor, the call fails. The message tells you to run `slurm-mcp login`. The model cannot do this for you. If `auto_connect = false`, the server uses only a connection that you opened yourself.

To open the connection by hand:

1. Open a terminal on your computer. Do not use the agent. Do not use the cluster.
2. Run `slurm-mcp login`. You can start from any folder.
3. Enter your password or passphrase. The prompt returns and the connection stays open.
4. To see if the connection is open, run `slurm-mcp check`.

If `slurm-mcp` is not in your `PATH`, use `/path/to/slurm-mcp/.venv/bin/slurm-mcp`.

## Windows

OpenSSH on Windows cannot share connections. The server turns the shared connection off by itself (`ssh_multiplexing = "auto"`). To force it, set `ssh_multiplexing` to `"on"` or `"off"`.

**Warning:** Without a shared connection, each tool call opens its own SSH connection. Some clusters limit logins. Many calls in a row can reach this limit.

To prepare a Windows computer:

1. Open PowerShell as administrator. Run `Get-Service ssh-agent | Set-Service -StartupType Automatic`, then `Start-Service ssh-agent`.
2. Load your key: `ssh-add $env:USERPROFILE\.ssh\id_ed25519`. `ssh-add` asks for the passphrase once. The ssh-agent keeps the key.
3. Run `slurm-mcp check`. It tests one connection that must log in without a prompt.
4. Run `slurm-mcp doctor`. The line `ssh connection` shows that the shared connection is off.

On Windows, these commands behave differently:

- `slurm-mcp check` does not use `ssh -O check`. It tests one new connection.
- `slurm-mcp login` does not keep a connection open. It tests one login, and the login can ask for a password.
- `slurm-mcp doctor` shows the value of `ssh_multiplexing`.
- The server does not create the `control_dir` folder.

Rules for paths:

- In the config file, write a Windows path with forward slashes (`D:/Data/results`) or in single quotes (`'D:\Data\results'`). `init` writes them correctly.
- In `claude_desktop_config.json`, write backslashes twice: `"D:\\tools\\slurm-mcp\\.venv\\Scripts\\slurm-mcp.exe"`.
- `local_dir`, `local_roots` and the upload folder can use a drive letter.
- The server refuses a drive root (`C:\`), your profile folder and any folder above it. It also refuses `C:\Windows`, `C:\Program Files` and general folders such as `Documents`, `Downloads` and `OneDrive`.
- The server treats a junction like a symlink. It does not enter it.
- Remote paths always use `/`.

Not verified on a real Windows computer:

- `scp` with a drive letter in a local path, for example `D:/Data/file`. If `scp` reads `D` as a host name, report it.
- The tests that create symlinks need Windows developer mode.

## Check that it works

```bash
slurm-mcp doctor
```

`slurm-mcp doctor` only reads. It checks these items:

- the config and the SSH connection (no prompt)
- `squeue`
- each root and each preset partition
- the GPU types, the upload folders and the jobs folder

Each line shows `OK`, `WARN` or `FAIL` and the reason.

```bash
slurm-mcp doctor --smoke
```

The smoke test runs a real small job. Follow these steps:

1. Run the command. It shows the full `smoke` script (15 minutes on `debug-cpu`).
2. Type `y` to approve the job.
3. Wait. The command follows the job, shows the diagnosis and the output, and reports pass or fail.

To test another preset, use `--preset debug`. After 5 minutes in the queue, the command stops and shows the job id. The job stays in the queue.
