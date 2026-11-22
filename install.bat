@echo off
rem Windows: creates a venv, installs slurm-mcp, runs the config setup.
rem Note: SSH connection multiplexing is not available on Windows OpenSSH.
cd /d "%~dp0"
python -m venv .venv || exit /b 1
.venv\Scripts\pip install --quiet -e . || exit /b 1
.venv\Scripts\slurm-mcp init
echo.
echo Register with Claude Code:
echo   claude mcp add slurm -- "%cd%\.venv\Scripts\slurm-mcp.exe"
