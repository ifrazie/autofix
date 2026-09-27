---
doc: prd
status: approved
---

# Autofix — Product Requirements

One line: A command-line tool for developers and security engineers that takes structured "vulnerability rule" files, finds every matching instance across a Python codebase, uses AI to apply a context-aware fix to each, runs the tests, and hands back a verified diff on a fresh branch.
Source: `scope.md > The Unique Kernel`, `scope.md > The Core Loop`.

## The Core Journey
Develops `scope.md > The Core Loop` and `scope.md > What "Working" Looks Like`.

1. The user runs `autofix` from the terminal, pointing it at a **target repo** (a local path or a GitHub URL it clones) and giving it one or more **rule files**.
2. For each rule, the tool uses the rule's **AST pattern** to find every matching instance across the codebase (the deterministic matcher).
3. For each match, the **AI performs the transformation** using local context — adapting the fix to the surrounding code rather than applying a fixed template.
4. When a match is ambiguous or the fix would be wrong (e.g. `md5()` used as a non-security checksum), the tool **skips it and reports it** as "matched but left alone, here's why."
5. The tool applies the confident fixes on a **fresh branch**, then runs the **test suite (`pytest`)**.
6. The user sees a **per-fix breakdown** with each rule's **rationale**, an **inline diff**, and the **test result**. If tests fail, the tool points at the **fix most likely responsible**.
7. Success: the user reads the diff, sees green tests, and merges — instead of hand-editing every instance.

## Interaction Surface
A single command-line tool. No screens, no UI.

- **Input:** a target (local path or GitHub URL) and one or more rule files.
- **Rule file:** a structured spec containing an **AST pattern** (the matcher), a **transformation** the AI carries out per match, and a short **human-readable rationale** (the security "why").
- **Output:** terminal report (per-fix breakdown, rationale, inline diff, test result) plus a new git branch holding the changes.

## Output and Readability
Develops `scope.md > What "Working" Looks Like`.

- Per-fix breakdown: what was changed, in which file/location, under which rule.
- Each rule's rationale printed alongside its fixes, so the "why" travels with the change.
- The diff is shown **inline** in the terminal (not left for the user to `git diff` themselves).
- Clear summary counts: instances matched, fixed, and skipped.

## Features and Behavior

### Matching (deterministic)
- Given a rule's AST pattern, find every instance across the target Python codebase.
- Matching is deterministic — the AST pattern decides what counts as a match.
  - [ ] Running against the seeded sample repo finds every planted instance of a rule's pattern.
  - [ ] The matched count reported equals the number of planted instances.

### Transforming (AI, context-aware)
- For each confident match, the AI applies the fix, adapting to surrounding code.
  - [ ] `md5()` used for hashing becomes `sha256()` with the surrounding call still valid.
  - [ ] `subprocess(..., shell=True)` becomes `shell=False` with the invocation adjusted as needed.

### Skipping ambiguous matches
- When a fix would be wrong or the context is ambiguous, skip it and report why.
  - [ ] A non-security `md5()` checksum in the sample repo is left unchanged and listed as "matched but left alone," with a reason.

### Verifying
- Apply confident fixes on a fresh branch, then run `pytest`.
  - [ ] Changes land on a new branch, not the working branch.
  - [ ] `pytest` runs automatically after fixes are applied.
  - [ ] On green, the summary shows tests passing.
  - [ ] On red, the tool names the fix most likely responsible.

### Reporting
- Produce the per-fix breakdown, per-rule rationale, inline diff, and summary counts.
  - [ ] Output includes a per-fix entry (file, location, rule).
  - [ ] Each fixed rule's rationale appears in the output.
  - [ ] The diff is visible inline in the terminal.

## States and Boundaries
- **Normal run** — matches found, some fixed, maybe some skipped; branch created, tests run, report shown.
- **Zero matches** — the tool explicitly tells the user nothing matched (no silent success).
- **Ambiguous match** — skipped and reported with a reason; never silently fixed.
- **Tests fail after fixes** — branch is left intact for inspection; tool names the likely-culprit fix.
- **Persistence** — changes live on a new git branch; nothing else is stored between runs.

## Product Decisions
- **Target can be a local path or a GitHub URL** — the tool clones remote repos so it works against real projects, not just local checkouts.
- **Rules live in files, not built into the engine** — adding a vuln class means writing a rule file, never touching the engine. This is the kernel made literal.
- **Rule format is a structured spec: AST pattern + transformation + rationale** — the AST pattern gives deterministic, trustworthy matching; the AI handles the context-sensitive transformation.
- **Division of labor: AST matches, AI transforms** — deterministic where it can be, intelligent where it must be.
- **Skip rather than force ambiguous fixes** — trust matters more than coverage; a reported skip beats a wrong rewrite.
- **Rationale travels into the output** — every fix carries its security "why."
- **Diff shown inline** — the proof is in front of the user, not a separate command away.

## What We're Building
- CLI that accepts a local path or GitHub URL plus one or more rule files.
- Rule-file format: AST pattern, transformation, rationale.
- Deterministic AST matcher across a Python codebase.
- AI transformation per confident match, with skip-and-report for ambiguous ones.
- Two shipped example rules: `md5()` → `sha256()`, `subprocess(..., shell=True)` → `shell=False`.
- A seeded sample Python repo (~15 files) including at least one ambiguous case to skip.
- Fix application on a fresh branch, automatic `pytest` run.
- Terminal report: per-fix breakdown, per-rule rationale, inline diff, summary counts, and likely-culprit identification on test failure.

## Deferred From the POC
- **GitHub PR creation** — the local branch + inline diff + green tests are sufficient proof; the PR adds auth/setup for little extra "wow." (`scope.md > Later`)
- **Self-detection of vulnerabilities** — the tool is handed rules; it doesn't discover vuln classes on its own yet.
- **Confidence scoring beyond fix/skip** — skips are reported, but there's no graded confidence output.

## Possible Later Enhancements
- Multi-language support beyond Python.
- A larger library of shipped rules.
- Opening a real GitHub PR with the verified diff.

## Non-Goals
- **No UI or dashboard** — pure CLI proves the loop. (`scope.md > Explicitly Cut`)
- **No auth or config management** — nothing needed to demonstrate the kernel. (`scope.md > Explicitly Cut`)
- **Not a scanner** — it doesn't find vulnerabilities to fix; it applies rules it's given.

## Open Questions
- The exact schema of the rule file (field names, how the AST pattern is expressed) — can wait for `4-spec`; the product-level shape (AST pattern + transformation + rationale) is settled.
