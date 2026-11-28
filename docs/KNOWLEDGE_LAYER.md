# The knowledge layer (`simenterprise-knowledge/1`)

A test set plants knowledge in a team's documents and asks whether an agent finds the
current version, its owner, and the documents that mislead. Writing that by hand for
every world is the cost that keeps most exported worlds unused. The knowledge layer is
the same information read out of a world's gold graph, in one file a set engine can
consume:

```
enterprise-sim world knowledge-layer EXPORT_DIR --out knowledge.json   # build, check, write
enterprise-sim world knowledge-layer EXPORT_DIR --check knowledge.json # check an existing one
```

It reads the export only (`export.json`, `config.json`, `kg/`, and the documents' text),
so it works for every exported world, live-prose ones included, with no model call. The
same export gives the same bytes. Building writes nothing unless the check below
passes; both commands exit 1 on a problem and print each one.

The layer is **gold**: it names which documents are wrong and who knows what. It
belongs beside the controller, never in an agent's workspace.

An export can carry its own layer: `enterprise-sim world export WORLD_DIR --out DIR
--with-knowledge` writes it as `knowledge/knowledge.json`, listed in `export.json` with
role `gold` (the catalog importer files gold under controller-only `meta/`). That copy
has `export_digest: null`, because `export.json` lists it and so binds it: the check
then requires the file `export.json` lists to be this layer, byte for byte. The export
refuses to finish if its layer does not hold. A sidecar layer of an export made without
the flag names that export's digest instead.

## Where it comes from

The fact ledger (`enterprise_sim/facts/ledger.py`) gives each project a few facts
(its owner, budget, review cadence, vendor and so on), each a chain of dated versions.
Each document that mentions a fact carries a gold `asserts` edge with a *stance*:

| Stance | Meaning | In the layer |
|---|---|---|
| `current` | states the version true at the document's date | a carrier of that item |
| `update` | announces a change to it | a carrier; its authors are the item's `announcer`s |
| `hedged` | states the true version with a hedge ("I believe ...") | a carrier |
| `retraction` | says a version was an error | the item's `retracted_in` |
| `stale` | states a superseded version as if it were current | `misleading`, kind `stale` |
| `conflicting` | states a value that was never true | `misleading`, kind `conflicting` |

`duplicate_of` edges (a document's near-copy, such as a `-v2-final` file) become
`misleading` entries of kind `near_duplicate`. A near-copy repeats its original's
statements, but it is never a carrier, an announcement or a retraction, and its authors
are not holders through it: it only misleads. (A stale or conflicting statement in a
near-copy is still listed as such.)

## The file

Top level:

| Key | Content |
|---|---|
| `format` | `"simenterprise-knowledge/1"` |
| `world_id`, `export_digest` | the world, and the sha256 of the export's `export.json` the layer was read from (null inside the export) |
| `window` | `{start, end}`: the simulated period |
| `counts` | items, facts, supersessions, carriers, misleading, holders |
| `items` | one per fact version, ordered by fact id and version |
| `supersessions` | one per change of a fact |
| `misleading` | the misleading documents, ordered by date and path |
| `people` | every person who holds an item: id, name, department, team, title, `joined_at`, `left_at`, `replaces` (the person they succeeded) |

An **item**:

| Key | Content |
|---|---|
| `id` | `<fact id>@v<n>`, unique in the file |
| `fact`, `version` | the fact and the version number (1 is the first) |
| `kind` | `decision` (a choice made: owner, vendor, datastore, metric definition), `policy` (a standing rule or cadence: review cadence, freshness SLA, retention), `target` (a planned number or date: budget, launch date, open roles) or `status` (status, priority). From the attribute catalog (`Attribute.kind`). |
| `attribute`, `label`, `question` | the attribute name, its noun phrase, and the question the fact answers |
| `subject` | the project: `{id, name, department}` |
| `value`, `canonical` | the value as stated, and its whitespace-free token (what a grader matches) |
| `statement` | one plain sentence: "The `label` of `subject` is `value`." |
| `valid_from`, `valid_to` | valid time (`valid_to` is null for the last version) |
| `retracted_at`, `retracted_in` | when the version was found to be an error, and the documents saying so |
| `supersedes`, `superseded_by` | the item before and after it in the chain (null at either end) |
| `holders` | `[{person, as}]`: `announcer` (wrote a document announcing it), `author` (wrote a document stating it), `owner` (owns the initiative the project is under). A person is listed once, with the strongest role in that order. |
| `carriers` | `[{path, stance, at, verbatim, readers?}]`: every document (other than a near-copy) stating the item as true; `verbatim` says whether its exported text holds the item's `value` byte for byte (a planted typo or a live model's paraphrase can drop it, and then `verbatim` is false); `readers`, only on a restricted document, is its reader list (person ids) from `docs/ACCESS.md`'s one function (controller-only) |

A **supersession**: `{fact, old, new, effective, announced_in}`: the item replaced, the
item replacing it, the new item's `valid_from`, and the documents announcing the change.

A **misleading** entry: `{kind, path, at, ...}`. `stale` and `conflicting` add `fact`,
`stated_value`, `misleads_about` (the item true at the document's date) and
`stated_item` (for `stale`, the superseded item it states; null for `conflicting`).
`near_duplicate` adds `duplicate_of` (the original document's path).

Paths are export paths (as listed in `export.json`); people are KG person ids.

**Times.** A world runs on one clock with no time zone. The layer writes its times as
`YYYY-MM-DDTHH:MM:SS` with no offset, and dates (a document's `at`) as `YYYY-MM-DD`. A
reader that needs instants takes them as UTC: append `Z`, and refuse any value of
another shape. Ids are `<type>:<slug>` (`person:ada-diaz`, `project:run-sales-sales`);
the part after the colon is a lowercase slug a reader can use as its own identifier.

## Concurrent workstreams (`[concurrency] days`)

A world with concurrent days (`enterprise_sim.facts.concurrency`) has a `workstreams` key. A world without them has
no such key. On each concurrent day two teams work the same hours: project B cites a fact of project A, the fact
changes at a mid-day time `T`, A's discoverer writes it down at `T`, and B's two workers write at `T - 2h` (the old
value, then true) and `T + 2h` (the old value: they have not heard). One entry per day:

| Key | Content |
|---|---|
| `day`, `fact`, `item` | the day, the fact and its new version (an item id) |
| `discovered_at`, `discoverer` | `T`, and the person who found it |
| `owner_team` | `{project, people}`: the project that owns the fact, with its staff |
| `discovery` | `{artifact, path, at, stated_item, verbatim}` of the discovery document, plus `readers` and `everyone` when the export has `access.json` |
| `needed_by` | `{project, people, before, after}`: the citing team, its two workers, and its two documents (`{artifact, path, at, stated_item, verbatim}`) |
| `window` | `{start, end}`: `T - 2h` to `T + 2h` |
| `answer` | `{value, canonical}`: the new value, which the citing team's work needs |
| `carried_in_tables` | the exported table files that hold the new value's token in any row, as typed evidence: a table a set renders whole gives the answer away, so leave it out of the environment |
| `carried_by_window_end` | the same, counting only rows dated on or before the window's end (undated rows always count): the tables that hold the value at the day itself |
| `costly_facts` | up to two more items of the needing project, in force at the window's end, each with the table files that hold its token in a row dated on or before the window's end: a fact a publisher reaches through a costly path |

A workstream document's `verbatim` is the carrier rule: its exported text holds its stated item's `value` byte
for byte. The prose noise (`[messiness]`) leaves a planted document's value as written: a value in another
surface form (`07/06/2026` can be either day-first or month-first) is not a sound grade. A live model's paraphrase
can still drop it, and then `verbatim` is false; a set engine uses only documents whose flag is true.

**Carried by the window's end.** A table row counts unless any of its date- or timestamp-typed cells (the
column types in `export.json`'s `tables`) is later than the window's end. Cell and window end are compared as
strings, cut to the shorter, so a date compares by day. An empty cell is ignored, and a row with no dated cell
(every row of a table with no such column) always counts. The value must be a whole token of a text cell (no
letter or digit next to it) or a whole cell of a number or other non-date type. A date or timestamp cell is never
evidence of a value: it dates its row (a daily table's `2026-03-27` row does not state a target date of 2026-03-27).
A header never counts.

## Caveats (`[caveats] count`)

A world with caveats (`enterprise_sim.facts.caveats`) has a `caveats` key, a `counts.caveats`, and on each item that
has one a `caveats` list of caveat ids; a world without them has none of these keys. A caveat is a decision's
exception or adjustment that changes the value a consumer must use: "Of that budget, $57k is held back until the Q3
review, so $343k is spendable now" (an `adjustment`: the value to use is derived from the base), or "The EU rollout
follows on 2026-06-24" (an `exception`: a scope gets its own value). One document of the owner project states the
value and the caveat together; every other document states the base value only, so a consumer that has not read
the caveat computes `value_before`. Caveats take several attributes per world (budget, launch date, p99 latency
target, gross margin target), one per fact, never a concurrent day's fact. A value after is never round (no multiple
of 5 of its shown unit; a date moves by a number of days that is neither a multiple of 5 nor of 7), and it is never a
value of the fact's chain. The prose noise keeps it as written. One entry per caveat:

| Key | Content |
|---|---|
| `id`, `item`, `fact` | `caveat:<item id>`, the base item and its fact |
| `kind`, `scope` | `adjustment` or `exception`, and the scope the value after holds in ("spendable now", "the EU rollout") |
| `question`, `statement` | the question the value after answers, and the base statement followed by the caveat sentence |
| `value_before`, `value_after`, `canonical_after` | the base value, the value to use, and its whitespace-free token |
| `typed_after` | the value after typed: `{type: money, unit: USD, amount}`, `{type: date, value}`, `{type: duration, unit: ms, amount}` or `{type: percent, unit: "%", amount}` |
| `arithmetic` | for a derived value, how it is decided: `{op: subtract, base, by, result, by_value}` (money), `{op: add_days, base, by, result}` (date), `{op: subtract_points, base, by, result}` (percent); null for an exception set outright (latency) |
| `valid_from`, `valid_to`, `author` | when the caveat was written (the document's time), the end of the base version, and its author |
| `carriers` | `[{artifact, path, at, verbatim}]`: the caveat document (and its renderings); `verbatim` means its text holds both values byte for byte; with `access.json`, `everyone` and, on a restricted one, `readers` |
| `silent_carriers` | `[{path, at, stance, readers?}]`: the base item's carriers dated on or after the caveat that do not state the value after (a consumer reading only them computes `value_before`) |
| `carried_in_tables` | the table files that hold `canonical_after` in any row, by the workstreams' typed-evidence rule |
| `reach` | with `access.json`: `{everyone, readers}` of the caveat (everyone as soon as an unrestricted carrier holds it verbatim) |

## The check

`check_layer` (and `--check`) refuses a layer when:

* its format, world id or export digest is not this export's (inside an export: it is
  not the `knowledge/knowledge.json` that `export.json` lists);
* an item id repeats;
* a carrier, retraction, announcement or misleading path is not a file of the export;
* a carrier's `verbatim` flag disagrees with the document's text (the value byte for byte);
* a carrier is a near-copy (the source of a `duplicate_of` edge);
* a carrier's `readers` is not the reader list of its document (or is present on an unrestricted one);
* the `workstreams` are not the world's concurrent days as recomputed from the export;
* the `caveats`, or an item's `caveats` list, are not those recomputed from the export alone (a fresh layer, never the
  items under check);
* a holder is not a person of the world's roster;
* a `supersedes`/`superseded_by` link names a missing item, or the chain is not
  symmetric, or a supersession or misleading entry names a missing item.

## Mapping onto a set engine

For the gym's casebook model (`Knowledge`, `Gold`): an item is a knowledge item
(`gold.kind` = `kind`, `gold.text` = `statement`, `gold.valid_from`/`valid_to`), one of
its holders is the persona that owns it, its carriers are the documents (`trace.files`)
a no-memory run must search, and a `stale` entry is an item with
`misleads_about` set. A holder whose `left_at` falls before the set's epoch is the
classic "only the person who left knew" case.
