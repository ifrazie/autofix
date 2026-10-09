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

The tool is organized into subcommands: `fix` runs the remediation pipeline,
`scan` previews matches without a model, `apply` replays a saved plan, `doctor`
checks the environment, and the `rules` group inspects rule files.

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

### Preview, save, and replay

```bash
# 1. Check the environment is ready (Python, git, pytest, and the provider):
autofix doctor --provider bedrock

# 2. Free, model-free preview of what the rules match (no writes, no cost):
autofix scan ./sample-repo --rules-dir rules/

# 3. Propose + verify fixes and save a reusable plan for review:
autofix fix ./sample-repo --rules-dir rules/ --plan-out autofix-plan.json

# 4. Later, replay the EXACT reviewed fixes with no model call. `apply` refuses
#    any file that drifted from the plan, so stale sources are never clobbered:
autofix apply autofix-plan.json
```

A **plan** records the final rewritten source per file plus a hash of the original
it was derived from, so the metered, non-deterministic model step happens once and
can be reviewed and replayed deterministically. See
`examples/github-actions-autofix.yml` for a CI job that runs `doctor`, `scan`, and
`fix --plan-out`, then uploads the report and plan as artifacts.

### `fix` options

| Flag | Purpose |
|------|---------|
| `--rule, -r <file>` | Rule YAML file (repeatable). |
| `--rules-dir <dir>` | Load every `*.yaml`/`*.yml` in a directory (in addition to `--rule`). |
| `--include <glob>` | Only scan files matching this glob, relative to the target (repeatable). |
| `--exclude <glob>` | Skip files matching this glob, relative to the target (repeatable; wins over `--include`). |
| `--provider {bedrock\|ollama}` | Model backend (default `bedrock`). |
| `--model <id>` | Model id (defaults to the provider's default model). |
| `--region <aws-region>` | AWS region (bedrock only). |
| `--host <url>` | Ollama host URL (ollama only). |
| `--concurrency <n>` | Number of matches to transform in parallel. |
| `--timeout <seconds>` | Overall per-match deadline, including retries (bounds wall-clock even on a stalled call). |
| `--max-retries <n>` | Extra attempts after the first for *transient* failures (throttling, 5xx, timeouts). |
| `--dry-run` | Match, transform, and report only — no writes, branch, or tests. |
| `--no-test` | Branch and commit the fixes but skip the pytest step (reported as `skipped`, not a pass). |
| `--json` | Emit a machine-readable JSON report instead of the human report. |
| `--plan-out <file>` | Write a reusable remediation plan (JSON) for later `autofix apply`. |
| `-v/--verbose`, `-q/--quiet` | Increase or suppress diagnostic output. |
| `--version` | Print the version and exit. |

Globs use `fnmatch` semantics where `*` matches across `/`, so `--include 'app/*'`
means "anything under `app/`". `--exclude` is applied after `--include`.

### Matching, conflicts, and coverage

- **Import aliases are resolved.** `import subprocess as sp` makes `sp.run(...)`
  match a `subprocess` rule, and `from hashlib import md5 as h` makes `h(...)`
  match an `md5` rule — while an unrelated object that merely shares a method name
  (e.g. a local `Store.load()` vs. a `yaml.load` rule) is *not* matched.
- **Conflicting fixes are reported, not lost.** If two rules target the same call
  or one nests inside the other, only one edit can land; the other is reported as
  `dropped (conflict)` rather than silently discarded. A fix identical to the
  original is reported as a no-op `skip`.
- **Unreadable files are surfaced.** A file that can't be read or parsed is listed
  as skipped (with a reason) so you know coverage is incomplete, instead of being
  silently treated as "no matches".
- The reviewed diff for each applied fix is regenerated from the final combined
  file, so it reflects every sibling fix and any imports Autofix added.

### Exit codes

`autofix fix` exits non-zero when the run cannot be trusted as a clean success:
any transform error, a verification that ran tests and did not pass (including
"no tests collected"), or a test suite that was already failing before the fix.
`--no-test` and `--dry-run` prove nothing about tests and never report a pass.

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

### A fix must be correct *and* complete

A rule's prompt should only tell the model to be `confident` when a direct,
single-expression rewrite is a correct **and complete** remediation. When it
isn't, the model is instructed to set `confident = false` and say why — the match
is then reported as "matched but left alone" rather than silently rewritten. Two
cases the shipped rules handle this way:

- **Password hashing (`md5.yaml`)** — swapping `md5()` for `sha256()` does *not*
  make password storage safe; a fast hash is unsuitable for passwords. These
  matches are reported as **requiring migration** to a dedicated password hash
  (Argon2id/bcrypt/PBKDF2), not auto-fixed.
- **Shell features (`shell.yaml`)** — a `shell=True` command that uses a pipe,
  redirection, shell expansion, or `&&`/`;` chaining can't be reproduced by an
  `argv` list, so it is matched but **skipped for manual remediation** instead of
  being mangled into a broken `shell=False` call.

## Development

```bash
ruff check .        # lint
mypy                # type-check
pytest -q           # tests (no model backend is called — a FakeAgent stands in)
```

CI runs all three across Python 3.11 / 3.12 / 3.13.
