# slurm-mcp: working agreement

## What this is
A Python MCP server (`mcp` 2.x, `MCPServer`) that inspects a Slurm cluster over SSH.
Phase 1 is read-only. Phase 2 (sbatch, scancel) is planned and must sit behind a
dry-run plus confirmation-token step.

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

## Structure rules
- Pure functions decide (parsing, planning, validation); `cluster.py` performs effects
  through a `Runner`. Keep that split: the pure part is the part worth unit-testing.
- Tool functions in `server.py` stay thin and are wrapped with `reportable` so that
  `SlurmMcpError` messages reach the model.
- Add a test with each behaviour change. Run `pytest -q` before finishing.

## Writing style (README, docstrings, commit messages)
Modest, factual tone. No em-dashes. No sales language. State limitations plainly.
