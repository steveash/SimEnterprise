# The Golden Run — v1 end-to-end acceptance

This is the **first vertical slice** (PLAN.md §4): the smallest run that still
exercises the whole framework end-to-end — one department, one scenario, a small
team, over a single business week — producing a multi-format corpus (markdown,
plus Word `.docx` with native threaded comments for the document deliverables)
*and* the gold knowledge graph that serves as its **answer key**.

It is the v1 acceptance artifact (bead `esim-3481176c`). The run is deterministic
and network-free (default `fake` LLM backend), so it reproduces **byte-for-byte**
from its seed and can be checked in CI without any API cost.

- **Config:** [`examples/golden.toml`](../examples/golden.toml)
- **Acceptance test:** [`tests/test_golden_run.py`](../tests/test_golden_run.py)

---

## Reproduce it

```bash
# Render the corpus + gold KG (network-free, deterministic).
# The config's output_dir lands the run under runs/golden/.
enterprise-sim run examples/golden.toml

# Evaluate the completed run (structural realism metrics).
enterprise-sim eval runs/golden/golden-slice-co-40644d551158
```

Both commands are also runnable as `python -m enterprise_sim.cli ...`. The run id
`golden-slice-co-40644d551158` is a pure function of `(config, seed)`; it is
pinned by the acceptance test, so any change to the config that would alter the
run surfaces as a loud test failure.

A `--dry-run` first prints the artifact count and estimated cost without
rendering:

```bash
enterprise-sim run examples/golden.toml --dry-run
```

## What the run produces

```
runs/golden/golden-slice-co-40644d551158/
├── manifest.json              # run index: ids, counts, validation summary
├── config.snapshot.json       # the validated config, frozen for reproducibility
├── organization/              # Layer A reference data (markdown)
│   ├── README.md  company.md  people.md
│   └── departments/engineering.md
├── artifacts/                 # Layer C corpus, clustered per scenario
│   └── initiative-engineering-build-software/
│       ├── ...-kickoff.md  ...-groom.md  ...-plan.md
│       ├── ...-plan-draft.docx (design review, native threaded comments)
│       └── ...-status.docx     (weekly status)
├── kg/                        # the gold knowledge graph (the answer key)
│   ├── nodes.jsonl  edges.jsonl  events.jsonl
│   ├── provenance.jsonl  mentions.jsonl  aliases.jsonl
│   ├── schema.json  graph.json
│   └── neo4j/import.cypher  (+ nodes/ relationships/)
└── validation/issues.jsonl    # soft consistency findings (report-and-continue)
```

### Shape of the slice (seed = 7)

| Dimension | Value |
|---|---|
| Window | 2026-01-05 .. 2026-01-09 (one business week) |
| Departments | **1** (engineering) |
| Scenarios | **1** (`build_software` playbook) |
| People | 9 (one small engineering department) |
| Events | 24 |
| Corpus artifacts | 5 — 3 markdown (kickoff, grooming, sprint plan) + 2 Word `.docx` (design review, weekly status) |
| KG nodes / edges | 55 / 128 |
| Provenance / mention rows | 12 / 125 |

> **On "~3 people":** PLAN.md §4 sketches the slice as "~3 people". The run is
> driven by the real `build_software` reference playbook and the `engineering`
> archetype, whose smallest believable department staffs ~9 people across its
> product / platform / quality teams. We keep the *real* archetype rather than a
> synthetic 3-person stub so the slice exercises the production team structure;
> the project team a single scenario actually engages is a handful of leads,
> contributors, and reviewers. Grow the run by raising `company.size` or adding
> `[[projects]]` to `golden.toml`.

## Why the gold KG is the answer key

A run emits two coupled outputs: the **corpus** (the evidence) and the **gold KG**
(entities + relationships + events, each edge carrying provenance back to the
artifacts that express it). The KG is a trustworthy answer key because the
acceptance test proves four properties against the *actual* rendered corpus:

1. **Provenance resolves.** Every `provenance.jsonl` row targets a real KG node or
   edge and cites real corpus files on disk — and provenance covers both nodes
   *and* reified edges, not just entities (D18/D19).
2. **Mentions are exact.** Every `mentions.jsonl` locator slices its artifact to
   exactly the recorded surface form, on the line it claims, and resolves to a
   real entity node (D20) — so the corpus carries verifiable entity-recognition +
   coreference labels.
3. **No hard inconsistency.** The consistency validator finds **no** dangling
   references, scheduling conflicts, or out-of-window stamps. The KG is internally
   sound.
4. **It reproduces.** Two runs of the config to different destinations produce a
   byte-identical corpus, KG, and validation log (D10/D26/D31).

### Validation semantics (D17/D30)

`validation/issues.jsonl` is **report-and-continue**: a finding is recorded and
summarized in `manifest.json`, never raised. The golden run currently logs 5
**soft** `unresolved_mention` findings — generated prose that named a template
phrase (e.g. "Kickoff Brief", "Sprint Plan") the grounding pass could not bind to
an in-scope entity after one repair (D30). These do **not** corrupt the KG; they
are inspectable evidence that the run is "never silently lossy". The acceptance
test asserts that *no hard* consistency kind appears and that the manifest summary
faithfully indexes `issues.jsonl`.

## Acceptance checklist

A change to the simulator keeps the golden run honest when:

- [ ] `enterprise-sim run examples/golden.toml` completes and writes the layout above.
- [ ] `pytest tests/test_golden_run.py` is green (shape, answer-key, reproducibility).
- [ ] `enterprise-sim eval runs/golden/<run-id>` reports all structural metrics passing.

If the golden config changes on purpose, update the pinned run id and counts in
the acceptance test and in this document together.
