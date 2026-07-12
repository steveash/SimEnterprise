# Enterprise Sim

Generate a fake-but-realistic enterprise organization and all the office-work artifacts it
would produce over a configurable time window — business goals, departments, programs and
scenarios of work, teams, people (with job descriptions, locations, calendars), projects,
and the documents/presentations/schedules those people create, in native formats
(`.docx`, `.pptx`, `.md`, `.json`) — plus the **ground-truth knowledge graph** those
artifacts encode, as a labeled answer key for KG/RAG/search eval.

**Start here:**
- [`PLAN.md`](./PLAN.md) — vision, decisions, and milestones.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — detailed component & plugin design.
- [`docs/GOLDEN_RUN.md`](./docs/GOLDEN_RUN.md) — the v1 end-to-end **golden run** and how its gold KG acts as an answer key.
- [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) — setup and the dev loop (contributors & agents: also [`CLAUDE.md`](./CLAUDE.md)).
- [`specs/ROADMAP.md`](./specs/ROADMAP.md) — what's next, at epic level.

## Quickstart

```bash
# Render the v1 golden run: a markdown corpus + gold knowledge graph,
# deterministic and network-free (default `fake` backend).
enterprise-sim run examples/golden.toml
enterprise-sim eval runs/golden/golden-slice-co-40644d551158
```

Status: v1 (markdown-only) end-to-end is wired — world → events → corpus → gold
KG, with a reproducible golden run as the acceptance artifact. Office formats and
new modalities arrive as additive producer plugins (PLAN.md M8–M10).
