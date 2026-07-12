# specs/ — the feature workflow

This repo is developed by coding agents (Claude Code) and humans working directly against
the repo — there is no external orchestrator or issue tracker. Everything an agent needs
to pick up work lives here.

## The loop

1. **Direction** — [`ROADMAP.md`](./ROADMAP.md) holds the epic-level feature requests,
   ordered by priority, each with enough context to start a spec. When asked to "work on
   the next thing," take the top epic that isn't `done` or explicitly claimed.
2. **Spec** — before multi-session work, copy [`TEMPLATE.md`](./TEMPLATE.md) to
   `specs/NNNN-slug.md` (next free number) and fill it in: the why, a design sketch that
   names the modules it touches, and testable acceptance criteria. Commit the spec first
   (or in the PR's first commit) so the intent is reviewable separately from the code.
   Small fixes and doc changes don't need a spec.
3. **Implement in gate-green slices** — every commit passes `./scripts/gate.sh`
   (see [`CLAUDE.md`](../CLAUDE.md), "The one rule"). Prefer several reviewable
   conventional-commit slices (`feat(scope): …`) over one megacommit.
4. **Close the loop** — when acceptance criteria hold, flip the spec's `Status:` header to
   `done`, update `ROADMAP.md`, and update any affected docs (`docs/*.md`, `CLAUDE.md`,
   `README.md`) in the same PR. A spec is done when its criteria are demonstrable by a
   command someone else can run (a test, the golden run, an eval script) — not when the
   code merely exists.

## Spec statuses

`draft` → `approved` → `in-progress` → `done` (or `parked` with a one-line reason).
The status lives in the spec's header block so `grep -r "^Status:" specs/` is a live
dashboard.

## What belongs where

| Question | Lives in |
|---|---|
| Why does the project exist; what was decided and why (D1–D33) | `PLAN.md` |
| How is the system designed (components, §-references) | `ARCHITECTURE.md` |
| What should we build next, at epic level | `specs/ROADMAP.md` |
| How exactly will feature X work, and when is it done | `specs/NNNN-*.md` |
| How a subsystem behaves today (user-facing) | `docs/*.md` |

Decisions that constrain the whole codebase (a D-number) still get recorded in `PLAN.md`
§2; a spec that introduces one should add it there when it lands.

## History note

Code comments and old commit messages reference `esim-*` ids from a retired external
tracker (beads/gastown). They are historical breadcrumbs only; do not create new ones.
