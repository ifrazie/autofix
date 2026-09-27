---
doc: spec
status: approved
---

# Autofix — Technical Spec

## How This Works, In Plain Language
Autofix is a Python command-line tool with four moving parts, each with one job:

1. **The rule loader** reads a *rule file* — a small YAML file that describes a vulnerable pattern (like "a call to `md5()`"), the fix to apply, and a one-line human "why." Rules are plain data files, never code inside the engine. Adding a new vuln class means writing a new YAML file; the engine never changes. That's the whole idea of the project.

2. **The matcher** turns the rule's pattern description into a *libcst matcher* and walks the target codebase's syntax tree to find every place the pattern occurs. We use **libcst** (not Python's built-in `ast`) because libcst keeps the original formatting, spacing, and comments — so when we later change one line, the diff shows *only* that line, not a reformatted file. Matching is deterministic: the pattern decides what counts, with no AI involved.

3. **The transformer** is where the AI works. For each match, we hand a **local AI model running in Ollama** (via the `strands-agents` SDK) the matched code plus the lines around it, and ask it for the corrected code — adapting the fix to that specific spot. The model returns a *structured result* (the replacement code plus a confidence signal), and **our code** — not the model — splices that replacement back into the file with libcst. If the model is unsure, or the match looks like a false positive (an `md5()` used as a harmless checksum, say), we skip it and record why. Keeping the AI on a short leash — one snippet in, one snippet out — is what makes the result trustworthy and reviewable.

4. **The verifier + reporter** creates a fresh git branch, applies the confident fixes, runs `pytest`, and prints a terminal report: what changed in which file under which rule, each rule's rationale, an inline diff, and summary counts. If tests fail, it names the fix most likely to blame and leaves the branch intact for inspection.

Why this shape: the deterministic work (finding, applying, branching, testing) stays in ordinary Python we can test and trust, and the AI is confined to the one job only a language model can do well — rewriting code in context. That split is what lets a scary phrase like "AI edits your codebase" become "AI proposes one snippet at a time, and you review a clean diff with green tests."

## The Core Journey Through the System
PRD ref: `prd.md > The Core Journey`.

```
  autofix <target> --rule md5.yaml --rule shell.yaml
        │
        ▼
  ┌───────────────┐   local path? use it.
  │ Target loader │   GitHub URL?  git clone into a temp dir.
  └──────┬────────┘
         ▼
  ┌───────────────┐   YAML → validated Rule objects
  │  Rule loader  │   (pattern spec + transform prompt + rationale)
  └──────┬────────┘
         ▼
  ┌───────────────┐   compile pattern → libcst matcher
  │    Matcher    │   walk every .py file → list of match sites
  └──────┬────────┘
         ▼  for each match
  ┌───────────────┐   snippet + context → Ollama (strands-agents)
  │  Transformer  │   → structured {replacement, confident?, reason}
  └──────┬────────┘   confident → apply via libcst | else → skip+record
         ▼
  ┌───────────────┐   git checkout -b autofix/<timestamp>
  │   Verifier    │   write files → commit → run pytest
  └──────┬────────┘
         ▼
  ┌───────────────┐   per-fix breakdown + rationale + inline diff
  │   Reporter    │   + counts (matched/fixed/skipped) + culprit-on-red
  └───────────────┘
```

Step by step: the user runs `autofix` pointing at a repo (local path or GitHub URL) with one or more `--rule` files → the target loader resolves it (cloning if remote) → the rule loader parses and validates each YAML rule → for each rule the matcher compiles its pattern into a libcst matcher and finds every instance → each match goes to the transformer, which asks the local model for a context-aware replacement and either applies it or skips-and-records → the verifier makes a fresh branch, writes the confident fixes, commits, and runs `pytest` → the reporter prints the breakdown, rationales, inline diff, and counts. The user reads the diff, sees green tests, and merges.

## Stack
- **Language: Python 3.10+** — same language as the target code and `pytest`, and native home for libcst and the Strands SDK. (`strands-agents` requires 3.10+.)
- **CLI framework: [Typer](https://typer.tiangolo.com/)** (or stdlib `argparse` if we want zero deps) — a thin layer for `autofix <target> --rule ...`. Typer gives clean help text and arg parsing for little cost; swap to `argparse` if we want to trim dependencies.
- **Syntax matching + editing: [libcst](https://libcst.readthedocs.io/)** — concrete syntax tree that preserves formatting and comments, so edits produce minimal diffs. Its [Matchers API](https://libcst.readthedocs.io/en/latest/matchers_tutorial.html) does the deterministic pattern matching; codemod/transformer visitors apply replacements. *Tradeoff accepted:* heavier API than stdlib `ast`, but stdlib `ast` discards formatting and would wreck the inline-diff proof.
- **AI transformation: [strands-agents SDK](https://github.com/strands-agents/harness-sdk)** (bare SDK, **not** the full harness) with **structured output**, using the **Ollama** provider. *Tradeoff accepted:* a local model is less capable than a frontier API model, so the model choice must be verified early (see Failure Modes); in exchange we get a fully local, no-API-key, no-cost pipeline and the learner's target learning experience. Docs: [Strands docs](https://strandsagents.com/) / repo README quickstart.
- **Local model runtime: [Ollama](https://ollama.com/)**, default model **`deepseek-coder-v2`** (a coding-tuned model), swappable via a flag/env var to test which model handles the rules well.
- **Git operations: `git` via `subprocess`** — branch, add, commit. Simpler and more transparent than adding GitPython; the commands are few and standard.
- **Diff rendering: Python stdlib [`difflib`](https://docs.python.org/3/library/difflib.html)** (unified diff), optionally colorized in the terminal.
- **Rule files: YAML via [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation)** — human-writable, pure data.
- **Testing: [`pytest`](https://docs.pytest.org/)** — both the tool's own tests and the suite run against the target repo.

*Unverified, check early in the build:* the exact `strands-agents` API for (a) selecting the Ollama provider and (b) requesting structured output, and whether `deepseek-coder-v2` reliably produces valid replacements for both shipped rules. Confirm against the current SDK docs in the first build step that touches the transformer.

## Where It Runs and How Someone Tries It
- **Runtime:** local command-line process. No server, no browser, no deployment.
- **Prerequisites:** Python 3.10+, `git`, and **Ollama installed and running** with the target model pulled (`ollama pull deepseek-coder-v2`). The tool talks to Ollama's local endpoint.
- **Install:** `pip install -e .` in the project root (or `pip install -r requirements.txt`).
- **Run (the demo):**
  ```
  ollama serve            # if not already running
  autofix ./sample-repo --rule rules/md5.yaml --rule rules/shell.yaml
  ```
  This finds every planted instance, applies confident fixes on a new `autofix/<timestamp>` branch, runs `pytest`, and prints the report inline.
- **What to record:** one terminal run against the seeded sample repo showing matched/fixed/skipped counts, the inline diff, green tests, and then re-running with only a *different* rule to show the same engine fixing a different vuln class (the "generality" wow beat).
- **Submission:** short demo video + public GitHub repo. No deployment; recording a local run is sufficient (`scope.md > What "Working" Looks Like`).

## Look and Feel
Terminal output only. Tone: precise and calm — a security tool earning trust, not a toy. Use clear section headers per fix, the rule's rationale printed alongside its changes, and a colorized unified diff (green additions / red deletions) with a summary line of counts. On test failure, a distinct, unmissable callout naming the likely-culprit fix. Readable when piped to a file (color degrades gracefully).

## Components

### Target Loader
Resolves the target argument: a local path is used in place; a GitHub URL is `git clone`d into a temp directory. Returns the working directory for the rest of the pipeline.
PRD ref: `prd.md > Product Decisions` ("Target can be a local path or a GitHub URL").

### Rule Loader + Rule Format
Reads each `--rule` YAML file, validates it, and produces a `Rule` object. The declarative schema (this resolves `prd.md > Open Questions`):
```yaml
id: md5-to-sha256
rationale: "md5 is cryptographically broken; use sha256 for hashing."
match:
  call: "md5"          # function/attribute call name to match
  # optional: keyword constraints, e.g.
  # keyword: { shell: true }
transform:
  prompt: >
    Replace this md5() hashing call with sha256(), preserving the
    surrounding call and arguments. If md5 is used as a non-security
    checksum, do not change it.
```
The `match` block is a **small pattern DSL** the matcher compiles into a libcst matcher (PoC supports call-name matching plus optional keyword-argument constraints — enough for both shipped rules and extensible later). `transform.prompt` is the instruction handed to the model; `rationale` is the human "why" that travels into the report.
PRD ref: `prd.md > Interaction Surface` (rule-file shape), `prd.md > Product Decisions` ("Rules live in files, not built into the engine").

### Matcher (deterministic)
Compiles a rule's `match` block into a libcst matcher and walks each `.py` file in the target, collecting match sites (file, node, location). No AI. The matched count is exact.
PRD ref: `prd.md > Features and Behavior > Matching (deterministic)`.

### Transformer (AI, context-aware)
For each match, sends the matched snippet + surrounding context + the rule's `transform.prompt` to the local model through the `strands-agents` SDK, requesting **structured output**: `{ replacement_code, confident: bool, reason }`. If `confident`, our libcst codemod splices `replacement_code` in at the match location; otherwise the match is recorded as skipped with `reason`.
PRD ref: `prd.md > Features and Behavior > Transforming (AI, context-aware)` and `> Skipping ambiguous matches`.

### Verifier
Creates a fresh branch `autofix/<timestamp>`, writes the modified files, commits, and runs `pytest` in the target. Captures pass/fail and, on failure, correlates to the most recently applied fix(es) to name a likely culprit. Leaves the branch intact.
PRD ref: `prd.md > Features and Behavior > Verifying`, `prd.md > States and Boundaries` ("Tests fail after fixes").

### Reporter
Prints the per-fix breakdown (file, location, rule id), each fixed rule's rationale, the inline unified diff, and summary counts (matched / fixed / skipped). Handles the zero-match case with an explicit "nothing matched" message. On red tests, prints the likely-culprit callout.
PRD ref: `prd.md > Output and Readability`, `prd.md > Features and Behavior > Reporting`, `prd.md > States and Boundaries` ("Zero matches").

## Data Model
No database; nothing persists between runs except the git branch the tool creates.

- **Rule** (in memory, from YAML): `id`, `rationale`, `match` spec, `transform.prompt`. Originates in the rule file; loaded at start; discarded at exit.
- **Match** (in memory): `file_path`, `node`, `location`, originating `rule`. Produced by the matcher; consumed by the transformer.
- **FixResult** (in memory): `match`, `status` (fixed / skipped), `replacement_code` or `skip_reason`, `diff`. Produced by the transformer; consumed by the verifier and reporter.
- **The branch** (`autofix/<timestamp>`, on disk in the target repo): the *only* durable output. When the user comes back later, the changes are there as a normal git branch to diff, test, or merge.

## File Structure
```
autofix/
├── src/
│   └── autofix/
│       ├── __init__.py
│       ├── cli.py            # entry point: parse args, orchestrate the pipeline
│       ├── target.py         # Target Loader (local path / git clone)
│       ├── rules.py          # Rule Loader + YAML schema + pattern-DSL model
│       ├── matcher.py        # compile match spec → libcst matcher; walk files
│       ├── transformer.py    # strands-agents + Ollama; structured output; apply via libcst
│       ├── verifier.py       # branch, commit, run pytest, culprit detection
│       └── reporter.py       # per-fix breakdown, rationale, inline diff, counts
├── rules/
│   ├── md5.yaml              # md5() → sha256()
│   └── shell.yaml            # subprocess(..., shell=True) → shell=False
├── sample-repo/             # seeded ~15-file Python repo with planted patterns
│   ├── ...                  #   incl. at least one ambiguous md5() to skip
│   └── tests/               #   pytest suite that must stay green
├── tests/                   # the tool's own unit tests (matcher, rules, etc.)
├── devpost/                 # Devpost learning workspace
├── pyproject.toml           # deps + `autofix` console script entry point
└── README.md
```

## External Services and Dependencies
- **Ollama (local)** — runs on the local machine, no key, no cost, no rate limit. Requires the model pulled (`ollama pull deepseek-coder-v2`). Accessed through the `strands-agents` Ollama provider; the SDK handles the HTTP calls to Ollama's local endpoint. *Verify early:* exact provider-selection and structured-output call in the current SDK version.
- **`strands-agents` SDK** — [github.com/strands-agents/harness-sdk](https://github.com/strands-agents/harness-sdk), Apache 2.0, actively maintained. Bare SDK usage (`Agent` + structured output), not the harness.
- **libcst** — [docs](https://libcst.readthedocs.io/), matcher + codemod APIs.
- **GitHub (optional, only when target is a URL)** — public repos cloned over `git`; no auth for public repos. Private repos are out of scope.

## Important Failure Modes
- **Local model produces invalid or non-compiling replacement code** → our libcst apply step parses the replacement; if it fails to parse, treat the match as *skipped* with a "model output invalid" reason rather than corrupting the file. (This is also the built-in guard if the chosen model is too weak.)
- **Model handles a rule poorly (the real PoC risk)** → because the model is swappable, verify `deepseek-coder-v2` on *both* rules in the first transformer build step; if it stumbles, swap the model — don't discover this at demo time.
- **Ollama not running / model not pulled** → fail fast with a plain message telling the user to start Ollama and `ollama pull <model>`, not a stack trace.
- **Zero matches** → explicit "nothing matched" report, never silent success (`prd.md > States and Boundaries`).
- **Tests fail after fixes** → branch left intact; reporter names the likely-culprit fix (`prd.md > Verifying`).

## What Was Simplified and Why
Carried forward from `scope.md > The POC Boundary` and `prd.md > Deferred From the POC`:

- **Local branch + inline diff + green tests** instead of opening a real **GitHub PR** — proves the loop without adding GitHub auth/setup for little extra "wow." The PR version would need a token and API wiring. (`scope.md > Later`)
- **Handed rules** instead of **self-detecting vulnerabilities** — the kernel is *applying* a known fix generically, not scanning. Detection is a separate, larger problem.
- **Two shipped rules, one language (Python)** instead of a rule library / multi-language — enough to demonstrate generality (swap a rule → fix a different class) at minimum cost.
- **A small call-matcher pattern DSL** instead of a full pattern language — both PoC rules are function calls; the DSL covers them and extends later without becoming a project of its own.
- **Simple/binary fix-vs-skip** instead of graded **confidence scoring** — a reported skip is enough to preserve trust for the PoC.

## Decisions and Open Issues

**Decisions (learner-chosen):**
- **Python 3.10+ CLI** — same language as target + tests; native libcst/Strands support.
- **libcst for matching and editing** — chosen over stdlib `ast` to preserve formatting so diffs are clean; accepted the heavier API.
- **Bare `strands-agents` SDK + structured output over Ollama**, not the full harness — keeps the AI on one job (snippet → replacement) so all deterministic machinery stays testable and the result stays trustworthy.
- **Default model `deepseek-coder-v2`, swappable** — verify model quality per-rule early rather than committing blindly.
- **Declarative YAML rule files + a small pattern DSL compiled to libcst matchers** — the truest expression of the "pluggable data, not hardcoded logic" kernel.
- **Local run + recorded demo, no deployment; Ollama a documented prerequisite.**

**Implementation details (derived, not learner-picked):** Typer vs argparse for the CLI; `subprocess` for git; `difflib` for the diff; the exact `FixResult`/`Match` shapes. Any of these can change during the build without reopening the spec.

**One useful unknown (the learner's stated exploration):** How to drive the **`strands-agents` SDK against a local Ollama model and get reliable structured output** — this is new to the learner and the reason for the tool choice. *How it will be checked:* the first build step that touches `transformer.py` writes a tiny end-to-end call (one snippet in, structured replacement out) against the live SDK + Ollama and confirms it works before the rest of the transformer is built, verifying provider selection, structured-output shape, and that `deepseek-coder-v2` handles both rules.

**Open issues:** None blocking. The only unverified items are the exact current `strands-agents` provider/structured-output API and the chosen model's competence on the two rules — both resolved by the early verification step above, not by more planning.
