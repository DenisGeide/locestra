from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_SCRIPT = ROOT / "scripts" / "process-ownership.ps1"


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def test_root_identity_rejects_sibling_prefix_collision():
    script = f"""
. {_ps_literal(str(OWNERSHIP_SCRIPT))}
$root = 'C:\\work\\local_agent'
$exact = "C:\\work\\local_agent\\.venv\\Scripts\\python.exe`nuvicorn services.gateway.app:app"
$sibling = "C:\\work\\local_agent_evil\\.venv\\Scripts\\python.exe`nuvicorn services.gateway.app:app"
if (-not (Test-OwnershipRootInIdentityText $exact $root)) {{ exit 11 }}
if (Test-OwnershipRootInIdentityText $sibling $root) {{ exit 12 }}
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
