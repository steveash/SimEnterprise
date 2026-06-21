# Enterprise Sim — Vision & Decisions

> Status: **DRAFT, under active review with Steve.** This document holds the *vision*,
> the *decisions made so far*, and the *milestones*. The detailed component/plugin
> design lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## 1. What we're building

**Enterprise Sim** generates a fake-but-realistic enterprise organization and *all the
office-work artifacts that organization would produce* over a configurable time window —
and, crucially, the **ground-truth knowledge graph** those artifacts encode.

A user describes a company at a high level; the simulator invents everything else — the
business **goals**, the **departments** organized around them, the **programs** and
**scenarios** of work running inside each department, the **people** doing that work, and
the **stream of artifacts** (status reports, design docs with review threads, decks,
schedules, and later Jira/ServiceNow/email) those people produce as the work unfolds.

Because we *generate* the world, we know every entity, relationship, and event with
certainty. So a run emits **two coupled outputs**:

1. **The corpus** — the artifacts themselves (the "evidence"), in native formats.
2. **The gold knowledge graph** — entities + relationships + events, where every edge
   carries **provenance** back to the exact artifacts that express it.

That makes Enterprise Sim a **labeled benchmark generator** for knowledge-graph
extraction, RAG, and search eval: run your extractor over the corpus, diff against the
gold KG, get precision/recall for free.

Think: "SimCity / The Sims, but for an enterprise org and its document exhaust — with an
answer key."

### Primary use case (drives all priorities)
- **Knowledge-graph / RAG / search eval.** The value is in *realistic structure and
  realistic interactions between entities* being faithfully reflected in the artifacts.
  The more the real-world dynamics of an enterprise (who reviews whose work, what work
  advances which goal, how a decision in one doc surfaces in the next) show up in the
  corpus, the better.
- Secondary: realistic corpora for DLP / e-discovery / SIEM / org-graph tools; demos and
  sandboxes that need a plausible, internally-consistent company.

---

## 2. Decisions made (review log)

These are settled unless we revisit them. Rationale and detail in `ARCHITECTURE.md`.

| # | Decision | Notes |
|---|---|---|
| D1 | **Gold KG is a first-class output and drives the data model.** | Every generation step records the entities/relations/events it expresses, with provenance to artifacts. |
| D2 | **Architecture = events-as-contract + plugin registries.** | Core simulates the enterprise and emits *format-agnostic business events* bound to the KG. Artifact producers are plugins. Core never imports a format library. |
| D3 | **Work is modeled as Goal → Department → Initiative (recursive) → Playbook → Process.** | "Program" and "scenario" are unified as one recursive `Initiative`; a scenario is an Initiative with a Playbook attached. |
| D4 | **Four extension registries:** DepartmentArchetype, Playbook, Process, Producer. | Add any one over time; lower layers keep working because they only speak `Event`/KG-entity. |
| D5 | **v1 is markdown-only.** | The registry maps every deliverable to the `markdown` producer. Full framework + a usable corpus before any Office-format risk. |
| D6 | **Multi-modal by design, single in v1.** | Registry allows many producers per event (one event → docx + Jira + email later); v1 wires only `markdown`. |
| D7 | **Language: Python.** | Mature `python-docx`/`python-pptx`, official Anthropic SDK with Bedrock support. |
| D8 | **Native DOCX threaded comments are required (v2).** | Build our own OOXML comment injector; **spike it first** to de-risk. Escape hatch: a .NET Open XML SDK helper invoked as a subprocess. |
| D9 | **LLM provider is pluggable:** Anthropic API key · AWS Bedrock · Claude CLI (`claude -p`). | API + Bedrock share the official SDK; the CLI path routes through the OAuth subscription for cheap bulk fan-out. Config-selected. |
| D10 | **Determinism = structural, not byte-identical.** | One seed threads through; each plugin gets a derived sub-seed. LLM nondeterminism makes byte-identical infeasible. |
| D11 | **Calendars: simple weekday business-hours in v1**; tz-aware working-hours later. | |
| D12 | **Email/Jira/ServiceNow deferred to post-v1** as new producer (and optional process) plugins. | Collaboration in v1 is captured via document review/comment threads. |
| D13 | **Configurable cost ceiling; default provider = API/Bedrock.** | Dry-run estimate + hard ceiling before large runs. CLI/subscription remains available for cheap bulk runs. |
| D14 | **Goals may nest (sub-goals) in v1.** | Goal is a recursive node like Initiative. |
| D15 | **Ship ≥2 playbooks in v1** (e.g. `build_software` + one non-engineering, such as `sell_merchandise` or `compliance_audit`) to prove the abstraction. | Forces the process-sharing/divergence model to be real, not theoretical. |
| D16 | **Rich cross-document reference KG is a primary goal** — artifacts cite prior artifacts/decisions, producing dense `references` edges. | Applies to *all* current and future producers/processes, not just v1. |
| D17 | **Validator reports-and-continues on dangling references; all validation issues are recorded during the run** to a validation log and summarized in the manifest. | A run is never silently lossy; issues are inspectable. |
| D18 | **KG = a labeled property graph with reified (first-class, id-bearing) edges**, kept as materialized state alongside an append-only event journal. Same schema in-memory and on disk. | Reified edges let provenance/eval target *relationships*. Timestamps enable projection-by-filter. See `ARCHITECTURE.md` §11. |
| D19 | **Canonical KG output = JSONL** (nodes/edges/events/provenance); **Neo4j/Cypher exporter first**, others pluggable. Provenance is **artifact-level with a span locator field reserved**. | JSONL is diffable/streamable; exporters generated from canonical form. |
| D20 | **Full mention occurrences in v1** via a deterministic mention tagger (constrained alias matching over in-scope entities). | Yields entity-recognition + coreference labels; alias table derived from observed surface forms. |
| D21 | **Authoring substrate = declarative Python** (builder/dataclass SDK). | Selectors/triggers/guards are expressions; embedding an expression language in YAML is worse. Optional YAML projection for pure-composition playbooks later. See `ARCHITECTURE.md` §12. |
| D22 | **Full six-trigger taxonomy in v1:** `OnStart`, `OnCadence`, `OnEvent`, `OnMilestone`, `OnCondition`, `Probabilistic`. | Proven sufficient across software/retail/pharma without new engine primitives. |
| D23 | **Validation & testing are first-class v1 deliverables.** Three tiers: (1) static lint/type-check/evaluators at build time; (2) isolated process/playbook **test kit** with an auto-applied **conformance invariant suite** + golden snapshots + determinism check; (3) structural + optional LLM-judge evaluators. | A process/playbook is not "done" until it lints clean and its tests/evals pass (CI-enforced). See `ARCHITECTURE.md` §13. |
| D24 | **An `author-playbook` agent skill** documents the model, the six triggers, the authoring workflow, validation/test usage, pattern recipes, and the acceptance checklist. | Enables agents to author *and self-verify* a new business domain. See `ARCHITECTURE.md` §14. |
| D25 | **v1 ships two playbooks: `build_software` + `sell_merchandise` (retail).** | Retail forces `OnCondition` + external entities + stateful lifecycle + code escape hatch, proving cross-vertical generality early. |

---

## 3. v1 scope (first shippable)

A complete, markdown-only end-to-end run that exercises the whole framework:

- **Org + intent:** company → goals → departments → programs/scenarios → teams → people →
  projects, all written to the KG and to markdown reference data.
- **Processes (v1 set):** `weekly_status`, `design_review` (with threaded review
  comments), `project_kickoff`, `sprint_cycle`.
- **Artifacts:** everything rendered as markdown/JSON by the `markdown` producer —
  including review threads represented in markdown.
- **Gold KG export** with provenance linking every edge to supporting artifacts.
- **Reproducible** via seed; `manifest.json` + config snapshot.

Office formats (`.docx` with native comments, `.pptx`) and new modalities (Jira, Outlook)
arrive in later milestones as *additive* producer plugins — **the event simulator does
not change** when they're added.

---

## 4. Milestones

Testing-first ordering: the authoring SDK + validation + test kit + skill land **before**
the real playbooks are authored, so the v1 playbooks are themselves the first proof the
authoring/validation loop works.

| # | Milestone | Output |
|---|---|---|
| M1 | Foundations: config/seed, KG store, event log, plugin registries, manifest | empty, reproducible run dir |
| M2 | Engine core: clock, event queue/scheduler, actor/relationship resolver | a trivial process runs and emits events |
| M3 | **Authoring SDK (declarative Python, full six triggers) + static linter/type-checker + isolated process/playbook test kit (conformance suite, golden snapshots, determinism) + evaluators + `author-playbook` skill** | the extensibility + quality backbone |
| M4 | Layer A: company → goals → departments → initiatives → teams → people → projects → KG + markdown | `organization/` reference data |
| M5 | Author `build_software` + `sell_merchandise` **through the SDK**; each passes lint + conformance + custom tests + eval | two validated, cross-vertical playbooks |
| M6 | `markdown` producer for all deliverables → **full markdown-only v1 corpus** | first end-to-end corpus |
| M7 | Gold-KG export (provenance + mentions + Neo4j exporter) + consistency validator | corpus + answer key |
| M8 | **Spike** native threaded-comment `.docx`, then `word` producer (rebind deliverables) | `.docx` artifacts w/ real comments |
| M9 | `pptx` producer | `.pptx` artifacts |
| M10 | New-modality producers (`jira`/`outlook`) proving extensibility; scale: concurrency, prompt caching, cost ceiling | large, multi-modal run |

**First vertical slice** (before grinding M-by-M): once M1–M3 exist, author a *tiny*
playbook through the SDK and validate it in isolation, then run 1 department / 1 scenario /
~3 people / 1 week end-to-end to a handful of markdown artifacts + a gold KG. Surfaces
integration risk — and exercises the authoring/test loop — early.

---

## 5. Risks

- **Native DOCX threaded comments** — no library support; we own the OOXML. Top technical
  risk; **spike before committing** (M7). .NET Open XML SDK subprocess is the fallback.
- **Internal consistency at scale** — every name/reference must resolve to a real entity.
  Mitigated by the KG-as-spine: producers render from a *projection of the KG at a
  timestamp*, so they can only reference real, in-window entities.
- **LLM cost/latency at enterprise scale** — mitigated by the CLI/subscription path for
  bulk fan-out, prompt caching on SDK paths, bounded concurrency, and a dry-run cost
  estimate + ceiling.
- **Playbook/process realism** — domain-specific work (selling, compliance) needs
  believable processes. Mitigated by making playbooks/processes a growing, reviewable
  catalog rather than hard-coded into the core.

---

## 6. Open questions still to resolve

Top-level design questions are resolved (see decisions D1–D20). The KG representation is
specified in `ARCHITECTURE.md` §11. Remaining questions are implementation-level and
raised per component as design proceeds. Next implementation elements to work through:
the LLM orchestration/prompt-assembly layer, the scheduler + actor/relationship resolver,
and the authoring format for playbooks/processes.
