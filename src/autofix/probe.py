"""Risk probe: prove the AI transform end-to-end.

    python -m autofix.probe [provider] [model]

Defaults to the Bedrock provider (Claude Sonnet 4.5); pass `ollama` to probe the
local backend instead. Feeds one real md5() snippet and one real
subprocess(shell=True) snippet through the strands-agents structured-output path
and prints the structured result for each, confirming the SDK wiring AND that the
chosen model handles both shipped rules.
"""

from __future__ import annotations

import sys

from .transformer import (
    DEFAULT_HOST,
    DEFAULT_PROVIDER,
    DEFAULT_REGION,
    PROVIDERS,
    ProviderError,
    default_model_for,
    make_agent,
    preflight,
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


def main(provider: str = DEFAULT_PROVIDER, model_id: str | None = None) -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in PROVIDERS:
        provider, argv = argv[0], argv[1:]
    if argv:
        model_id = argv[0]
    model_id = model_id or default_model_for(provider)

    print(f"Probing {provider}:{model_id} ...\n")
    try:
        resolved = preflight(provider, model=model_id, host=DEFAULT_HOST, region=DEFAULT_REGION)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if resolved != model_id:
        print(f"(resolved to '{resolved}')\n")
    agent = make_agent(provider, model=resolved, host=DEFAULT_HOST, region=DEFAULT_REGION)

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
