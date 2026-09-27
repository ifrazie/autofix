"""Backup tasks."""

import subprocess


def snapshot(path: str) -> str:
    """Take a tar snapshot of a directory. VULNERABLE: shell=True."""
    proc = subprocess.Popen("tar -czf backup.tgz " + path, shell=True)
    proc.wait()
    return "backup.tgz"
