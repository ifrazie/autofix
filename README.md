# Autofix

A rule-driven vulnerability remediation engine. You hand it a **rule** (a vulnerable
pattern plus its known-good fix) and a target Python repository; it finds every
instance of the pattern, uses an AI model to apply a context-aware fix to each,
runs the test suite, and hands back a verified diff on a fresh git branch.

The vulnerability class is **pluggable data, not hardcoded logic**: there is one
engine, and each vuln class is just a YAML rule. Adding a new class means writing a
rule file, never touching the engine.

## Prerequisites

- Python 3.10+
- `git`
- A model provider (pick one):
  - **AWS Bedrock (default):** AWS credentials with access to Claude Sonnet 4.5 in
    `us-east-1` (`aws configure`, env vars, or an instance role).
  - **Ollama (local, `--provider ollama`):** [Ollama](https://ollama.com/) running
    locally with the model pulled: `ollama pull deepseek-coder-v2`.

## Install

```
pip install -e .
```

## Run

```
# Default: AWS Bedrock (Claude Sonnet 4.5)
autofix ./sample-repo --rule rules/md5.yaml --rule rules/shell.yaml

# Fully local instead:
ollama serve
autofix ./sample-repo --provider ollama --rule rules/md5.yaml --rule rules/shell.yaml
```

This finds every planted instance, applies confident fixes on a new
`autofix/<timestamp>` branch, runs `pytest`, and prints an inline report.

Flags: `--provider {bedrock|ollama}`, `--model <id>`, `--region <aws-region>`
(bedrock), `--host <url>` (ollama).

## Rule format

```yaml
id: md5-to-sha256
rationale: "MD5 is cryptographically broken; use SHA-256 for hashing."
match:
  call: "md5"                # function/attribute call name to match
  # keyword: { shell: true } # optional keyword-argument constraint
transform:
  prompt: "Replace this md5() hashing call with sha256(), preserving ..."
```
