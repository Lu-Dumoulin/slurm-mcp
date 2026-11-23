@echo off
rem Windows: creates a venv, installs slurm-mcp, runs the config setup.
rem OpenSSH on Windows cannot share connections. slurm-mcp turns that off by itself (ssh_multiplexing = auto).
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer was not found. Install it, then run this script again.
  exit /b 1
)
python -c "import sys; sys.exit(sys.version_info < (3, 11))"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  exit /b 1
)
python -m venv .venv || exit /b 1
.venv\Scripts\python -m pip install --quiet -e . || exit /b 1
.venv\Scripts\slurm-mcp init
echo.
echo Register with Claude Code:
echo   claude mcp add slurm -- "%cd%\.venv\Scripts\slurm-mcp.exe"
echo Each call opens its own SSH connection here, so your key must work without a prompt.
echo Load it with ssh-add, then test it:
echo   "%cd%\.venv\Scripts\slurm-mcp.exe" check
