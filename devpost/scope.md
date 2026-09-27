---
doc: scope
status: approved
---

# Autofix — a rule-driven vulnerability remediation engine

One line: A CLI that takes a "vulnerable pattern → known-good fix" rule and applies it consistently across an entire Python codebase, then proves the fix with a diff and green tests.

## The Unique Kernel
The vulnerability class is *pluggable data*, not hardcoded logic. There's one remediation engine, and each vuln class is just a rule (a vulnerable pattern plus its known-good fix). The AI's job is applying that fix everywhere it belongs while handling the messy real-world variations a dumb regex codemod would choke on. Scanners already find problems; this closes the "the fix is obvious but tedious to apply across the whole codebase" gap — and does it generically.

## Who It's For
A developer or security engineer staring at a confirmed finding — `md5()` used for hashing, `subprocess(..., shell=True)`, a raw SQL string — where the fix is genuinely obvious but scattered across a dozen-plus files. Today they either grep-and-hand-edit each site (slow, error-prone) or write a one-off codemod that breaks on the variations. They want the tedium gone without losing trust in the result.

## The Core Loop
Point the tool at a repo and hand it a rule. It finds every instance of the vulnerable pattern across the codebase, applies the correct fix to each, runs the test suite, and produces a reviewable diff on a fresh branch. The user reads the diff, sees the tests pass, and merges — instead of editing fifteen files by hand.

## Why This Matters to the Learner
The annoying moment is real and specific: "the fix is obvious but tedious to apply across a codebase." The learner wants to ship something that actually works end-to-end, and this closes a gap they've personally felt in remediation work.

## What "Working" Looks Like
Point the tool at a sample Python repo seeded with, say, `md5()` calls and `shell=True` subprocess calls scattered across ~15 files. Run one command with two rules loaded. It finds every instance, applies the correct fix to each, runs `pytest`, and produces a clean diff on a new branch showing exactly what changed and why — with green tests.

The "oh, that's cool" beat: one command turns a repo full of scattered vulnerabilities into a verified, test-passing diff you'd actually merge — and swapping in a *different* rule fixes a *different* vuln class with no code changes to the engine. Generality, demonstrated.

## The POC Boundary
- The remediation engine + a simple rule format (vulnerable pattern → fix).
- **Two rules:** `md5()` → `sha256()` and `subprocess(..., shell=True)` → `shell=False`.
- One language: **Python** (cleanest AST and test story).
- Runs against one sample repo seeded with the target patterns.
- Applies fixes, runs `pytest`, commits to a fresh local branch, and produces a readable diff.
- Pure CLI.

## Later
- A **GitHub PR** with the verified diff (nice-to-have; the local branch + diff + green tests are the proof).
- Multi-language support.
- A larger rule library.
- Detecting the vulnerabilities itself, rather than being handed rules.
- False-positive handling / confidence scoring.

## Explicitly Cut
- **Any UI or dashboard** — pure CLI proves the loop; a UI is pure overhead for a PoC.
- **Auth / config management** — no accounts or settings needed to demonstrate the kernel.
- **Real GitHub PR creation (for now)** — adds auth and repo setup for little extra "wow"; a local branch + diff + green tests is sufficient proof. Kept as a Later nice-to-have.
