---
name: author-playbook
description: >
  Author a new business domain for Enterprise Sim as a validated playbook +
  processes, using the declarative Python SDK and the lint -> test-kit -> eval
  validation loop — without touching the engine core. Use when someone says
  "here's a new vertical / process / scenario, make the simulator generate a
  realistic corpus for it." Covers the model (process vs playbook, six triggers,
  selectors, steps, declares), the step-by-step authoring workflow, the
  validation loop, three copy-adaptable reference patterns, and an acceptance
  checklist + anti-patterns.
version: "1.0.0"
author: "Enterprise Sim"
---

# author-playbook

This skill turns *"here is a new business domain"* into a **working, validated
playbook** that the Enterprise Sim engine runs to produce a realistic event +
artifact corpus — **with the engine core untouched**. New domains arrive as
authored-and-tested plugins; that is what makes the system self-extending
(ARCHITECTURE.md §14, decision D24).

You author against one Python package, `enterprise_sim.authoring`. You never edit
`enterprise_sim/core/**` — if you think you need a new engine primitive, you are
almost certainly missing a pattern below (the three reference playbooks in §12.3
proved very different verticals need **no new primitives**).

Everything you write is importable from the package root:

```python
from enterprise_sim.authoring import (
    # the model (§12.2 building blocks)
    Selector, Match, Role, Step, EmittedEvent, KGEffect, ConditionExpr,
    Spread, Declares, Process, Activation, Playbook, Deliverable,
    # the six triggers (§12.1)
    OnStart, OnCadence, OnEvent, OnMilestone, OnCondition, Probabilistic,
    # the quality stack (§13)
    lint_playbook, lint_process,                       # Tier 1
    run_process, run_playbook, TestWorld,              # Tier 2 execution
    check_conformance, check_playbook, assert_conforms,  # Tier 2 suite
    snapshot, assert_golden,                           # Tier 2 regression
    # the three reference patterns (§12.3) to copy-adapt
    build_software, sell_merchandise, run_clinical_study, REFERENCE_PLAYBOOKS,
)
```

---

## 1. Model primer

Two nouns. Learn the split before you write anything.

### Process — *a kind of work*

A **reusable, named work activity** that, when instantiated, plays out a **timed
sequence of steps** over one or more entities, **emitting events**, optionally
**producing deliverables**, and **mutating the knowledge graph (KG)**. A process
has internal temporal structure (a review is *draft → multi-day comment window →
revise → approve*) and is **domain-opaque to the engine** — the engine only reads
its `declares` block, never your step logic.

- **Declarative** process: a tuple of `Step`s. The common case. Fully introspectable,
  fully testable, fully lintable.
- **`impl`-backed** process: an escape hatch `impl="pkg.module:ClassName"` for logic
  the declarative steps can't express (a stateful purchase-order lifecycle, a stock
  watcher). The engine still trusts `declares`; the test kit checks the real run
  against it dynamically (conformance **I5**).

### Playbook — *a goal-oriented composition*

Scenario-level **roles** plus a set of **activations**, each wiring a process to a
**trigger**, a **role binding**, and **params/anchor**. Activations form an
**event-driven triggering graph**: one process's emitted event can trigger another
process via `OnEvent`. The playbook also states `deliverable_expectations` — its
claim about which artifact kinds it should yield (checked by invariant **P4**).

> **Everything is event-driven; time is first-class on every trigger and step.**

### The six triggers — *when to use each*

| Trigger | Fires when | Reach for it when… |
|---|---|---|
| `OnStart()` | scenario begins | the work seeds the scenario (kick off the first process, start a monitor). |
| `OnCadence(rule)` | recurring schedule | the work is periodic: `"per_sprint:2w"`, `"weekly:FRI"`, `"daily:workdays"`. |
| `OnEvent(type, where=…)` | another process/world emits a matching event | you are building a **gate** or **cascade** — process B reacts to process A's output. The `where` Match predicates narrow the match. |
| `OnMilestone(name)` | a `MILESTONE` effect announces `name` | a coarse project landmark is reached (ship, enrollment-open) — looser than a specific event. |
| `OnCondition(expr)` | a KG-state predicate becomes true | the work is **state-driven**: "stock ≤ 10", "stage == approved". Re-evaluates the instant a `KGEffect.mutate` touches the attr, plus a daily tick. |
| `Probabilistic(rate, per)` | seeded stochastic sampling | the work arrives randomly but at a known rate: adverse events, support tickets. `per ∈ {day, week, sprint, month}`. Pre-sampled from the seed, so it stays deterministic. |

### Selectors — *binding entities out of the KG*

```python
Selector(
    type="Person",                       # node type to query
    where=(Match("team", "eq", "engineering"),),  # conjunction of predicates
    exclude=("person:alice",),           # node ids to drop (e.g. the author)
    rank_by=("affinity", "inverse_load", "expertise"),  # combine ranking signals
    expertise=("pharmacovigilance",),    # tags feeding the expertise signal
    count="2..3",                        # fixed int OR "lo..hi" range string
    distinct=True,                       # never pick the same node twice (default)
    external=False,                      # True -> an out-of-org party (supplier, IRB, CRO)
)
```

- `Match(field, op, value)` with `op ∈ {eq, ne, in, contains, gte, lte}`.
- **`external=True`** is how you bind a counterparty that is *not* an employee — the
  world builder materialises it. This is the §12.3 answer to "external parties."
- A **`Role`** wraps a selector: `Role("reviewers", select=Selector(...))`. A role
  with `select=None` is **bound on activation** instead (`Activation.bind={...}`) —
  use that for a fixed author or the project the work is *about*.

### Steps — *one timed unit of a process*

```python
Step(
    id="review", by="reviewers",     # which role acts (and is booked)
    after="draft", offset="1d",      # placement: after another step's end, + working-time
    # ...or at="day 0" for an absolute offset from the instance start
    duration="3d",                   # working-time window (None => instantaneous point event)
    emits=(EmittedEvent("ReviewOpened"),),
    produces=Deliverable("design_doc", "document"),   # abstract artifact request, or None
    effects=(KGEffect.mutate("study:trial7", "stage", "approved"),
             KGEffect.milestone("design_signed_off")),
    repeat=Spread(role="reviewers", per_actor="2..5", emits="CommentPosted"),  # multi-actor sub-events
    when=ConditionExpr("study:trial7", "stage", "eq", "open"),  # guard: skip unless true
    parent_step="draft",             # causal/threading parent
)
```

- **Timing** resolves against the working calendar (Mon–Fri 9–17 by default).
  `at="day N"` and `after="step-id"` are mutually exclusive; default is `at="day 0"`.
- **`Spread`** distributes per-actor sub-events (comments) across the step's window,
  seeded and threaded back to an earlier event so comment threads stay well-formed.
- **`KGEffect`** classmethods: `.create(node_id, node_type, props)`, `.mutate(node_id,
  attr, value)`, `.add_edge(edge_id, edge_type, src, dst)`, `.milestone(name)`. A
  `MILESTONE` effect is what fires a subscribed `OnMilestone`; a `mutate` is what
  flips an `OnCondition`.

### `declares` — *the contract the engine trusts*

```python
Declares(
    events=("DesignDrafted", "ReviewOpened", "CommentPosted", "DesignApproved"),
    deliverables=("design_doc",),
    effects=("milestone:design_signed_off",),  # signature form: "mutate:attr", "milestone:name", "create:Type", "add_edge:Type"
)
```

The engine does **not** execute your steps to discover what a process does — it
reads `declares`. So `declares` must match reality:

- **Declarative** processes: lint statically checks steps against `declares` —
  emitting something undeclared is an **error**, declaring something never emitted is
  a **warning**.
- **`impl`** processes: there are no steps to read, so `declares` is trusted at lint
  time and verified dynamically by conformance **I5** at run time.

---

## 2. Authoring workflow

Work top-down, then wire bottom-up. Put your playbook in
`enterprise_sim/playbooks/<name>.py` (or a plugin package) as a function returning
a `Playbook`, mirroring `enterprise_sim/authoring/patterns.py`.

1. **Describe the domain & goal.** One sentence: *"Keep {sku} in stock by reordering
   from {supplier} when low."* That becomes `Playbook.goal_template`. Pick the
   `vertical` string (`technology`, `retail`, `pharma`, …).
2. **Identify entities & roles** — including **external** ones. Who acts? Who is an
   out-of-org counterparty (`Selector(external=True)`)? Which roles resolve from the
   KG (`select=…`) vs. are fixed on activation (`select=None` + `bind`)?
3. **Decompose the work into processes.** Each *kind of work* is one `Process`. A
   gated approval chain is several small processes wired by events, **not** one giant
   process. A stateful lifecycle is an `impl` process.
4. **For each process, fill in:** roles → steps (timing, `by`, `emits`, `produces`,
   `effects`, `repeat`, `when`, `parent_step`) → the `declares` block. For `impl`
   processes, write only `roles` + `impl` + `declares`.
5. **Compose activations + the triggering graph.** One `Activation` per (process,
   trigger) wiring. Choose the trigger per the table in §1. Make sure every `OnEvent`
   type is emitted by some other process's `declares` (or you have a dead trigger).
   Set each activation's `anchor` (the focal node the work is *for*) and any `bind`.
6. **Declare expectations.** List `deliverable_expectations` — the artifact kinds the
   playbook should yield. Invariant **P4** holds you to it.
7. **Validate** (next section) and iterate to green.

---

## 3. The validation loop (the heart)

> A process/playbook is **not "done" until Tier 1 is clean and Tiers 2–3 pass.**
> CI enforces this. The skill's whole point is that you can **self-verify** before
> handing the domain off — you do not need a human to tell you it works.

Run the three tiers in order; each is cheaper than the next and catches a different
class of defect.

### Tier 1 — static lint (`enterprise-sim lint`, no execution)

Catches what is *wrong by construction* before anything runs.

```bash
enterprise-sim lint <name|module:callable|file.json>   # one target
enterprise-sim lint                                    # all reference playbooks
```

Or in Python:

```python
from enterprise_sim.authoring import lint_playbook
res = lint_playbook(my_playbook())
assert res.ok, [str(d) for d in res.errors]   # res.ok is False iff any ERROR
for d in res.warnings: print(d)                # warnings don't fail the gate
```

Lint families (read the diagnostics — each tells you the family + location):

- **Type / schema** — timing strings, `count`/`per_actor` ranges, `OnCadence` rules,
  `Match`/`OnCondition` operators, `Probabilistic` rates all parse and are sane.
- **Reference integrity** — every `by`/`repeat.role` resolves to a bound role; every
  `after`/`parent_step` targets an existing step; step deps are acyclic; ids unique;
  `rank_by` names a real signal.
- **`declares` conformance (static)** — declarative steps match `declares` (undeclared
  emit = error; unused declare = warning).
- **Event-graph soundness** — **dead triggers** (`OnEvent` nobody emits),
  **unreachable processes** (no path from a seeding trigger), **unguarded cycles**.
- **Feasibility & volume** — count ranges satisfiable; cadence/probabilistic rates
  won't explode artifact counts (the cost linter).
- **Determinism** — an AST rule forbidding wall-clock / unseeded random in `impl`
  source (`scan_impl_source`).

`enterprise-sim lint` exits non-zero if any target has an error.

### Tier 2 — isolated test kit (deterministic execution)

Runs your process/playbook in isolation against an **auto-synthesised world** — no
hand-built fixtures — and gives you back a frozen event stream to assert against,
**plus a conformance suite that runs for free on every process** (you write none).
This is a Python API used from pytest.

```python
from enterprise_sim.authoring import run_process, assert_conforms, check_playbook

def test_my_review_process():
    res = run_process(my_review_process())   # TestWorld.satisfying(...) is synthesised for you
    assert_conforms(res)                      # raises on any I1–I8 violation

    # domain-specific assertions via the fluent query API:
    assert res.deliverable("design_doc") is not None
    assert 2 <= res.events("CommentPosted").count <= 8
    serious = res.events("AdverseEventReported").where(severity="serious")
    assert res.has_milestone("design_signed_off")

def test_my_playbook_graph():
    assert not check_playbook(my_playbook())  # P1–P6, static; empty list == clean
    res = run_playbook(my_playbook())         # drives the whole triggering graph
    assert_conforms(res)
```

**The built-in conformance suite (free on every process):**

| Code | Checks |
|---|---|
| **I1** | every event timestamp is within the window and in working hours |
| **I2** | monotonic timeline — a causal child never precedes its parent |
| **I3** | participants ⊆ bound roles (real entities; `exclude`/`distinct` respected) |
| **I4** | comment threads well-formed (every reply resolves to an earlier event) |
| **I5** | dynamic `declares` conformance — the run emits nothing it did not declare (**critical for `impl`**) |
| **I6** | determinism — a second seeded run from a fresh world emits an identical stream |
| **I7** | every KG effect references a real entity |
| **I8** | no nondeterministic primitives in any `impl` source |

**Playbook invariants** (`check_playbook`, static — also cover `impl` via `declares`):
**P1** no dead `OnEvent` trigger · **P2** every activation reachable · **P3** cycles
guarded/bounded · **P4** `deliverable_expectations` covered · **P5** staffing
feasible · **P6** cadence/probabilistic volume within bounds.

**Golden snapshots** lock the (seeded, stable) stream for regression:

```python
from enterprise_sim.authoring import assert_golden
assert_golden(run_process(my_process()), "tests/playbooks/my_process.jsonl")
# first run writes the golden; later runs diff against it. Seed defaults to 1.
```

> **`impl` scope note:** the declarative engine has no `impl` *runner* yet, so an
> `impl`-backed process runs to an empty stream under Tier-2 execution. Cover `impl`
> processes through the **static playbook suite (P1–P6)** and the **dynamic
> `declares` check (I5)** rather than expecting a populated event stream from
> `run_process` on the `impl` itself.

### Tier 3 — evaluators (`enterprise-sim eval`)

Structural realism metrics over a *completed run* — comments-per-reviewer
distribution, working-hours adherence, cadence plausibility, role-participation
balance — plus an optional LLM-as-judge on a sampled artifact for content realism
(uses the §7 provider abstraction).

```bash
enterprise-sim eval <run-output-dir>
```

> **Status:** the `eval` subcommand is the M-later tier and is currently a stub
> (`not yet implemented`). Until it lands, "Tier 3 passes" is satisfied by the
> structural assertions you write in Tier 2 (counts, distributions, working-hours
> via I1). Don't block your domain on it, but design your `deliverable_expectations`
> and assertions so they'll map cleanly onto the metrics when it arrives.

### The loop

```
write/adjust  ->  enterprise-sim lint  ->  fix errors  ->  pytest (run_* + assert_conforms + check_playbook)
      ^                                                              |
      |________________________ iterate until green _______________|
```

---

## 4. Pattern library — three copy-adaptable recipes

The three reference playbooks (in `enterprise_sim/authoring/patterns.py`, all
lint-clean and conformance-passing) are your starting templates. Import them, read
the source, copy the shape closest to your domain, and adapt. Between them they
exercise **all six triggers**, the `impl` hatch, external selectors, multi-actor
spread, and KG effects/milestones.

### A. `build_software` (technology) — cadence + feature-event + milestone

**Shape:** mostly declarative. `sprint_planning` fires `OnCadence("per_sprint:2w")`;
each plan emits `SprintPlanned`, which triggers (`OnEvent`) a `design_review` running
a multi-day, multi-reviewer comment window (`Spread`); shipping is an `OnMilestone`
retro.

**Copy this when** your domain is **recurring work + reactive review + a landmark**.
The review process is the canonical *draft → spread-comment window → approve* recipe.

```python
design_review = Process(
    name="design_review", roles=(eng_lead, reviewers),
    steps=(
        Step(id="draft", by="lead", at="day 0", duration="1d",
             emits=(EmittedEvent("DesignDrafted"),),
             produces=Deliverable("design_doc", "document")),
        Step(id="review", by="reviewers", after="draft", duration="3d",
             emits=(EmittedEvent("ReviewOpened"),),
             repeat=Spread(role="reviewers", per_actor="2..5", emits="CommentPosted"),
             parent_step="draft"),
        Step(id="approve", by="lead", after="review",
             emits=(EmittedEvent("DesignApproved"),),
             effects=(KGEffect.milestone("design_signed_off"),),
             parent_step="review"),
    ),
    declares=Declares(
        events=("DesignDrafted", "ReviewOpened", "CommentPosted", "DesignApproved"),
        deliverables=("design_doc",), effects=("milestone:design_signed_off",)),
)
```

### B. `sell_merchandise` (retail) — condition cascade + external party + stateful `impl`

**Shape:** an `impl` `inventory_monitor` watches stock and emits `LowStock`; an
`OnCondition("stock_level ≤ 10")` opens a `supplier_negotiation` with an **external**
supplier; the negotiation's `NegotiationClosed` triggers (`OnEvent`) a **stateful**
`purchase_order` process whose lifecycle (draft → approved → sent → received) lives
behind `impl`.

**Copy this when** your domain has **state-driven work**, an **out-of-org
counterparty**, or a **lifecycle the declarative steps can't express**.

```python
supplier = Role("supplier", select=Selector(type="Supplier", external=True, count=1))

purchase_order = Process(
    name="purchase_order", roles=(buyer, supplier),
    impl="enterprise_sim.processes.purchase_order:PurchaseOrder",   # escape hatch
    declares=Declares(
        events=("PODrafted", "POApproved", "POSent", "POReceived"),
        deliverables=("purchase_order",), effects=("mutate:stock_level",)),
)
# wired by:  Activation(id="raise_po_on_close", process=purchase_order,
#                        trigger=OnEvent("NegotiationClosed"), bind={"supplier": ("supplier:acme",)})
```

> Keep `impl` source deterministic (no `datetime.now`, `random.*`, `uuid.uuid4`) —
> I8 / the lint AST rule will reject it. Seed your own `random.Random` if you need
> randomness.

### C. `run_clinical_study` (pharma) — gated event-chain + external regulators + probabilistic SLA

**Shape:** a linear **gate chain** wires each approval to the next via `OnEvent`
(`ProtocolApproved` → `IRBApproved` → `StudyStarted`), with the IRB/CRO bound by
**external** selectors. Adverse events arrive as a seeded `Probabilistic` stream;
each opens an urgent report with an SLA-bounded safety sign-off chain (multi-step
process emitting a `SafetySignedOff` milestone).

**Copy this when** your domain is a **sequence of gated approvals**, has **regulators
/ external reviewers**, or has **random urgent work with a response SLA**.

```python
Activation(id="irb_on_protocol", process=irb_review,
           trigger=OnEvent("ProtocolApproved"), bind={"irb": ("irb:central",)})   # gate: A's event -> B
Activation(id="ae_stream", process=adverse_event,
           trigger=Probabilistic(rate=0.5, per="week"))                            # random urgent stream
```

**Maps from concept to primitive:** gates are **event-chains** (`OnEvent`);
lifecycles are **guarded steps** (`when=`) or an **`impl`** process; simulated state
is the **`impl` hatch**; external parties are **`Selector(external=True)`**;
landmarks are **`KGEffect.milestone` + `OnMilestone`**. No new engine primitives.

---

## 5. Acceptance checklist & anti-patterns

### Done means all of these:

- [ ] `enterprise-sim lint <your-playbook>` is **clean** (zero errors; review warnings).
- [ ] A pytest covers each process with `run_process(...)` + `assert_conforms(...)` —
      the I1–I8 conformance suite passes.
- [ ] `check_playbook(...)` returns **empty** (P1–P6 hold), and `run_playbook(...)`
      drives the whole triggering graph + `assert_conforms`.
- [ ] **Custom domain assertions** pass (counts, distributions, expected deliverables/
      milestones) — not just the free suite.
- [ ] **Golden snapshot** committed for at least the headline process(es).
- [ ] `deliverable_expectations` are all covered (P4) and reflect what the domain
      should actually produce.
- [ ] Tier 3 structural expectations are expressible (and pass once `eval` lands).
- [ ] **No engine core (`enterprise_sim/core/**`) was edited.**

### Anti-patterns (each maps to a check that will fail you):

- **Nondeterminism in `impl`** — wall-clock or unseeded random. → lint AST rule / I8.
  Seed an explicit `random.Random`.
- **Over-declaring** — listing events/deliverables/effects in `declares` you never
  emit. → static `declares` warning. Trim it.
- **Under-declaring** — emitting something not in `declares`. → static error (declarative)
  / I5 (impl). The engine would never see it; declare it.
- **Dead triggers** — an `OnEvent(type)` no process emits. → event-graph soundness / P1.
  Wire an emitter or remove the activation.
- **Unreachable processes** — an activation with no path from a seeding trigger
  (`OnStart`/cadence/condition/probabilistic/external milestone). → P2.
- **Unguarded cycles** — an event loop with no damping guard (`when=` / milestone /
  condition gate). → P3 / lint. Runaway artifact risk.
- **Infeasible staffing / exploding volume** — `count` minimums the world can't satisfy,
  or cadence/probabilistic rates that blow up artifact counts. → P5 / P6 / cost linter.
- **One giant process** instead of small processes wired by events. Decompose; let the
  triggering graph express the flow.
- **Reaching into engine internals** — importing from `enterprise_sim.core.sim.spec`
  etc. Author only against `enterprise_sim.authoring`; the SDK lowers to the spec for you.

---

**See also:** `ARCHITECTURE.md` §12 (authoring format), §13 (validation tiers),
§14 (this skill); `enterprise_sim/authoring/{sdk,patterns,lint,testkit}.py` for the
authoritative API; `tests/test_testkit.py` and `tests/test_lint.py` for worked
usage.
