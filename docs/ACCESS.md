# Per-document reader lists (`simenterprise-access/1`, decision record 0130)

A test set is informative only where the no-Spindle floor fails. A set engine that knows who can read a fact anywhere in
an export can place a deciding fact where only its holder can read it. `access.json` gives the reader list of each
restricted document, and the knowledge layer gives each item's reach.

```
enterprise-sim world export WORLD_DIR --out DIR --with-access [--with-knowledge]   # access.json (gold), checked
enterprise-sim world access-check EXPORT_DIR                                       # the check over an export
```

The lists are **controller-only** gold: never in a file a persona reads. `access_plants` in the catalog is not this; it
is the credential-plant flag of decision 0003.

**The direction that must never fail is too wide**, so every rule makes the uncertain case narrower.

## Which documents are restricted

`enterprise_sim.exporters.access.access_lists` is the one function that decides. It reads the export's `kg/`, its
file list and its `config.json`.

- **The unit is a family:** the artifacts that share an `event`, which are a document with its twins, drafts,
  renderings, near-copies and following chat thread. A family is restricted as a whole, and every member gets the
  family's list.
- **The rules are kinds.** Across kinds the readers intersect, and the family's authors (its `authored` edges) are
  always readers:

| Rule | Readers | Reason |
|---|---|---|
| some member mentions a person's sensitive category | the family's authors and reviewers, and every person it is about | the categories |
| the family's project is walled (decision 0036) | that project's staff | `walled` |
| `[access] restricted_projects` picks the family's project (that many unwalled projects, by a hash of the project id) | that project's staff | `project` |
| `[access] team_restricted_rate` picks the family (a hash of its event id; only families no other rule restricts and that have an author) | everyone ever a member of the authors' teams | `team` |

**A project's derived records** state its facts too: its slice's `trails/`, `registry/` and `observations/`
files. For a walled or restricted project they get the project's staff as readers (rows with `artifact: null` and
`family: project:<id>`); otherwise the reach of those facts would be everyone.

A person of the world is any Person node, leavers included. `[access]` is export metadata: it is not part of the
world id, and at its default (0) it is not in the stored config.

## The file

`{format, world_id, documents: [{artifact, family, paths, readers, teams, reasons}]}`, with one row per restricted
artifact, sorted by artifact:
- `paths` lists every exported data file of the artifact, mapped through `source_path` and `rendered_from`;
- `readers` are person ids, sorted and unique, and already include the members of `teams`.

## In the knowledge layer

Only when the export carries `access.json`:
- each restricted carrier gets `readers`;
- each item gets `reach: {everyone, readers}`. This is the union of the readers of every exported data file whose
  text holds the item's canonical token, and `everyone` is true when any such file is unrestricted.

A set engine places a deciding fact only on an item whose reach is not `everyone` and leaves out the publisher. It
never relies on one carrier's `readers`. A layer of an export without `access.json` has no access fields.

## The check (fails closed, with `--with-access`)

`check_access` (`world access-check`, and the export itself before it finishes) refuses any of these:
- a list that is not the function's output;
- a row with no readers;
- a reader who is not a person of the world;
- an author who is not a reader;
- a family whose members' lists differ;
- a walled family read off its project's staff;
- a row path that is not an exported data file;
- an exported data file of a restricted artifact that no row covers;
- a walled project's record that no row covers.

`world knowledge-layer --check` holds `readers` and `reach` to `access.json`.
