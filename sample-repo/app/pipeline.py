"""Log maintenance tasks.

VULNERABLE: shell=True, but the command uses a shell pipe (``|``), which an
argv list with shell=False cannot reproduce. Autofix should MATCH this call but
SKIP the automatic fix, reporting that it needs manual remediation.
"""

import subprocess


def count_errors(logfile: str) -> int:
    """Count ERROR lines in a log via a shell pipeline."""
    proc = subprocess.run(
        "grep ERROR " + logfile + " | wc -l",
        shell=True,
        capture_output=True,
        text=True,
    )
    return int(proc.stdout.strip() or 0)
