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
enterprise-sim eval runs/golden/golden-slice-co-6c66fbef69f8
```

Status: v1 (markdown-only) end-to-end is wired — world → events → corpus → gold
KG, with a reproducible golden run as the acceptance artifact. Office formats and
new modalities arrive as additive producer plugins (PLAN.md M8–M10).

### Run on Amazon Bedrock

Real-backend corpus generation works with only AWS credentials — no
`ANTHROPIC_API_KEY`. The model id in `[model].name` must be a Bedrock
**inference-profile id** (dated, region-scoped), not the 1P name; a 1P id fails
fast at client build with the shape to use.

```bash
uv sync --extra bench                    # anthropic[bedrock] signing stack (boto3)
export AWS_REGION=us-east-1              # plus your usual AWS creds/profile

# config/[model].name must be an inference-profile id, e.g.:
#   [model]
#   name = "us.anthropic.claude-sonnet-4-6-20250929-v1:0"
uv run enterprise-sim run examples/demo.toml --backend bedrock

# One-call live smoke (skips cleanly if no AWS creds are present):
uv run python scripts/bedrock_smoke.py
```

See `docs/DEVELOPMENT.md` for region/profile config, the pricing note, and the
`bench`/`reconstruct` Bedrock flags.
