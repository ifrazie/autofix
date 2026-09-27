"""Deployment helpers."""

import subprocess


def restart_service(name: str) -> int:
    """Restart a systemd service. VULNERABLE: shell=True with interpolation."""
    result = subprocess.run("systemctl restart " + name, shell=True)
    return result.returncode
