#!/bin/sh
# Creates a virtual environment, installs slurm-mcp, runs the interactive config setup,
# and puts a `slurm-mcp` command on your PATH so it can be run from any folder.
# Run from the repository folder:  ./install.sh
set -eu
cd "$(dirname "$0")"
HERE="$(pwd)"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11+ is required (set PYTHON=...)"; exit 1; }
"$PY" -m venv .venv
.venv/bin/pip install --quiet -e .

# A symlink in ~/.local/bin means `slurm-mcp login` works from anywhere.
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$HERE/.venv/bin/slurm-mcp" "$BIN/slurm-mcp"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Note: $BIN is not on your PATH. Add this line to ~/.zshrc or ~/.bashrc, then open a new terminal:"
     echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

.venv/bin/slurm-mcp init
echo
echo "Register with Claude Code:"
echo "  claude mcp add slurm -- $BIN/slurm-mcp"
echo "Open the shared SSH connection if needed (from any folder):"
echo "  slurm-mcp login"
