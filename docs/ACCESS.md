# Per-document reader lists (`simenterprise-access/1`, decision record 0130)

A test set is informative only where the no-Spindle floor fails. When a set engine knows which documents only certain
people may read, it can place a deciding fact where only its holder can read it. `access.json` says who may read
each restricted document of an export.

```
enterprise-sim world export WORLD_DIR --out DIR --with-access   # writes access.json (gold), checked
enterprise-sim world access-check EXPORT_DIR                    # the check over an export
```

The lists are **controller-only** gold: never in a file a persona reads. `access_plants` in the catalog is not this; it
is the credential-plant flag of decision 0003.

## Which documents are restricted

`enterprise_sim.exporters.access.access_lists` is the one function that decides, from the export's `kg/` and
`config.json`:

| Rule | Readers | Reason |
|---|---|---|
| the document mentions a person's sensitive category (`mentions_sensitive`) | its authors, reviewers and the people it is about | the category (compensation, health, ...) |
| it expresses a walled project (decision 0036) | that project's staff | `walled` |
| `[access] team_restricted_rate` picks it (a hash of the artifact id, among documents no other rule restricts) | the members of its authors' teams | `team` |

When several rules apply, the readers are the intersection, and the authors are always readers, so a list is never
empty. `[access]` is export metadata: it is not part of the world id, and at its default (0) it is not in the stored
config.

## The file

`{format, world_id, documents: [{path, artifact, readers, teams, reasons}]}`, one row per restricted document,
sorted by export path. `readers` are person ids, sorted and unique, and already include the members of `teams`.

## The check (fails closed)

`check_access` (`world access-check`, and the export itself before it finishes) refuses any of these:
- a list that is not the function's output for this export;
- a restricted document with no readers;
- a reader outside the roster;
- an author who is not a reader;
- a walled document readable by anyone off its project's staff.

The knowledge layer's restricted carriers carry the same `readers` (`docs/KNOWLEDGE_LAYER.md`).
