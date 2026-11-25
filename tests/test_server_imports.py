"""A running server must not import slurm-mcp code while a tool runs.

A tool that imports a module on its first call loads the NEW file from disk after a `git pull`,
next to the OLD modules that are already in memory. The mix fails with an error such as a missing
attribute. This happened with `check_setup` after an update without a restart. All imports happen
when the server starts, so a running server keeps one consistent version.
"""
import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "slurm_mcp"


def test_importing_the_server_loads_every_module_a_tool_needs():
    code = "import sys, slurm_mcp.server; print(sorted(m for m in ('slurm_mcp.doctor','slurm_mcp.cluster','slurm_mcp.config') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(SRC.parent)}, check=True).stdout
    assert out.strip() == "['slurm_mcp.cluster', 'slurm_mcp.config', 'slurm_mcp.doctor']"


def test_no_module_imports_inside_a_function_except_the_command_line_entry_points():
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name == "__main__.py":                 # a short-lived process: it imports and exits
            continue
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
    assert offenders == []
