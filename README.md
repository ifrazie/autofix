# Autofix

A rule-driven vulnerability remediation engine. You hand it a **rule** (a vulnerable
pattern plus its known-good fix) and a target Python repository; it finds every
instance of the pattern, uses an AI model to apply a context-aware fix to each,
runs the test suite, and hands back a verified diff on a fresh git branch.

The vulnerability class is **pluggable data, not hardcoded logic**: there is one
engine, and each vuln class is just a YAML rule. Adding a new class means writing a
rule file, never touching the engine.

## Prerequisites

- Python 3.11+
- `git`
- A model provider (pick one):
  - **AWS Bedrock (default):** AWS credentials with access to Claude Sonnet 4.5 in
    `us-east-1` (`aws configure`, env vars, or an instance role).
  - **Ollama (local, `--provider ollama`):** [Ollama](https://ollama.com/) running
    locally with the model pulled: `ollama pull deepseek-coder-v2`.

## Install

```bash
pip install -e .

# With dev tooling (ruff, mypy, pytest):
pip install -e ".[dev]"
```

## Run

The tool is organized into subcommands. `fix` runs the remediation pipeline; the
`rules` group inspects rule files without touching any code.

```bash
# Default: AWS Bedrock (Claude Sonnet 4.5)
autofix fix ./sample-repo --rule rules/md5.yaml --rule rules/shell.yaml

# Load every rule in a directory instead of listing them one by one:
autofix fix ./sample-repo --rules-dir rules/

# Fully local instead:
ollama serve
autofix fix ./sample-repo --provider ollama --rule rules/md5.yaml
```

`fix` finds every planted instance, applies confident fixes on a new
`autofix/<timestamp>` branch, runs `pytest`, and prints an inline report.

### `fix` options

| Flag | Purpose |
|------|---------|
| `--rule, -r <file>` | Rule YAML file (repeatable). |
| `--rules-dir <dir>` | Load every `*.yaml`/`*.yml` in a directory (in addition to `--rule`). |
| `--provider {bedrock\|ollama}` | Model backend (default `bedrock`). |
| `--model <id>` | Model id (defaults to the provider's default model). |
| `--region <aws-region>` | AWS region (bedrock only). |
| `--host <url>` | Ollama host URL (ollama only). |
| `--concurrency <n>` | Number of matches to transform in parallel. |
| `--timeout <seconds>` | Overall per-match budget, including retries. |
| `--max-retries <n>` | Extra attempts after the first for transient failures. |
| `--dry-run` | Match, transform, and report only — no writes, branch, or tests. |
| `--no-test` | Branch and commit the fixes but skip the pytest step. |
| `--json` | Emit a machine-readable JSON report instead of the human report. |
| `-v/--verbose`, `-q/--quiet` | Increase or suppress diagnostic output. |
| `--version` | Print the version and exit. |

### Inspecting rules

```bash
autofix rules list --rules-dir rules/       # list rule ids + rationale
autofix rules validate --rules-dir rules/   # schema-check rules without running a fix
```

## Configuration

Defaults can come from a config file so you don't repeat flags every run. Autofix
reads `.autofix.toml` (a top-level table) or a `[tool.autofix]` table in
`pyproject.toml`, searched from the current directory upward. Precedence, highest
first:

**CLI flag > `AUTOFIX_*` env var > config file > built-in default**

```toml
# .autofix.toml
provider = "ollama"
rules_dir = "rules"
concurrency = 8
```

Recognized keys: `provider`, `model`, `region`, `host`, `rules_dir`,
`concurrency`, `timeout`, `max_retries`. An explicit flag always wins over the
config, so `autofix fix . --provider bedrock` overrides the file above.

## Rule format

```yaml
id: md5-to-sha256
rationale: "MD5 is cryptographically broken; use SHA-256 for hashing."
match:
  call: "md5"                 # bare name matches md5(...) or hashlib.md5(...);
                              # a dotted name like "yaml.load" matches that path exactly
  # keyword: { shell: true }  # optional keyword-argument constraint
  # args: ["md5", null]       # optional positional-arg literals (null = any value here)
transform:
  prompt: "Replace this md5() hashing call with sha256(), preserving ..."
  # ensure_import: "import secrets"   # import(s) to add when a fix applies (added iff missing)
```

`ensure_import` accepts a single string or a list; it lets a fix add the import it
needs (e.g. `random` → `secrets`) so the rewritten file stays runnable.

## Development

```bash
ruff check .        # lint
mypy                # type-check
pytest -q           # tests (no model backend is called — a FakeAgent stands in)
```

CI runs all three across Python 3.11 / 3.12 / 3.13.
