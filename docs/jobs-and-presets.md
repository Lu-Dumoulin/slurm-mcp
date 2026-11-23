# Generated jobs and presets

See also: [Safety model](safety.md), [Configuration](configuration.md).

Terms: a **root** is a remote folder that the tools can use (you list roots in `remote_roots`). An **upload folder** is a local folder that the agent can upload from.

Typical use: you write code with an agent and run a quick test on the cluster. You read why it failed, fix it and run the real job. The agent does not write batch files. It selects a **preset** from your config. The server makes the script, as CommonUI does.

A preset sets the partition, time, CPUs, memory and (for GPU presets) the number of GPUs. It has **hard caps**: `max_time`, `max_cpus`, `max_mem` and `max_gpus`. A GPU preset uses `max_cpus_per_gpu` instead of `max_cpus`, so 2 GPUs allow twice the CPUs. The server refuses a value above a cap.

`config.example.toml` has these Baobab presets. We checked them with read-only `sinfo`:

- `smoke`: 15 minutes on `debug-cpu`. This is the fastest start.
- `debug`: 30 minutes on `public-short-cpu`.
- `standard`: a CPU job for production runs.
- `gpu_debug`: 30 minutes on `shared-gpu`. Baobab has no debug GPU partition.
- `julia_sim` and `gpu`: GPU jobs.

The CommonUI notebook gives these values:

- the GPU list and the partitions
- the default time of 12 h and the default memory of 3000 MB
- the array limit `%40`

CommonUI selects A100-40Gb and H100. The example selects all three types. The example presets use the partition of our lab (`private-kruse-gpu`). On another cluster, change the partitions, times and GPU types.

- **The agent never assumes a GPU.** `gpu` is a required parameter and has no default. The agent must ask you. If you want a GPU, it asks which type from your list. The dry run shows the choice, for example `GPU: yes, 1 x A100-40Gb|H100`.
- **The server checks GPU availability.** The dry run asks `sinfo` which GPU features each chosen partition has. If a type is missing on one partition, you get a warning ("A100-80Gb is not available on private-kruse-gpu"). Slurm uses the partition that has a matching node. If no chosen partition has a chosen type, the job cannot start. Then the dry run refuses.
- **The command is free-form but quoted.** It is a list of arguments, for example `["julia", "--optimize=3", "main.jl"]`. The server writes it as one quoted `srun` line, never as a shell string. `{task_id}` in an argument becomes the array task id.
- **The server validates everything else.** A strict pattern or a list in your config limits each value. The values are: job name, partition, time, memory, array spec, module names and GPU types. Nobody can add an `#SBATCH` line or a second command.
- **Array limits.** An array has at most `max_array_size` tasks (default 10000). This is the Slurm limit of Baobab, so the indices must stay below it. Each array gets a `%N` limit for concurrent tasks.
- **You review the whole script.** The dry run returns the script text, its SHA-256, a cost summary and the `sbatch` command. The cost summary shows tasks, CPUs, time, memory, partition and GPU.
- **The token covers the exact script.** `submit_job` writes these bytes to `jobs_dir` and runs `sbatch` on them.

A job that runs your code is still code on the cluster. You control it with four checks. These are your review of the full script, the resource caps, a working folder inside a root, and the approval prompt of your client.

## Parameter sweeps from a table

Some runs, such as ActiveDewetting, use task N of a job array for row N of a `DF.csv`. For these runs, `submit_preset_dry_run` accepts three optional arguments:

- `array_from_table`: the path of a CSV on the cluster. The array becomes `1-N`, with one task for each data row (the header does not count). One fixed `awk` call counts the rows. The array limit and the index rule apply.
- `env`: variables to export, for example `{"path_to_data": "/srv/.../Data/proj/{task_id}/"}`. The server checks the names and quotes the values. `PATH`, `LD_*`, `HOME`, `SLURM_*` and similar names are not allowed. `{task_id}` becomes the task id.
- `task_dir`: a folder template such as `/srv/.../Data/proj/{task_id}` inside a root. Each task creates the folder with `mkdir -p` and enters it before it runs the command.

A preset can have default `modules` and `env`. The `julia_sim` preset has them (`module load Julia`, `use_gpu=true`). With `command=["julia", "--optimize=3", "<code>/main.jl"]`, the script is equal to the script that the CommonUI notebook writes for ActiveDewetting.

## Extra sbatch options

Both submit dry runs accept a short whitelist of options:

- `dependency`: for example `afterok:123`, `afterany:1:2` or `singleton`.
- `qos`: limited to `allowed_qos` (in a preset, and in the config for existing scripts).
- `exclusive`: needs `allow_exclusive`.
- `hold`: submits the job on hold.
- `mail_type`: mail goes only to `mail_user` in the config, never to an address from the agent.

The server checks every value before it reaches the script or the command line.

## Queue limit and warning

The server refuses nothing below `max_queued_jobs` (default 10000), because big scans are normal. Array tasks count as jobs. Above the limit, the server refuses the dry run.

If a dry run brings your queue above `warn_queued_jobs` (default 1000), it adds a warning. The warning is in the result and at the top of `review_text`. It reads: "LARGE SUBMISSION: this adds N job(s) or tasks; you would have M jobs queued (you have K now; warning threshold 1000, hard limit 10000)".
