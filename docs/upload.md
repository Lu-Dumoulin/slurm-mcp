# Code upload

See also: [Safety model](safety.md), [Configuration](configuration.md).

Terms: a **root** is a remote folder that the tools can use (you list roots in `remote_roots`). An **upload folder** is a local folder that the agent can upload from.

Use `upload_code_dry_run(remote_dir, local_path=None)` and then `upload_code(token, review_text, ...)`. This is like the CommonUI notebook, which copies a local `sim/` folder into `~/Code/<project>/` with `scp`, without exclusions or deletion. We add a review step. The contents of a folder go into `remote_dir`. A single file goes in as `remote_dir/<name>`. The server creates the destination if needed.

- **Upload folder.** By default, this is the folder where the server started (the working folder of the agent). It is also the default `local_path`. The dry run shows which rule allowed the source (`allowed_because`).
- **Claude Desktop.** It starts servers in `/` (we checked this on a running install). The server refuses `/`, so you must list upload folders in `local_roots`.
- **Claude Code.** It is designed to start servers in the project folder (we did not check this). The server also uses `CLAUDE_PROJECT_DIR` if it is set.
- **Folders that are never used.** `/`, your home folder or a folder above it, system folders, and general folders such as `~/Documents`. Set `upload_from_cwd = false` to turn the default off. `slurm-mcp doctor` lists the upload folders.
- **Destination.** It must be inside a root. The server resolves it on the cluster with `realpath`.
- **Incremental, no deletion.** The server sends only missing or newer files, so a second upload of the same code sends nothing. It never deletes remote files (CommonUI does not delete either). The dry run lists the files that it would replace as overwrites. It warns you if the remote copy is newer.
- **Large files.** There is no size limit, because folders can hold initial conditions. The dry run lists files above `large_file_mb` (default 10) with their sizes. `upload_code` refuses until you call it with `confirm_large_files` that names exactly those paths. The token stays valid, so the agent asks you and calls again.
- **File count.** Only the number of files has a limit (`max_upload_files`, 5000).
- **Files that are never sent.** `.git`, virtual environments, `__pycache__`, and the secrets from "Safety model" (`.env` files, SSH and cloud keys, `*.pem`, `*.key` and more). Also the folders `out`, `output`, `outputs` and `results`. The server skips symlinks and files with spaces or unusual characters in the name. Add patterns with `upload_exclude`. The server sends `data` folders.
- **No partial files.** The server copies each file to `.<name>.slurm-mcp-part` and renames it when the copy is complete. If a copy fails, the old file stays unchanged. The server reports the failed file and removes its temporary file. This is the only remote deletion of this server. It continues with the other files.
- **What you review is what is sent.** The token covers the exact file list (`plan_sha256`). If a local file changes or becomes a symlink after the dry run, the server uploads nothing.
- **Transport.** The server runs one `scp -p` for each file over the shared connection. This keeps the modification times. It does not use `rsync`, as in CommonUI, to stay portable (macOS has a limited `openrsync`).

## Compare local and remote files

`compare_code(remote_dir, local_path=None)` only reads, like the check in CommonUI. It lists these files:

- files that exist only locally
- files that exist only on the cluster
- files with a different size
- files with the same size that are newer on one side

It also counts the matching files. The size decides. The time only labels files with the same size. Files that exist only on the cluster do not count against "identical".

`upload_code(..., verify=true)` does the same size check right after the upload. It is off by default.
