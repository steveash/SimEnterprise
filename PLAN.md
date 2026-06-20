# Enterprise Sim — Design Plan

> Status: **DRAFT for review** (synthesized by Mayor from initial requirements, 2026-06-20).
> Owner-to-be: crew **steve**. This document is the starting point for the build; treat
> every "Open question" as a decision Steve confirms before that part is implemented.

---

## 1. What we're building

**Enterprise Sim** generates a fake-but-realistic enterprise organization and *all the
office-work artifacts that organization would produce* over a configurable time window.

A user describes a company at a high level; the simulator invents everything else — the
departments, teams, people (with job descriptions, locations, calendars), the projects
each team is running, and then the **stream of work artifacts** those people would create
during the simulated period (status reports, presentations, schedules, meeting calendars,
collaborative documents with comment threads, etc.).

The output of a run is one large, self-contained **directory hierarchy** containing:
- **Reference data** — markdown files describing every department, team, person, and project.
- **Work artifacts** — the actual files those people "produced" during the time window,
  in their native formats (`.docx`, `.pptx`, `.md`, `.json`).

Think: "SimCity / Sims, but for an enterprise org and its document exhaust."

### Primary use cases (why this is valuable)
- Generating realistic corpora for testing/eval (search, RAG, DLP, e-discovery, SIEM,
  org-graph tools, document-intelligence pipelines) without using real, sensitive data.
- Demos and sandboxes that need a plausible company with internally-consistent people,
  projects, and documents.

---

## 2. Inputs (simulation configuration)

The user provides a small config; the simulator derives everything else.

| Input | Description | Example |
|---|---|---|
| `company.size` | How big the company is — headcount or a t-shirt size that maps to a headcount range | `500`, or `large` |
| `company.vertical` | Industry vertical | `fintech`, `healthcare`, `aerospace` |
| `company.name` | Optional; generated if omitted | `Northwind Financial` |
| `projects.count` / `projects.description` | How many concurrent projects and what *kind* of work they are | `12`, "agile software + a SOX compliance initiative" |
| `simulation.period` | Length of the simulated window (or explicit start/end dates) | `1 week`, `1 month` |
| `seed` | RNG seed for reproducible runs | `12345` |
| `output_dir` | Where the hierarchy is written | `./output` |
| `model` / cost knobs | Which LLM + a realism/cost dial (see §7) | `claude-sonnet-4-6` |

Proposed config format: a single `config.yaml` (or JSON). A schema + validation is M1.

---

## 3. Output (the deliverable of a run)

```
output/<run-id>/
  manifest.json                      # everything generated, with metadata + provenance
  config.snapshot.yaml               # exact config + seed used (reproducibility)
  organization/                      # REFERENCE DATA — small markdown files
    company.md
    locations/<location>.md
    departments/<dept-slug>/
      department.md
      teams/<team-slug>/team.md
    people/<person-slug>.md          # job description, title, location, manager, contact, team
    projects/<project-slug>/project.md
  calendars/
    <person-slug>.md | .json         # per-person schedule of meetings over the window
  artifacts/                         # WORK ARTIFACTS — native formats, time-stamped
    <project-slug>/
      status-reports/2026-06-15-weekly-status.docx     # .docx, with comment threads
      presentations/q3-kickoff.pptx                     # .pptx
      schedules/sprint-3.md | .json
      documents/<doc>.docx
      meetings/<meeting>.md                             # notes/agendas where relevant
```

### Artifact format rules (from requirements)
- **Entity descriptions** (departments / teams / people / projects): plain **markdown**.
- **Schedules / calendars**: **markdown or JSON** with structured scheduling info.
- **Documents** (e.g. status reports, design docs): real **Microsoft Word `.docx`**.
  - If the document was *collaborated on*, it must contain **comments and threaded
    replies**, each authored by a **real person who exists in this company**, with
    timestamps inside the simulation window.
- **Presentations**: real **PowerPoint `.pptx`** with actual slides.

---

## 4. Domain model (entities & relationships)

```
Company 1───* Location
Company 1───* Department 1───* Team 1───* Person
Person  *───1 Team,  *───1 Location,  *───1 Manager(Person)
Department / Team 1───* Project
Project *───* Person (members)
Person  1───* CalendarEvent (meetings)  *───* Project
Artifact *───1 Project,  *───* Person (authors / commenters)
```

Each entity carries enough attributes to make downstream artifacts consistent:
- **Company**: name, vertical, size, description, founding/era, locations.
- **Location**: city, country, timezone, office type (HQ / satellite / remote-hub).
- **Department**: name, function, head (Person), charter.
- **Team**: name, department, charter, members, lead.
- **Person**: name, title, **job description**, department/team, **location**,
  manager, email, seniority, working hours (derived from location timezone).
- **Project**: name, owning team/department, description, status, timeline
  (start/end/milestones), members, expected artifact cadence.
- **CalendarEvent / Meeting**: title, attendees, time (tz-aware), recurrence,
  linked project, kind (standup / 1:1 / review / all-hands).

**Internal-consistency invariants** (these are the hard part and the whole point):
- Every name that appears in any document/comment/meeting maps to a real `Person`.
- Comment authors and meeting attendees are real people on the relevant team/project.
- Timestamps fall inside the simulation window and within the author's working hours/tz.
- A person's calendar, the meetings they attend, and the artifacts they author are mutually
  consistent (you don't author a status report during an all-hands you're attending).

---

## 5. The simulation pipeline

A run proceeds in phases. Phases 1–3 build the *world*; phase 4 decides *what happens*;
phase 5 *renders* it; phase 6 *assembles* output.

**Phase 0 — Configure & seed.** Parse + validate config; fix the RNG seed; resolve the
date window and working calendar (holidays/weekends optional).

**Phase 1 — Company skeleton.** Generate company → locations → departments → teams.
Output `organization/` reference markdown for these.

**Phase 2 — People.** Generate people with names, titles, **job descriptions**; assign
to teams, locations, managers; build the org chart; seed each person's **baseline
calendar** (recurring standups, 1:1s, all-hands, team syncs) across the window.

**Phase 3 — Projects.** For each team/department, create the configured number & kind of
projects, each with a timeline, members, milestones, and an expected **artifact cadence**
(e.g. "weekly status report," "kickoff deck," "sprint schedule").

**Phase 4 — Timeline / event simulation.** Walk each working day in the window. For each
project, given its cadence and milestones, emit concrete **activity events**: meetings
held, a status report authored on Fridays, a kickoff deck at project start, a doc reviewed
(→ comment thread), a schedule revised, etc. Output is an ordered **task list** of
artifacts-to-generate, each annotated with project, author(s), participants, and timestamp.

**Phase 5 — Artifact generation.** For each task, produce the concrete file with the right
**renderer** (§6). This is the LLM-heavy, parallelizable phase.

**Phase 6 — Assembly.** Write the directory hierarchy, per-person calendars, and a
`manifest.json` recording every artifact + provenance; snapshot config+seed.

---

## 6. Renderers (per artifact type)

| Artifact | Format | Renderer approach | Difficulty |
|---|---|---|---|
| Entity descriptions | `.md` | template + LLM-written prose | easy |
| Schedules | `.md` / `.json` | structured generation | easy |
| Calendars | `.md` / `.json` | from Phase 2/4 events | easy |
| Status reports / docs | `.docx` | `python-docx` body + **OOXML comment injection** | **hard** |
| Presentations | `.pptx` | `python-pptx` | medium |

### The hard renderer: DOCX with threaded comments
`python-docx` writes document bodies well but has **no native support for comments or
threaded replies**. Word stores these as separate OOXML parts inside the `.docx` zip:
- `word/comments.xml` — comment text + author + date
- `word/commentsExtended.xml` — parent/child links that create **reply threads**
- `word/commentsIds.xml`, `word/people.xml` — durable IDs and author identities
- comment range markers (`commentRangeStart/End`, `commentReference`) in `document.xml`

**Plan:** build a small OOXML post-processing helper that opens the `.docx` zip and injects
these parts so comments + replies render natively in Word, each attributed to a real
`Person` with an in-window timestamp. **This is the #1 technical risk — prototype it first
(spike in M6) before committing to the rest of the docx work.** Fallback if it proves too
costly: tracked-changes / inline "[COMMENT — Alice]:" annotations (lower fidelity).

---

## 7. Content engine, scale & cost

The content (names, job descriptions, document prose, meeting topics, status narratives,
**comment threads** where people argue/agree) is generated by an **LLM — Claude**. This is
an LLM-native application, so default to current Claude models:
- **Bulk content** (entity prose, document bodies, slides, comments): `claude-sonnet-4-6`
  — strong quality at lower cost for high-volume generation.
- **Hard structural reasoning** (org design, project planning, keeping the world
  internally consistent): `claude-opus-4-8` for those steps if needed.
- Make the model **configurable**; expose a realism/cost dial.

**Scale reality:** a large company over a month produces *many* artifacts → *many* LLM
calls. Design for it from the start:
- **Concurrency** with bounded parallelism in Phase 5.
- **Prompt caching** of stable context (company/org/project descriptions reused across many
  artifacts) to cut cost.
- **Cost ceiling + dry-run estimate**: before a big run, estimate artifact count × tokens
  and report projected cost; allow a cap.
- This fan-out maps naturally onto **Gas Town orchestration** — Phase 5 could be a workflow
  that slings artifact-generation tasks to polecats. (Decide in M9; not required for v1.)

> Note on auth: this town currently runs Claude via **OAuth subscription**, and the daemon
> only just got an `ANTHROPIC_API_KEY` wired in. Decide early whether Enterprise Sim calls
> the **Claude API** (needs a key + budget) or routes through the harness — this affects
> the whole cost model. (Open question Q3.)

---

## 8. Tech stack (proposed — confirm with Steve)

- **Language: Python.** Best ecosystem for the hard parts: `python-docx`, `python-pptx`,
  and direct OOXML manipulation for comments. Anthropic SDK for content.
- **Structure**: a CLI (`enterprise-sim run --config config.yaml`) over a library of
  `generators/` (org, people, projects, timeline) and `renderers/` (md, json, docx, pptx).
- **Reproducibility**: single seed threads through all randomness; config snapshotted.
- **Testing**: golden small-company run; OOXML validity checks (the generated `.docx`/
  `.pptx` must actually open in Word/PowerPoint).

---

## 9. Build milestones

| # | Milestone | Output |
|---|---|---|
| M1 | Config schema + validation; run skeleton + `manifest`/seed plumbing | `enterprise-sim run` produces an empty, reproducible run dir |
| M2 | Phase 1 — company / departments / teams generation → markdown | `organization/` skeleton |
| M3 | Phase 2 — people + org chart + baseline calendars | people markdown + calendars |
| M4 | Phase 3 — projects | project markdown |
| M5 | Phase 4 — timeline/event engine → artifact task list | task list (no files yet) |
| M6 | Phase 5a — markdown/json renderers (schedules, calendars) **+ DOCX comment spike** | first real artifacts + a proven comment-injection prototype |
| M7 | Phase 5b — full DOCX renderer (status reports w/ threaded comments) | `.docx` artifacts |
| M8 | Phase 5c — PPTX renderer | `.pptx` artifacts |
| M9 | Scale/perf: concurrency, prompt caching, cost ceiling; optional Gas Town workflow | a full large-company / 1-month run |

Suggested **first vertical slice** before M-by-M: a *tiny* end-to-end run (3 people, 1
project, 1 week, 1 status report + 1 deck) to exercise every phase and surface integration
risk early — especially the DOCX-comments spike.

---

## 10. Open questions for Steve (decisions before/while building)

1. **Scope of v1** — which artifact types are in the first shippable version? (Recommend:
   markdown reference data + schedules + status-report `.docx` *with* comments; defer pptx
   and deep calendars if needable.)
2. **Email artifacts?** Requirements mention people "collaborating" — do we also emit email
   threads, or is collaboration captured purely via document comment threads in v1?
3. **Claude API vs harness auth + cost budget** — see §7. What's the cost ceiling per run?
4. **DOCX comments fidelity** — commit to native OOXML threaded comments (higher effort,
   higher fidelity) vs inline annotation fallback?
5. **Determinism strength** — must two runs with the same seed be byte-identical, or just
   structurally equivalent? (LLM nondeterminism makes byte-identical hard; likely "cache +
   structural" is the realistic target.)
6. **Realism depth of calendars** — full tz-aware working-hours/holiday modeling, or a
   simpler weekday-business-hours model for v1?
7. **Sizing semantics** — does `size` mean exact headcount, or a t-shirt size mapping to a
   range/shape (span of control, dept count)?

---

## 11. Risks

- **DOCX threaded comments** (no library support) — top risk; spike first (M6).
- **Internal consistency at scale** — keeping every reference valid as volume grows;
  mitigate with a central in-memory world model that all renderers read from.
- **LLM cost/latency at enterprise scale** — mitigate with caching, concurrency, cost caps,
  and (optionally) Gas Town fan-out.
- **Output validity** — generated Office files must actually open; add format-validation
  tests to CI.
