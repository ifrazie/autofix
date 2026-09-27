# Autofix

A rule-driven vulnerability remediation engine. You hand it a **rule** (a vulnerable
pattern plus its known-good fix) and a target Python repository; it finds every
instance of the pattern, uses a local AI model to apply a context-aware fix to each,
runs the test suite, and hands back a verified diff on a fresh git branch.

The vulnerability class is **pluggable data, not hardcoded logic**: there is one
engine, and each vuln class is just a YAML rule. Adding a new class means writing a
rule file, never touching the engine.

## Prerequisites

- Python 3.10+
- `git`
- [Ollama](https://ollama.com/) running locally with the model pulled:
  `ollama pull deepseek-coder-v2`

## Install

```
pip install -e .
```

## Run

```
autofix ./sample-repo --rule rules/md5.yaml --rule rules/shell.yaml
```

This finds every planted instance, applies confident fixes on a new
`autofix/<timestamp>` branch, runs `pytest`, and prints an inline report.

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
