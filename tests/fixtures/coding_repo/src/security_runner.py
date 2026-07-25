from __future__ import annotations

import subprocess


def render_report(report_name: str) -> str:
    """Render a report through the fixture command."""

    completed = subprocess.run(
        f"fixture-report --name {report_name}",
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout
