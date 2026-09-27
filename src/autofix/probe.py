"""Risk probe: prove the local-AI transform end-to-end.

Run with Ollama serving the model:

    python -m autofix.probe

Feeds one real md5() snippet and one real subprocess(shell=True) snippet through
the strands-agents + Ollama structured-output path and prints the structured
result for each, so we can confirm the SDK wiring AND that the chosen model
handles both shipped rules *before* building the full transformer.
"""

from __future__ import annotations

import sys

from .transformer import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    OllamaError,
    build_agent,
    check_ollama,
    transform_match,
)

MD5_PROMPT = (
    "Replace this md5() hashing call with sha256(), preserving the surrounding "
    "call and arguments. If md5 is used as a non-security checksum, do not change it."
)
SHELL_PROMPT = (
    "Rewrite this subprocess call to use shell=False by passing the command as a "
    "list of arguments, preserving other keyword arguments."
)

CASES = [
    (
        "md5 (security hashing)",
        MD5_PROMPT,
        'hashlib.md5((salt + password).encode("utf-8"))',
        "def hash_password(password, salt):\n    return hashlib.md5((salt + password).encode('utf-8')).hexdigest()",
    ),
    (
        "subprocess shell=True",
        SHELL_PROMPT,
        'subprocess.run("systemctl restart " + name, shell=True)',
        "def restart_service(name):\n    result = subprocess.run('systemctl restart ' + name, shell=True)\n    return result.returncode",
    ),
]


def main(model_id: str = DEFAULT_MODEL, host: str = DEFAULT_HOST) -> int:
    if len(sys.argv) > 1:
        model_id = sys.argv[1]

    print(f"Probing model '{model_id}' at {host} ...\n")
    try:
        resolved = check_ollama(model_id, host)
    except OllamaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if resolved != model_id:
        print(f"(resolved to installed tag '{resolved}')\n")
    agent = build_agent(resolved, host)

    for label, prompt, snippet, context in CASES:
        print(f"--- {label} ---")
        print(f"  in:  {snippet}")
        result = transform_match(agent, prompt, snippet, context)
        print(f"  out: {result.replacement_code}")
        print(f"  confident: {result.confident}")
        print(f"  reason: {result.reason}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
