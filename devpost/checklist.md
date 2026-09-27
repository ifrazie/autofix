---
doc: checklist
status: approved
---

# Build Checklist

Build mode: fast

## Slices

- [x] **1. Point autofix at the sample repo with a rule and see every vulnerable instance found**
  Becomes usable: A working `autofix ./sample-repo --rule rules/md5.yaml` command that resolves the target, loads and validates the YAML rule, walks every `.py` file, and prints each matched site (file, location, rule id) plus a matched count. No fixes yet.
  Why now: Delivers the deterministic half of the kernel — pluggable rule data driving a real match across a codebase — on slice one, so every later slice has a target list to act on. Bootstrapping (scaffold, deps, console script, seeded sample repo) lands here as part of the first real behavior.
  PRD ref: `prd.md > The Core Journey` (steps 1-2), `prd.md > Features and Behavior > Matching (deterministic)`
  Spec ref: `spec.md > Components` (Target Loader, Rule Loader + Rule Format, Matcher), `spec.md > File Structure`, `spec.md > Data Model` (Rule, Match)
  Build: Scaffold per the spec's file structure (`pyproject.toml` with the `autofix` console script, `src/autofix/`), install deps (libcst, PyYAML, Typer/argparse). Implement the target loader (local path; git-clone for URL), rule loader + YAML schema + the small call-matching pattern DSL, and the matcher compiling the DSL to a libcst matcher across `.py` files. Seed `sample-repo/` (~15 files) with planted `md5()` and `subprocess(..., shell=True)` calls plus at least one ambiguous non-security `md5()` checksum, and a passing pytest suite. Print a plain per-match listing + matched count.
  Verify (mechanical): Run `autofix ./sample-repo --rule rules/md5.yaml`; confirm the matched count equals the number of planted `md5()` instances. Run the tool's own unit tests for the rule loader and matcher and confirm green. Confirm `sample-repo`'s own `pytest` suite passes on its untouched state.
  Learner check: Run the command against the sample repo and confirm the instances it lists are exactly the ones you planted.
  Commit: `Add scaffold, rule loader, matcher, and seeded sample repo`

- [ ] **2. Prove the local-AI transform works end-to-end (risk probe)**
  Becomes usable: A small runnable script/subcommand that sends one matched snippet + context and the rule's transform prompt to the local model through `strands-agents` over Ollama, and prints the structured `{ replacement_code, confident, reason }` result — confirmed for BOTH shipped rules (`md5`, `shell`).
  Why now: This is the project's one real unknown (the learner's stated exploration) and the biggest demo risk. It is a layer-only slice justified as risk-proof-with-runnable-evidence: it confirms provider selection, structured-output shape, and that `deepseek-coder-v2` actually handles both rules — before we invest in the full transformer. If the model stumbles, we swap it here, not at demo time.
  PRD ref: `prd.md > Features and Behavior > Transforming (AI, context-aware)`
  Spec ref: `spec.md > Stack` (strands-agents + Ollama, unverified items), `spec.md > Components` (Transformer), `spec.md > Decisions and Open Issues` (the one useful unknown), `spec.md > Important Failure Modes` (Ollama not running; model handles rule poorly)
  Build: Wire the `strands-agents` Ollama provider with structured output against the current SDK API. Write a tiny end-to-end probe that feeds a real `md5()` snippet and a real `shell=True` snippet with surrounding context and each rule's prompt, and returns the structured result. Add fail-fast handling when Ollama isn't running / the model isn't pulled (plain message, not a stack trace).
  Verify (mechanical): Run the probe with Ollama serving `deepseek-coder-v2`; confirm it returns a valid structured object for the `md5` snippet (a plausible `sha256` replacement, `confident: true`) and for the `shell=True` snippet (`shell=False`, `confident: true`). Confirm the fail-fast path prints a clear message when Ollama is down.
  Learner check: Run the probe yourself with Ollama running and confirm the model returns sensible replacement code for both rules; tell me if either looks off (this is where we'd change the model).
  Commit: `Verify strands-agents + Ollama structured transform on both rules`

- [ ] **3. Autofix rewrites confident matches and skips ambiguous ones**
  Becomes usable: For each match found in slice 1, the transformer asks the model for a replacement and — via libcst — splices confident replacements back into the file in memory, while ambiguous/low-confidence matches (the non-security checksum) are left unchanged and recorded as "matched but left alone, here's why." Per-match fixed/skipped status is printed.
  Why now: Completes the transform half of the kernel on top of the proven probe, turning found matches into applied edits with trust guarded by skip-and-report.
  PRD ref: `prd.md > Features and Behavior > Transforming (AI, context-aware)`, `> Skipping ambiguous matches`
  Spec ref: `spec.md > Components` (Transformer), `spec.md > Data Model` (FixResult), `spec.md > Important Failure Modes` (invalid replacement → skip)
  Build: Build the full transformer over the matcher's sites: per match, call the model, and if `confident` splice `replacement_code` via a libcst codemod at the match location; otherwise record a skip with the reason. Parse the replacement before applying — on parse failure, skip with "model output invalid" rather than corrupting the file. Produce `FixResult`s (fixed/skipped, replacement or reason).
  Verify (mechanical): Run against the sample repo with both rules; confirm `md5()` hashing sites become `sha256()`, `shell=True` becomes `shell=False`, and the ambiguous checksum is skipped with a stated reason. Confirm an intentionally un-parseable replacement is skipped, not applied. Confirm modified files still parse.
  Learner check: Run it and confirm the fixes applied are the ones you'd expect — and that the harmless checksum was correctly left alone.
  Commit: `Apply confident fixes via libcst, skip and report ambiguous matches`

- [ ] **4. Fixes land on a fresh branch with pytest run automatically**
  Becomes usable: The confident fixes are written to disk on a new `autofix/<timestamp>` branch, committed, and `pytest` runs automatically in the target. Green shows tests passing; red leaves the branch intact and names the fix most likely responsible.
  Why now: This is the "verified" promise of the kernel — the deterministic, trustworthy proof around the AI edits. It's the first place file-writing + git + tests interact, so proving it before the full report keeps the risk contained.
  PRD ref: `prd.md > Features and Behavior > Verifying`, `prd.md > States and Boundaries` (tests fail after fixes)
  Spec ref: `spec.md > Components` (Verifier), `spec.md > Data Model` (the branch), `spec.md > Important Failure Modes` (tests fail after fixes)
  Build: Implement the verifier: `git checkout -b autofix/<timestamp>`, write modified files, commit, run `pytest` in the target via subprocess, capture pass/fail. On failure, correlate to the most recently applied fix(es) to name a likely culprit. Leave the branch intact and the working branch untouched.
  Verify (mechanical): Run against the sample repo; confirm a new `autofix/<timestamp>` branch exists with the fixes committed, the working branch is unchanged, and `pytest` ran with a captured green result. Temporarily plant a fix that breaks a test and confirm red is reported with a named likely-culprit and the branch preserved.
  Learner check: Run it, then `git branch` and `git diff` the new branch yourself to confirm the changes are there and tests passed.
  Commit: `Add verifier: fresh branch, commit, automatic pytest, culprit-on-red`

- [ ] **5. The full report and the generality demo**
  Becomes usable: A complete, calm terminal report — per-fix breakdown (file/location/rule), each fixed rule's rationale printed alongside its changes, a colorized inline unified diff, and summary counts (matched/fixed/skipped) — plus the explicit zero-match message and the unmissable likely-culprit callout on red. Running with only a *different* single rule fixes a *different* vuln class with no engine changes: the "generality" wow beat.
  Why now: Completes the core journey and makes the proof reviewable in front of the user (`scope.md > What "Working" Looks Like`). Ships last because it presents everything the earlier slices produced.
  PRD ref: `prd.md > Output and Readability`, `prd.md > Features and Behavior > Reporting`, `prd.md > States and Boundaries` (zero matches), `prd.md > The Core Journey` (steps 6-7)
  Spec ref: `spec.md > Components` (Reporter), `spec.md > Look and Feel`
  Build: Implement the reporter: per-fix entries, per-rule rationale, `difflib` unified diff colorized in the terminal (degrading gracefully when piped), summary counts, the explicit "nothing matched" path, and the distinct culprit callout on red. Wire it as the final stage of the CLI pipeline.
  Verify (mechanical): Full run against the sample repo with both rules shows the complete report inline with correct counts and green tests; a run with only `rules/shell.yaml` fixes only the shell class (generality); a run with a rule that matches nothing prints the explicit zero-match message; piping to a file degrades color gracefully.
  Learner check: Run the full demo, then re-run with only the other rule, and confirm the same engine cleanly fixes a different vuln class — the beat you'll want on camera.
  Commit: `Add full terminal report, zero-match and culprit callouts, generality demo`

## Hands-on Checkpoints

- [ ] Early usable behavior explored — after Slice 2 (the AI-transform probe), where feedback on model behavior can still change the model choice and shape slices 3-5
- [ ] Final kick-the-tires exploration and feedback completed — after Slice 5, running the full demo including the generality beat

## Final Review

- [ ] Final review complete — feedback resolved and learner confirms ready to ship

## Code Tour and App Map

- [ ] Learning activity complete — guided route, focused alternative, prior practice connected, or brief recap
- [ ] Optional edit and transfer reflection addressed — offered/declined/already covered/not applicable as appropriate
- [ ] `devpost/app-map.html` generated from finished code, checked, and shown, including a project-grounded practice to reuse

Activity and evidence: [pending]
Route and stops: [pending]
Edit outcome: [pending]
Reflection: [pending]
Activity mode: [pending]

## Revisions
