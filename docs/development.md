# Development

## Tests

To run the tests:

1. Run `pip install -e ".[dev]"` once. This installs pytest.
2. Run `pytest -q`.

No test needs a cluster or SSH keys. The SSH layer is behind a `Runner` interface. The tests use a scripted `FakeRunner` (`tests/fakes.py`). One end-to-end test uses a stub `ssh` that the real server process calls over MCP stdio. Parsing, path checks, sync planning, script generation and diagnosis are pure functions with their own tests. These include golden tests of the generated scripts. CI runs Python 3.11 to 3.13.

**Warning:** The tests do not cover a real Slurm installation. The formats of `squeue`, `sacct` and `scontrol` differ a little between Slurm versions. `find -printf` needs GNU find on the cluster. Check the tools on your own cluster before you rely on them.

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
