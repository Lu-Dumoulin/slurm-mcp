# Tools

See also: [Safety model](safety.md), [Configuration](configuration.md).

| Tool | What it does | Confirmation |
|---|---|---|
| `check_setup` | Runs the checks of `slurm-mcp doctor`, without the smoke test | no |
| `cluster_info` | Shows partitions (time limits, nodes, CPUs, memory, GPUs) and Slurm limits such as `MaxArraySize` | no |
| `queue` | Shows your pending and running jobs | no |
| `recent_jobs(days)` | Shows your jobs of the last 1 to 30 days | no |
| `job_status(job_id)` | Shows `sacct` rows and selected `scontrol` fields | no |
| `job_log(job_id, stream, lines)` | Shows the end of stdout or stderr | no |
| `diagnose_job(job_id)` | Gives a verdict (timeout, out of memory, exit code, pending reason, memory used and requested) and the end of the logs | no |
| `read_file(path, start_line, max_lines)` | Shows up to 1000 lines of a text file | no |
| `grep_file(path, pattern, regex, ignore_case, context, max_matches)` | Searches one file (a fixed string, unless `regex` is true) | no |
| `tail_file(path, lines)` | Shows the end of a file | no |
| `list_files(directory, max_depth, pattern)` | Lists files and folders with type, size and modification time | no |
| `read_parameter_table(path)` | Reads a CSV such as `sim/DF.csv` as rows | no |
| `storage_info(path)` | Shows `df` of the roots, `du` of a folder (with a time limit) and quota commands (Baobab: home and scratch quota, parsed) | no |
| `list_presets` | Shows presets, defaults, caps and GPU types | no |
| `compare_code(remote_dir, local_path)` | Compares a local folder with a cluster folder (sizes, as in CommonUI) | no |
| `fetch_results(directory, pattern, dry_run)` | Downloads missing or newer files to `local_dir` (`dry_run` is true by default) | local disk only |
| `submit_preset_dry_run(preset, gpu, command, ...)` | Makes the full sbatch script from a preset | no (gives a token) |
| `submit_job_dry_run(script_path, ...)` | Does the same for a script that is already on the cluster | no (gives a token) |
| `submit_job(token, review_text)` | Writes the script (if generated) and runs `sbatch` | yes |
| `upload_code_dry_run(remote_dir, local_path)` | Shows which local files it would copy, the large files and the warnings | no (gives a token) |
| `upload_code(token, review_text, confirm_large_files, verify)` | Copies those files. It replaces files with the same path. It deletes nothing | yes |
| `cancel_job_dry_run(job_id)`, `cancel_jobs_dry_run(job_ids, name, state, all_mine)` | Lists the jobs that `scancel` would cancel (one job or several of your jobs) | no (gives a token) |
| `cancel_job(token, review_text)`, `cancel_jobs(token, review_text)` | Cancels exactly those jobs | yes |
| `hold_job_dry_run(job_id)`, `release_job_dry_run(job_id)` | Shows the pending job to hold, or the job that you held and want to release | no (gives a token) |
| `hold_job(token, review_text)`, `release_job(token, review_text)` | Runs `scontrol hold` or `scontrol release` | yes |
| `delete_dry_run(path)` | Shows what a recursive delete would remove: counts, size, largest files | no (gives a token) |
| `delete(token, review_text)` | Deletes it. You cannot undo this | yes |

Changes, their dry runs and `list_presets` exist only when `enable_writes = true`. The upload tools and `compare_code` also need an allowed local folder (see "Code upload").
