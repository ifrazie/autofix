"""Miscellaneous maintenance tasks."""

import subprocess


def disk_usage(mount: str) -> int:
    """Report disk usage for a mount point. VULNERABLE: shell=True."""
    return subprocess.call("df -h " + mount, shell=True)
