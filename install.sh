#!/bin/sh
# Creates a virtual environment, installs slurm-mcp, and runs the interactive config setup.
# Run from the repository folder:  ./install.sh
set -eu
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11+ is required (set PYTHON=...)"; exit 1; }
"$PY" -m venv .venv
.venv/bin/pip install --quiet -e .
.venv/bin/slurm-mcp init
echo
echo "Register with Claude Code:"
echo "  claude mcp add slurm -- $(pwd)/.venv/bin/slurm-mcp"
echo "Open the shared SSH connection (asks for your password or second factor):"
echo "  $(pwd)/.venv/bin/slurm-mcp login"
