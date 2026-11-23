# slurm-mcp: working agreement

## What this is
A Python MCP server (`mcp` 2.x, `MCPServer`) that inspects a Slurm cluster over SSH.
Inspection tools are read-only. Submit and cancel exist (phase 2), are off unless
`enable_writes = true`, and always go through a dry run plus a single-use confirmation token.

## Guardrails
1. **No arbitrary shell.** Remote commands are fixed templates in `commands.py`, built as
   argument lists and joined with `shlex.join`. Never interpolate a tool parameter into a
   command string.
2. **Every remote path goes through `validate_remote_path`**, including paths read back
   from Slurm. Job ids go through `validate_job_id`. Use `fullmatch`, not `match` with `$`
   (`$` accepts a trailing newline).
3. **Tests never touch the network.** Use `FakeRunner` from `tests/fakes.py`. Anything that
   needs a real cluster belongs in a separate, manually run script, not in `pytest`.
4. **No credentials, hosts or usernames in the repo.** They live in the user's config file.
5. **Nothing writes to stdout** except the MCP protocol (stdio transport). Log to stderr.
6. **No new dependencies without a reason.**
7. **Writes are two steps.** A write tool takes only a confirmation token from its dry run,
   never the parameters again, and is not registered unless `enable_writes` is true. Do not
   add a write path that skips `ConfirmationStore`, or one that accepts free-form commands
   or extra `sbatch` options without a whitelist and tests.
8. **Generated scripts are built only by `jobscript.py`.** Every value in the script passes a
   strict pattern, a config list or `shlex.quote`; add a golden test for any change to the
   rendered text. Caps in presets are hard limits, never defaults that a parameter can lift.
9. **`gpu` stays a required parameter with no default.** The user decides, the model asks.
10. **Uploads are the only path from local disk to the cluster.** `uploads.py` enforces the local
   allowlist (`local_roots`), name exclusions, no symlink following and size limits; `cluster.py`
   resolves the remote destination with `realpath`. Never add a delete option (the only remote
   removal is our own `.slurm-mcp-part` temp file), never upload outside the allowed folders
   (the server's working folder when it is not too broad, plus `local_roots`), keep the token
   bound to the reviewed file list, and keep the extra confirmation for files above `large_file_mb`.
11. **Delete and cancel-many stay narrow.** `delete` only strictly inside the roots, never a root,
   its parents or a symlink, with a token tied to the listed content and a re-check before
   `rm`. `cancel_jobs` only lists jobs from `squeue --me` and passes explicit ids to `scancel`,
   never `-u`. Extra sbatch options stay a validated whitelist; the mail address comes from the
   config only.
12. **Every write tool takes the plan's `review_text`.** The dry run returns the exact command and
   the full script, file list, job list or entries; the write call must repeat it unchanged (the
   server checks it before redeeming the token) so the client's approval prompt shows what will
   happen. New write tools must do the same. Read paths go through `_read_path` (secret names,
   realpath); never read a path with `_path` alone. Roots stay narrow: the config refuses `/`.
   Changes to `~/.config/slurm-mcp/config.toml` or the Claude Desktop config are proposed first
   with a warning about what they widen and a diff, and applied only after the user approves.
13. **Never run a real submit, cancel, delete or upload to "test" the server** without the user's explicit
   say-so; use the mocked runner.

## Structure rules
- Pure functions decide (parsing, planning, validation); `cluster.py` performs effects
  through a `Runner`. Keep that split: the pure part is the part worth unit-testing.
- Tool functions in `server.py` stay thin and are wrapped with `reportable` so that
  `SlurmMcpError` messages reach the model.
- Add a test with each behaviour change. Run `pytest -q` before finishing.

## Writing style (README, docstrings, commit messages)
Modest, factual tone. No em-dashes. No sales language. State limitations plainly.
