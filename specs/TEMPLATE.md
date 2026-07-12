# NNNN — Title

Status: draft
Epic: <ROADMAP.md epic this advances, or "standalone">
Owner: <agent/human, optional>

## Why

What problem this solves and for whom. Link the ROADMAP epic and any PLAN.md decisions
(D-numbers) or ARCHITECTURE.md sections (§) that constrain the design.

## What (scope)

- In scope: …
- Out of scope: … (explicitly — scope creep is the failure mode)

## Design sketch

Enough detail that a second agent could implement it without re-deriving the approach:
modules touched, new/changed public surfaces, data-shape changes, migration/compat notes.
Call out anything that touches an invariant from CLAUDE.md (core purity, determinism,
keyless gate, golden-run pin) and how the design preserves it.

## Test & validation plan

- Keyless tests: what proves this deterministically in `./scripts/gate.sh`?
- Keyed/live coverage (if any): how is it gated, and what smoke covers its lazy deps?
- Does the golden run pin change? If yes, plan the regen + docs/GOLDEN_RUN.md update.

## Acceptance criteria

- [ ] Concrete, checkable statements — each one a command or test someone else can run.
- [ ] Docs updated (`docs/*.md`, `CLAUDE.md` if the workflow/deps changed).
- [ ] `./scripts/gate.sh` green on every commit.

## Open questions

Things to resolve during implementation, with the current lean.
