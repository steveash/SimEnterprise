# Enterprise Sim — Architecture

> Detailed component & plugin design. Vision, decisions, and milestones live in
> [`PLAN.md`](./PLAN.md). This document is the source of truth for *how* the system is
> structured.

---

## 1. Guiding principles

1. **Events are the contract.** The core simulates the enterprise and emits a stream of
   **format-agnostic business events** bound to entities in the knowledge graph. It knows
   nothing about `.docx`, Jira, or email. Downstream producers turn events into artifacts.
2. **The knowledge graph is the spine.** A single typed graph is the source of truth.
   World-builders write entities; the event simulator writes events and relationships;
   producers read *projections* of it; assembly exports it as the gold answer key.
3. **Everything that varies by domain is a plugin.** Department archetypes, playbooks,
   processes, and producers are registered into catalogs. Adding capability never requires
   touching the core — the core only ever speaks `Event` + KG-entity.
4. **Render from a timestamped projection.** A producer is given the slice of the KG that
   is "known" at the event's timestamp (relevant subgraph + recent history). It therefore
   *cannot* reference a person, project, or decision that doesn't exist yet. Consistency is
   a property of the data we feed in, not something we hope the LLM gets right.
5. **Provenance is mandatory.** Every produced artifact records which KG entities/relations/
   events it expresses, so the gold KG can point back to supporting evidence.

---

## 2. The four layers

```
┌─ Layer A: World & Intent Builder ─────────────────────────────────────────┐
│  company → goals → departments (archetype-biased) → initiatives            │
│  (programs/scenarios, scenarios bind playbooks) → teams → people → projects │
│  writes ENTITIES + intent structure into the Knowledge Graph              │
└────────────────────────────────────────────────────────────────────────────┘
┌─ Layer B: Event Simulator ────────────────────────────────────────────────┐
│  for each scenario: instantiate its Playbook → schedule its Processes      │
│  • Process plugins emit business events (meetings, drafts, comments, …)     │
│  • Actor/relationship resolver picks realistic participants from the KG     │
│  • Scheduler assigns timestamps (working hours, multi-day threading)        │
│  → emits an ordered BUSINESS EVENT LOG + mutates the KG                     │
└────────────────────────────────────────────────────────────────────────────┘
┌─ Layer C: Artifact Producers (plugins) ───────────────────────────────────┐
│  markdown · word · powerpoint · jira · outlook · …                         │
│  each DECLARES which events/deliverables it handles, consumes events,       │
│  renders files, records provenance back into the KG                        │
└────────────────────────────────────────────────────────────────────────────┘
┌─ Layer D: Assembly ───────────────────────────────────────────────────────┐
│  directory hierarchy · manifest.json · GOLD KG export (entities +          │
│  relations + events, each edge → the artifacts that express it)            │
└────────────────────────────────────────────────────────────────────────────┘

Cross-cutting: KG store · LLM provider (api/bedrock/cli) · seed/determinism · config
```

---

## 3. The knowledge graph (the spine)

A typed, in-memory **labeled property graph** (serializable to JSON) of nodes and edges.
Every node *and edge* has a stable `id`; everything else references ids. The full
representation, store API, projection mechanism, provenance/mention model, and output
formats are specified in **§11**. Two broad node families:

### Org & intent nodes
- **Company** — name, vertical, size, era, description.
- **Goal** — a business objective ("grow EU revenue 20%", "achieve SOC2"). May link to
  sub-goals. Owned by company/departments.
- **Location** — city, country, timezone, office type.
- **Department** — function, charter, head; *informed by a DepartmentArchetype*. Advances
  one or more Goals.
- **Initiative** — **the recursive work node.** `type ∈ {program, scenario}`,
  `parent: Initiative?`, `goal_refs`, owner, timeline, staffing. A **program** is a
  container that may nest sub-initiatives; a **scenario** is an Initiative that binds a
  **Playbook**. (Program and scenario are the *same* entity, distinguished by `type` and
  whether a playbook is attached — so work nests arbitrarily.)
- **Team** — department, charter, lead, members.
- **Person** — name, title, **job description**, team, location, manager, seniority,
  **expertise tags**, working hours. Expertise + role drive realistic reviewer selection.
- **Project** — concrete deliverable effort under an Initiative; timeline, milestones,
  members with **roles** (lead / contributor / reviewer / approver / stakeholder / sponsor).

### Activity nodes (written by the event simulator + producers)
- **Artifact** — an *abstract* work product (a status report, a design doc): kind, authors,
  reviewers, timestamps. Distinct from the **rendered file(s)** a producer emits for it.
- **Meeting** — kind, attendees, time, linked initiative/project.
- **Comment** — author, target artifact, `in_reply_to` (threading), timestamp.
- **Decision** — what was decided, where (meeting/doc), who; links downstream references.
- **CalendarEvent** — per-person schedule entries.

**Edges** capture the relationships KG-extraction cares about: `reports_to`, `member_of`,
`leads`, `advances_goal`, `owns_initiative`, `authored`, `reviewed`, `approved`,
`attended`, `decided`, `references` (artifact→artifact), `expresses` (artifact→any node,
for provenance).

---

## 4. The intent hierarchy & the four registries

```
Company
  └─ Goal                      business objective
       └─ Department            org unit (informed by DepartmentArchetype)
            └─ Initiative ────── recursive: program ⊃ sub-program ⊃ scenario
                 └─ Playbook     a kind of work (bound at scenario level)
                      └─ Process  event-emitter
                           └─ emits Events → KG + Producers
```

Everything traces up to a **Goal**, which is what makes the KG queryable end-to-end
("what work advanced Goal X, by whom, producing which artifacts?").

### Registry 1 — DepartmentArchetype
A kind of department. Declares typical goals, typical team shapes, and which **playbooks**
it tends to run. Examples: `engineering`, `sales`, `legal`, `finance`, `marketing`.
*Used by Layer A to bias generation toward a believable department.*

### Registry 2 — Playbook
A **kind of work** — the unit the user wants to grow over time. Declares:
- **required roles** (who must staff it),
- **expected deliverables** (abstract kinds: `status_report`, `design_doc`, `kickoff_deck`),
- the **set of Processes** it runs, with cadence/params.

Examples: `build_software`, `sell_merchandise`, `compliance_audit`, `hire_team`. A
**scenario** is an Initiative bound to a Playbook. Playbooks overlap on shared processes
(e.g. `status_report`) and diverge on specialized ones.

### Registry 3 — Process
The atomic **event-emitter**. Recurring (a cadence) or triggered (a milestone/condition).
Declares the **event types** it emits and the **abstract deliverables** it requests —
never a file format. Shared by reference across playbooks.

v1 set: `weekly_status`, `project_kickoff`, `sprint_cycle`, `design_review`.
Future: `inventory_monitor`, `negotiate_terms`, `incident_response`, `quarterly_planning`.

### Registry 4 — Producer
Renders events → concrete artifacts. Declares which events/deliverables it handles and the
format(s) it emits. A config-driven **binding** maps `deliverable.kind → producer`, which
is the seam that makes the roadmap additive:

- **v1:** every deliverable kind → `markdown` producer.
- **v2:** add `word` producer, rebind `status_report`/`design_doc` to it — *event
  simulator unchanged*.
- **Later:** `jira`, `outlook`, `servicenow` producers; optionally new processes that emit
  their events. The binding may be **one-to-many** (one event → docx + Jira + email) to
  produce a cross-modal, KG-consistent corpus.

> **Extensibility invariant:** adding an archetype, playbook, process, or producer must
> never require editing `core/`. If it does, the abstraction leaked — fix the abstraction.

---

## 5. Key contracts (illustrative)

```python
# A format-agnostic business event, bound to KG entities.
@dataclass
class Event:
    id: str
    type: str                      # "DeliverableDrafted", "CommentPosted", "MeetingHeld" …
    timestamp: datetime
    actors: dict[str, list[str]]   # role -> [person_id]  e.g. {"author":[p1],"reviewers":[p2,p3]}
    initiative: str | None
    project: str | None
    subjects: list[str]            # KG node ids this event is "about"
    deliverable: Deliverable | None  # abstract: {"kind": "status_report", "medium": "document"}
    parent_event: str | None       # threading / causal chains
    payload: dict                  # semantic brief for the producer (topic, intent, tone)

# A kind of work. Bundles processes + staffing/deliverable expectations.
class Playbook(Protocol):
    name: str
    def required_roles(self) -> list[RoleSpec]: ...
    def expected_deliverables(self) -> list[DeliverableSpec]: ...
    def processes(self) -> list[ProcessRef]: ...        # which processes + cadence/params
    def instantiate(self, initiative, world, ctx) -> InitiativePlan: ...

# An event-emitter. Knows nothing about file formats.
class Process(Protocol):
    name: str
    emits: list[str]               # event types it can produce
    requests: list[str]            # abstract deliverable kinds
    def run(self, scenario, world, clock, rng, ctx) -> Iterable[Event]: ...

# A renderer. Pure function of (event, KG-projection) -> artifacts + provenance.
class ArtifactProducer(Protocol):
    name: str
    def handles(self, event: Event) -> bool: ...        # by type / deliverable kind / medium
    def produce(self, event: Event, world: WorldView, ctx) -> list[ProducedArtifact]: ...
    # ProducedArtifact = {path, format, artifact_id, authored_by,
    #                      provenance=[edges this file expresses]}
```

`WorldView` is a **timestamped projection** of the KG — only entities/events that exist at
`event.timestamp`, plus the relevant subgraph (the scenario, its people, recent artifacts).
Producers physically cannot reference the future or non-existent entities.

---

## 6. Data flow of a run

1. **Configure & seed** — parse/validate config; fix RNG seed; resolve date window and a
   simple business-day working calendar.
2. **Layer A — build world & intent:** company → goals → departments (archetype-biased) →
   the LLM **selects and parameterizes playbooks** per department/program from the registry
   → instantiate scenarios → staff with teams/people → projects. All written to the KG and
   to `organization/` markdown.
3. **Layer B — simulate events:** advance the clock over the window. For each scenario,
   instantiate its playbook and schedule its processes; each process emits events; the
   actor resolver fills participants from the KG; the scheduler places timestamps and
   threads multi-day interactions (reviews trickle comments over days). Events mutate the
   KG (new Artifact/Comment/Decision/Meeting nodes + edges).
4. **Layer C — produce artifacts:** route each event to the producer(s) bound to its
   deliverable/medium; render files from the timestamped projection; record provenance.
   *(LLM-heavy, parallelizable phase.)*
5. **Layer D — assemble:** write the directory hierarchy + per-person calendars +
   `manifest.json`; export the **gold KG** (entities + relations + events with provenance);
   snapshot config + seed; run the **consistency validator**.

---

## 7. Cross-cutting concerns

### LLM provider abstraction
A single internal interface — `complete(system, messages, model, *, cache_hint) -> Completion`
— with config-selected backends:
- **`anthropic_api`** — official SDK + `ANTHROPIC_API_KEY`. Supports prompt caching.
- **`bedrock`** — *same* official SDK via `AnthropicBedrock` (and `AnthropicVertex` available
  if GCP is ever needed). "Key vs Bedrock" is one dependency, two constructors.
- **`claude_cli`** — shell out to `claude -p --output-format json` to route bulk fan-out
  through the OAuth subscription (cheap). Caveat: less control over cache-control and token
  accounting, so prompts are structured so the cacheable prefix still benefits SDK paths.

All callers (world-builders and producers) see only the interface. Backend, model, and a
realism/cost dial are config.

### Determinism
Structural, not byte-identical (LLM nondeterminism). One root seed threads through; each
plugin/process/producer derives a stable sub-seed (e.g. `hash(root_seed, plugin_name,
entity_id)`), so re-runs reproduce the same *structure, staffing, schedule, and event log*.
Config + seed are snapshotted.

### Prompt structure for cache & consistency
Stable context (company/department/scenario/project descriptions) is assembled as a
**cacheable prefix** reused across the many artifacts of a scenario; only the per-artifact
brief varies. This is a prompt-layer constraint from day one, not a late optimization.

### Scale & cost
Bounded-concurrency producer phase; dry-run estimate (artifact count × token estimate)
with a cost ceiling before large runs. Gas Town workflow fan-out is an *optional* later
backend for Layer C, not a dependency.

---

## 8. Repository layout (Python)

```
enterprise_sim/
  core/
    world/        # KG model: entity/edge types, store, timestamped projections
    events/       # Event types, event log
    sim/          # clock, event queue/scheduler, actor/relationship resolver
    llm/          # provider abstraction: anthropic_api · bedrock · claude_cli
    config/       # schema, validation, seed/determinism
    registry/     # the four plugin registries + discovery
  authoring/      # the playbook/process SDK (§12) + quality stack (§13)
    sdk.py          # Selector, Role, Trigger, Step, Process, Activation, Playbook, Declares
    lint.py         # Tier 1 static lint / type-check / event-graph checks
    testkit.py      # Tier 2 TestWorld, run_process/run_playbook, conformance suite
    eval.py         # Tier 3 structural + LLM-judge evaluators
  world_builders/ # Layer A generators (company, goals, departments, initiatives, people…)
  archetypes/     # DepartmentArchetype plugins (engineering, retail, legal, …)
  playbooks/      # Playbook plugins (build_software, sell_merchandise, …)
  processes/      # Process plugins (weekly_status, design_review, inventory_monitor, …)
  producers/      # Producer plugins (markdown, word, pptx, jira, outlook, …)
  exporters/      # KG interop exporters (neo4j first; rdf/graphml later)
  assembly/       # dir layout, manifest, gold-KG export, consistency validator
  cli.py          # enterprise-sim {run, lint, eval} …
skills/
  author-playbook/  # §14 agent skill: model, triggers, authoring + validation loop, patterns
tests/
  conformance/    # the built-in invariant suite (I1–I8, P1–P6)
  playbooks/      # per-playbook unit tests + golden snapshots
```

---

## 9. The hard renderer (deferred to M7): native DOCX threaded comments

`python-docx` writes bodies but has no real support for comments or threaded replies. Word
stores these as separate OOXML parts inside the `.docx` zip:
- `word/comments.xml` — comment text + author + date
- `word/commentsExtended.xml` — parent/child links that create **reply threads**
- `word/commentsIds.xml`, `word/people.xml` — durable ids + author identities
- range markers (`commentRangeStart/End`, `commentReference`) in `document.xml`

**Plan:** a small OOXML post-processor opens the `.docx` zip and injects these parts so
comments + replies render natively in Word, each attributed to a real `Person` with an
in-window timestamp. **Spike this in isolation first** (one doc, one threaded comment,
confirm it opens cleanly in Word) before building the full `word` producer. **Escape
hatch:** a tiny .NET Open XML SDK helper invoked as a subprocess (gold-standard native
support) if the hand-rolled injector proves too costly.

Until then, the `markdown` producer represents review threads structurally in markdown, so
the *event model and KG are fully exercised in v1* and the docx work is purely additive.

---

## 10. Why this serves the knowledge-graph goal

- The **gold KG is generated, not extracted** — perfect ground truth with provenance.
- Artifacts are **traces of processes**, not isolated documents, so the corpus encodes the
  real edges (who reviewed whom, what advanced which goal, which decision spawned which
  doc) that KG extraction is meant to recover.
- Reviewers/commenters are chosen from the KG by **team + role + expertise + seniority**,
  so collaboration looks like the real world (mostly intra-team, occasional cross-functional).
- Cross-document **`references`** edges make the corpus internally referential — the
  interesting case for both RAG and KG extraction.

---

## 11. Knowledge graph: representation & output

The KG is both the **spine of the run** and the **answer key** shipped with the corpus.
It is one **labeled property graph (LPG)** — typed nodes with properties, typed edges with
properties — used identically in memory and on disk (the output is a dump of the store, so
the simulated world and the published answer key can never drift).

### 11.1 In-memory representation

A `World` store holds three keyed collections plus traversal indexes:

```python
@dataclass
class Node:   id; type; props: dict; created_at; aliases: list[str]
@dataclass
class Edge:   id; type; src; dst; props: dict; created_at        # FIRST-CLASS: has its own id
@dataclass
class Event:  id; type; timestamp; actors: dict; subjects: list; parent_event; payload: dict
```

Three decisions that matter:

1. **Edges are reified** (first-class, id-bearing, stored in their own table with adjacency
   indexes `by_src[type]` / `by_dst[type]`). Provenance and KG-eval must be able to target a
   *relationship* — "artifact X expresses the `reviewed(ada, design-doc-7)` edge" — which is
   impossible if edges are mere node attributes.
2. **Everything carries `created_at` (sim-time).** A **projection at time T** is the subgraph
   of nodes/edges with `created_at <= T`, narrowed to a focus subgraph (scenario, its people,
   recent artifacts in a window). This `WorldView` is what each producer receives, so a
   producer *cannot* reference the future or a non-existent entity. Consistency is enforced
   by construction.
3. **Deterministic ids and ordering.** Ids are human-readable and content-derived
   (`person:ada-lovelace`, `proj:payments-api`, `art:2026-06-19-payments-weekly-status`,
   `edge:reviewed:ada:design-doc-7`); storage is insertion-ordered; queries sort by id.
   Same seed → identical graph, ids, and event order.

The store exposes typed helpers (not raw dict access): `reviewers_for(project, expertise,
free_at)`, `recent_artifacts(scenario, before, within)`, `decisions_in(initiative)`,
`neighbors(id, edge_type, dir)`, `projection(at, focus, window)`, `record_provenance(...)`,
`record_mention(...)`, `validation_issue(...)`.

**Library choice:** a hand-rolled typed store, *not* `networkx` or an embedded graph DB.
networkx stores attributes as loose dicts (no schema, awkward temporal projection + edge
reification, heavy at 10⁵–10⁶ elements); a graph DB is an unnecessary v1 dependency. We
instead provide **export adapters** to networkx (algorithms/viz) and other formats, gaining
their benefits without coupling the core. Scale target (large company / 1 month ≈ 10⁵ nodes,
up to ~10⁶ edges) fits in memory; the journal + timestamp model makes later spilling or
per-scenario sharding straightforward.

### 11.2 KG vs the event journal

The KG is the **materialized current state**; the **event log is an append-only journal**.
Both are first-class outputs. Layer A writes static structure as genesis nodes; Layer B
events *apply* to the KG (adding `Artifact`/`Comment`/`Decision`/`Meeting` nodes + edges,
stamped with the event timestamp). Projections filter the materialized KG by timestamp
(fast); the retained journal gives replay, debugging, and a temporal ground truth of *what
happened when*. Event-influenced, not pure event-sourcing.

### 11.3 Provenance vs mentions (two distinct answer keys)

- **Provenance** — which artifact(s) *express* a node or edge. **Artifact-level in v1**, with
  a `locator` field reserved for future span-level grounding. Targets both nodes and edges
  (hence edge reification). This answers "did the extractor recover this entity/relationship?"
- **Mentions** — *where* each entity's surface form occurs in artifact text. **Full
  occurrences in v1**, with span locators. This answers entity-recognition + **coreference**.

**Mention tagging** runs after each artifact renders: a deterministic tagger scans the text
for the **known surface forms of the in-scope entities** (canonical names + aliases from that
artifact's `WorldView`). The candidate set is small and known, so this is constrained,
high-precision alias matching — not open-domain NER. Templated references (author lines,
attendee lists) are recorded exactly; free-prose references are caught by the tagger;
unresolved ambiguity (two in-scope "Chris"es) is logged as a validation issue. The per-entity
**alias table** is the union of observed surface forms.

Locator format is per-medium: markdown → char offset + length (and line); docx (later) →
paragraph/run index. The schema is uniform so consumers handle one shape.

### 11.4 On-disk output

```
kg/
  nodes.jsonl          # {id, type, props, created_at, aliases}
  edges.jsonl          # {id, type, src, dst, props, created_at}
  events.jsonl         # the temporal journal
  provenance.jsonl     # {target_id (node|edge), artifacts: [{path, locator?}]}
  mentions.jsonl       # {artifact_path, entity_id, surface_form, locator}
  aliases.jsonl        # {entity_id, canonical, aliases: [...]}
  schema.json          # JSON Schema for all of the above (self-describing)
  graph.json           # convenience node-link form (networkx-loadable)
  neo4j/               # FIRST interop exporter (see 11.5)
    import.cypher        # MERGE script for small graphs
    nodes/*.csv          # neo4j-admin bulk import (one file per label)
    relationships/*.csv  # :START_ID / :END_ID / :TYPE per relationship type
validation/
  issues.jsonl         # dangling refs, scheduling conflicts, out-of-window stamps (D17)
```

JSONL is the **canonical** form — streamable, diffable line-by-line (so two runs `git`-diff
cleanly), loadable anywhere. `graph.json` is a convenience; everything else is generated
from the canonical files.

### 11.5 Interop exporters (pluggable, Neo4j first)

Exporters are a small registry mirroring producers; we ship **Neo4j/Cypher** first
(property-graph native, best match for the LPG). Mapping: node `type` → label, props →
properties, `aliases` → array property, edges → typed relationships with properties.
Provenance and mentions become real relationships where possible —
`(:Artifact)-[:MENTIONS {surface, locator}]->(entity)` and `(:Artifact)-[:EXPRESSES]->(node)`.
**Reification wrinkle:** vanilla Neo4j can't point a relationship at a relationship, so
*edge-targeted* provenance is emitted as an `expressed_by: [artifact_ids]` property on that
relationship instead of an `:EXPRESSES` edge. RDF/Turtle and GraphML exporters can be added
later behind the same interface.

---

## 12. Playbook/Process authoring format

**Authoring substrate is declarative Python** (a builder/dataclass SDK in
`enterprise_sim/authoring/`), because selectors, triggers, and guards are *expressions* and
plugins are Python anyway. An optional YAML→objects loader for pure-composition playbooks
can come later; it is not the core.

### 12.1 Model

- **Process** — a reusable, named work activity that, when instantiated, plays out a **timed
  sequence of steps** over one or more entities, **emitting events**, optionally **producing
  deliverables**, and **mutating the KG**. It has internal temporal structure (a review is
  *draft → multi-day comment window → revise → approve*), is **domain-opaque to the engine**
  (the engine only reads its `declares` block), and supports a **code escape hatch** (`impl`)
  for logic the declarative steps can't express.
- **Playbook** — a goal-oriented **composition**: scenario-level roles + a set of
  **activations**, each wiring a process to a **trigger**, a **role binding**, and **params**.
  Activations form an **event-driven triggering graph** (one process's emitted event can
  trigger another).

**Everything is event-driven; time is first-class on every trigger and step.** Six triggers:

| Trigger | Fires when |
|---|---|
| `OnStart()` | scenario begins |
| `OnCadence(rule)` | recurring schedule (`weekly:FRI`, `per_sprint:2w`, `daily:workdays`) |
| `OnEvent(type, where)` | another process/world emits a matching event |
| `OnMilestone(name)` | initiative/project milestone reached |
| `OnCondition(expr)` | a KG/state predicate becomes true |
| `Probabilistic(rate, per)` | seeded stochastic sampling |

### 12.2 Building blocks

```python
Selector(type, where=[...], exclude=[...], rank_by=[...], count="2..3")  # bind entities from KG
Role(name, select=Selector(...))

Step(id, at, duration, by,                       # at="day 0" | after="draft"+"1d"
     emits=[Event(...)], produces=Deliverable(kind, medium, authors, reviewers) | None,
     effects=[KGEffect.create(...) | .mutate(...)],
     repeat=Spread(per_actor="1..3", over="duration"), when=expr)

Process(name, description, roles=[Role(...)], params={...},
        steps=[Step(...)] OR impl="pkg.module:ClassName",
        declares=Declares(events=[...], deliverables=[...], effects=[...]))   # engine trusts this

Activation(process, trigger=Trigger(...), bind={role: ...}, params={...})
Playbook(name, vertical, goal_template, roles=[Role(...)],
         activations=[Activation(...)], deliverable_expectations=[...])
```

### 12.3 Cross-vertical validation (reference patterns)

Three worked playbooks proved the format needs **no new engine primitives** across very
different domains — they become the skill's pattern library (§14):
- **`build_software`** (technology): `OnCadence` sprints + `OnEvent` design reviews + `OnMilestone` ship. Mostly declarative.
- **`sell_merchandise`** (retail): `OnCondition`/`OnEvent` cascade (low-stock → supplier negotiation → PO), an **external** `supplier` entity, a **stateful** PO lifecycle, and an `impl`-backed `inventory_monitor`.
- **`run_clinical_study`** (pharma): a **gated event-chain** (`ProtocolApproved` → `IRBApproved` → …), **external** regulators (CRO/IRB), and an urgent `Probabilistic` adverse-event report with an SLA + sign-off chain + amendment cascade.

Gates are event-chains; lifecycles are guarded steps; simulated state is the `impl` hatch;
external parties are `Selector(external=...)` — all expressed in the primitives above.

---

## 13. Validation & testing (first-class)

A process/playbook is **not "done" until Tier 1 is clean and Tiers 2–3 pass** (CI-enforced).
Lives in `enterprise_sim/authoring/{lint,testkit,eval}.py`.

### Tier 1 — Static lint / type-check (`enterprise-sim lint`, no execution)
- **Type/schema**: fields present & typed (dataclasses + pydantic + mypy); cadence rules
  parse; `OnEvent` references a known event type; `OnCondition` compiles; `Probabilistic`
  rate sane.
- **Reference integrity**: every `@role` resolves; `after="step"` targets an existing step;
  no cyclic step deps; selector attributes/`count` valid; deliverable kinds registered.
- **`declares` conformance (static)**: steps' emitted events / produced deliverables /
  effects must match the `declares` block — the engine trusts `declares`.
- **Event-graph soundness**: flag **dead triggers** (`OnEvent` nobody emits), **unreachable
  processes** (no path from a start/cadence trigger), and **unguarded cycles** (runaway risk).
- **Feasibility & volume**: selector minimums satisfiable by role/archetype; cadence/
  probabilistic rates won't explode artifact counts (cost linter).
- **Determinism**: AST rule forbids wall-clock / unseeded random in `impl` code.

### Tier 2 — Isolated test kit (deterministic execution)
- **Auto-synthesized world**: `TestWorld.satisfying(process)` reads `roles` and generates a
  minimal KG that binds them — no hand-built fixtures.
- **Run in isolation**: `run_process(p, world, start=…, seed=1)` → event stream + deliverable
  specs + KG mutations. `run_playbook(...)` for the triggering graph.
- **Built-in conformance suite, applied to *every* process for free** (author writes none):
  I1 timestamps in window + working hours · I2 monotonic timeline / `after`+durations honored
  · I3 participants ⊆ bound roles (exclude/distinct respected) · I4 comment threads
  well-formed (replies resolve to a parent) · I5 **dynamic `declares` conformance** (critical
  for `impl`) · I6 **determinism** (two seeded runs ⇒ identical streams) · I7 KG effects
  reference real entities · I8 no nondeterministic primitives.
- **Custom assertions** (fluent): `result.events("CommentPosted").count in 2..8`,
  `result.deliverable("design_doc").reviewers ⊆ team`, `result.decision_made()`.
- **Golden snapshots** of the (seeded, stable) event/deliverable stream for regression.
- **Playbook invariants**: P1 every `OnEvent` trigger has an emitter (no dead trigger) ·
  P2 all activations reachable · P3 cycles guarded/bounded · P4 `deliverable_expectations`
  covered · P5 staffing feasible · P6 volume within bounds.

### Tier 3 — Evaluators (`enterprise-sim eval`)
- **Structural realism metrics**: comments-per-reviewer distribution, working-hours
  adherence, cadence plausibility, role-participation balance.
- **Optional LLM-as-judge** on a sampled artifact for content realism (uses §7 provider).

---

## 14. The `author-playbook` skill

A real, invokable skill (`skills/author-playbook/`) so an agent can take "here's a new
business domain" → a working, validated playbook with the core untouched. Contents:

1. **Model primer** — process vs playbook; the six triggers with *when to use each*;
   selectors; steps; `declares`.
2. **Authoring workflow** — describe domain & goal → identify entities/roles (incl.
   external) → decompose work into processes → per process: roles/steps/events/deliverables/
   effects + triggers → compose activations + triggering graph → declare expectations.
3. **Validation loop (the heart)** — run `lint`, read diagnostics; scaffold a test with
   `ProcessTestKit`; rely on the auto-conformance suite; add domain assertions; run `eval`;
   iterate to green. The skill documents the test capabilities so the agent self-verifies.
4. **Pattern library** — the three §12.3 playbooks as copy-adaptable recipes (gated
   event-chain, recurring cadence + feature-event, condition cascade + external counterparty
   + stateful lifecycle + `impl`).
5. **Acceptance checklist & anti-patterns** — lints clean; conformance + custom tests pass;
   eval above threshold; expectations covered. Avoid nondeterminism, over/under-declaring,
   dead triggers, unguarded cycles.

This is what makes the system **self-extending**: new domains arrive as authored-and-tested
plugins.

---

## 15. Event simulator: scheduler & actor/relationship resolver

Layer B's engine. Two intertwined jobs — **drive time + fire triggers** (the scheduler) and
**bind participants** (the resolver) — and it is the most direct determinant of corpus
realism. It is what conformance invariants I1–I4 (§13) assert against.

### 15.1 Deterministic discrete-event simulation

The engine is **event-driven**, not fixed-tick: a min-heap priority queue of future
`ScheduledActivation` / `ScheduledStep` items keyed by timestamp. Pop the earliest, execute
it (which may emit events and enqueue more), advance the clock to it. This maps the trigger
taxonomy cleanly:

- **`OnCadence`** seeds recurring firings; each firing enqueues its successor.
- **`Probabilistic`** is **pre-sampled** over the window from the seeded RNG (inter-arrival
  times → enqueued), keeping it deterministic.
- **`OnEvent`** is reactive: a step's emitted event is matched against `OnEvent`
  subscriptions and enqueues activations — this is also how **gates** and **cascades** run.
- **`OnCondition`** subscribes to the **KG effects that can change its predicate**; when an
  event's `effects` mutate a referenced attribute, only that condition re-evaluates. A coarse
  daily safety-tick catches purely time-based predicates. (Conditions thus reuse the reactive
  machinery rather than polling.)

**Determinism principle (D26): scheduling is deterministic-sequential; only rendering
parallelizes.** The scheduler emits a fully-ordered event log first — ties broken by a stable
key `(timestamp, process_priority, instance_id, step_id)`, never insertion race; every
placement draws from a **split RNG sub-stream** seeded by `(root, scenario, process,
instance, step)`. Layer C then parallelizes over that frozen log, so concurrency never
changes *what* happens, only how fast it renders. This is what makes invariant I6 hold.

### 15.2 Working-time model & step placement (D27)

- A `WorkingCalendar` answers `is_working(t)`, `advance(t, n_business)`, `next_free_slot(...)`.
  v1 is **business-hour granularity** on weekday 9–17 local windows; tz-aware per-person +
  holidays later.
- Step `duration` is measured in **working time**. A spanning step (e.g. a 3–5 business-day
  review window with `Spread(per_actor="1..3")`) distributes each reviewer's comments across
  their working hours in the window — seeded, realistically clustered (bursts; some early,
  some late), avoiding busy slots. Threading is emitted as ordered `CommentPosted` events with
  `in_reply_to` parent links; the scheduler owns *timing/structure*, the producer owns *text*.
- **Greedy soft-constraint placement**: a per-person **busy map** is filled as meetings and
  authoring land; new activities prefer free slots and overlap only if forced — logging a
  validation issue when they must. Believable, mostly-non-overlapping calendars without a
  solver. **Per-person calendars derive from the busy map.**

### 15.3 Actor/relationship resolver (D28)

Binds a `Selector` to real people via: **candidate query** (KG filter by team / expertise /
seniority / role) → **exclude** → **rank** → **sample `count`** (range → seeded draw) →
**availability bias** (prefer free-near-the-needed-time).

Collaboration realism is **affinity + capacity**:
- Layer A seeds **latent affinities** (who tends to work with whom, from team + expertise +
  proximity).
- `rank_by` combines **affinity** (preferential — go-to experts surface), **inverse current
  load** (capacity cap — nobody is on every review), and **expertise match**.
- Picking someone **reinforces the affinity** (preferential attachment), so frequent-
  collaborator clusters self-organize over the run, with capacity caps preventing overload.
  The preferential-vs-load balance is a tunable knob.
- The resolver **writes relationship edges** (`reviews_for`, `collaborates_with`, …) as it
  binds — directly populating the relationship layer of the KG.

### 15.4 Output

A fully-ordered, deterministic **event log** + the KG mutations it implies
(`Artifact`/`Meeting`/`Comment`/`Decision` nodes, edges, per-person `CalendarEvent`s). This
frozen log is exactly what Layer C renders and Layer D exports.

---

## 16. LLM orchestration & prompt assembly

Serves both Layer A (world-building) and Layer C (producers): turn a timestamped `WorldView`
projection into **grounded, cheap, reproducible** content over the §7 provider backends.

### 16.1 Layered, cache-aware prompt assembly (D29)

Each call is assembled stable→volatile so prompt caching pays off:

```
[ system prompt           ]  per artifact-kind; cache across the run
[ company profile          ]  cache across all artifacts
[ scenario/project context ]  cache across a scenario's artifacts   (≤4 cache breakpoints)
──────────────────────────── cache_control breakpoint
[ task brief + roster      ]  unique: event payload, deliverable kind, participants,
                              timestamp, candidate reference set
```

The stable blocks carry `cache_control`. **Layer C must order the render phase clustered by
shared prefix** (by scenario/project) so the cache stays warm within its TTL — a concrete
constraint on the parallel render scheduling. The `claude_cli` backend can't set breakpoints,
so caching benefits api/bedrock (the default for big runs, D13). **Prompt templates are owned
by each generator/producer plugin**; context-assembly + system prompts are shared infra.

### 16.2 Grounding (D30)

Three layers keep prose consistent with the KG:
1. **Constrained input** — the `WorldView` contains only real, in-window entities, and the
   prompt includes an explicit **roster** ("refer only to these people, by these names").
2. **Templated references** — author lines, attendee lists, "reviewed by X" are filled from
   **bound roles**, not generated; only prose is LLM-written.
3. **Detect + one repair pass** — the mention tagger (§11.3) scans output; an unresolved
   name-like token triggers a single repair re-prompt; if still bad, log a validation issue
   and keep the artifact (D17).

### 16.3 Generation modes & reference capture (D32)

- **`generate_structured(schema)`** — tool-use / JSON-schema forced output for world-building
  (person attributes, milestones) and artifact metadata/outlines. Low temperature.
- **`generate_content()`** — prose bodies, returning `{ content, references_used: [artifact_id] }`.
  The task brief supplies a candidate **reference set** (recent relevant artifacts/decisions
  from the `WorldView`); the model weaves in citations and **reports which it used**; we verify
  against the supplied set and create `references` edges (D16). Slightly higher temperature.

### 16.4 Provider mechanics & cost

One `LLMClient` over api / bedrock / cli with cross-cutting concerns:
- **Retry** w/ backoff (respect `Retry-After`); **bounded-concurrency** semaphore (the Layer C
  parallelism); per-backend rate limits.
- **Cost accounting** — per-call input/cached/output tokens → per-run aggregate → $ via a
  pricing table; enforce the **ceiling** and emit a **dry-run estimate** (task count × est
  tokens) before big runs (D13).
- **On-disk response cache** keyed by `(prompt_hash, model)` (D31) — cheap reproducible
  re-runs; only changed artifacts regenerate.
- **`fake`/echo backend** (D31) — deterministic templated placeholder content so the §13 test
  kit runs with **no real LLM calls** (free, fast, deterministic).
- **Determinism caveat** — we never rely on LLM determinism; the *structure* (which calls, what
  context, what order) is deterministic, content varies. Prompt + response caches aid repeatability.
