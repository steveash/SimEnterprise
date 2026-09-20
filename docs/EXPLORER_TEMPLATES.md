# Explorer: authoring templates (departments & scenario playbooks)

> Part of [`EXPLORER.md`](./EXPLORER.md). The **Templates** view lets you create
> every kind of simulation customization — a new **department archetype**, a new
> **scenario playbook** (with its processes), or both as a bundle — by talking to
> an agent that runs the repo's [`author-playbook`](../skills/author-playbook/SKILL.md)
> skill, validates the result with the lint → test-kit loop, and saves it as a
> reusable **template** that new runs and run extensions can select.

---

## 1. What a template is

Today the "templates" of the simulator are its registered plugins: a
`DepartmentArchetypeSpec` (charter, goals, team shapes, playbooks it runs) and a
`Playbook` authored with the declarative SDK (`enterprise_sim/authoring/sdk.py`).
They live inside the package (`enterprise_sim/archetypes/`, `…/playbooks/`) and
are found by `discover()` scanning those packages.

A **template** is the same thing, living **outside the package** in a user
directory so the UI can create, validate and delete it without touching the
codebase:

```
templates/                      # GRAPH_EXPLORER_TEMPLATES_DIR, default <repo>/templates
  <slug>/
    template.json               # metadata (below)
    plugin.py                   # registers ARCHETYPES / PLAYBOOKS / PROCESSES on import — same shape as
                                # enterprise_sim/playbooks/build_software.py & archetypes/engineering.py
    test_<slug>.py              # pytest: run_playbook + assert_conforms + check_playbook (+ golden snapshot)
    <slug>.golden.jsonl         # optional testkit snapshot
```

`template.json`:
```json
{
  "slug": "clinical_trials",
  "name": "Clinical trials",
  "kind": "bundle",                    // "department" | "playbook" | "bundle"
  "description": "A biotech clinical-operations department running study playbooks.",
  "provides": {"archetypes": ["clinical_ops"], "playbooks": ["run_clinical_study"], "processes": ["…"]},
  "created_at": "…", "updated_at": "…",
  "validation": {"status": "valid" | "invalid" | "unvalidated", "checked_at": "…", "summary": "lint 0 errors / conformance ok / 3 tests passed"}
}
```

## 2. Python: external plugin discovery + `enterprise_sim.templates`

### 2.1 Discovery (`core/registry/discovery.py`, additive)

```python
def discover_paths(paths: Iterable[str | Path]) -> list[str]:
    """Import every `<dir>/*/plugin.py` (and `<dir>/*.py`) so their registrations fire; idempotent."""
def plugin_paths(config_paths: Sequence[str | Path] = ()) -> list[Path]:
    """`config.plugins` + os.environ['ENTERPRISE_SIM_PLUGIN_PATH'] (os.pathsep-split), deduped, existing only."""
```

Modules are loaded via `importlib.util.spec_from_file_location` under the
synthetic package name `enterprise_sim_templates.<slug>` and cached in
`sys.modules`, so a second discovery is a no-op (the registry rejects duplicate
names, so re-import must not happen). `build_world` and `build_corpus` call
`discover_paths(plugin_paths(config.plugins))` right after their existing
`discover(...)` calls. The `job` worker and every `templates`/`evals` CLI
command put the templates dir on `ENTERPRISE_SIM_PLUGIN_PATH`; the sidecar sets
the same variable for every process it spawns.

A template can reference built-in processes/playbooks by name (an archetype's
`playbooks=("build_software",)` is valid) and built-ins may not be shadowed
(registering an existing name is an error surfaced by `templates validate`).

### 2.2 `enterprise_sim/templates/` + CLI

```
enterprise-sim templates list      [--dir DIR]            # JSON: every template.json (+ "loadable": bool, "error")
enterprise-sim templates scaffold  --dir DIR --slug S --kind K --name N [--description D]
                                                          # writes template.json + a plugin.py/test skeleton
enterprise-sim templates validate  --dir DIR --slug S     # JSON report (below); also rewrites template.json.validation
enterprise-sim templates delete    --dir DIR --slug S
enterprise-sim templates catalog   [--dir DIR]            # JSON: built-in + template archetypes/playbooks/processes
                                                          #   (what `job catalog` embeds; the run form's dropdowns)
```

`validate` runs, in order, and reports each as `{"step", "ok", "detail"}`:
1. **import** — `discover_paths([dir])` in a fresh subprocess (so a broken file
   cannot poison the caller), diffing the registries before/after to fill `provides`;
2. **lint** — `lint_playbook()` for each provided playbook; `lint_process()` for
   standalone processes; diagnostics are returned verbatim (`code`, `severity`, `message`, `location`);
3. **conformance** — `check_playbook()` (P1–P6) and `run_playbook()` +
   `check_conformance()` (I1–I8) on the synthesized `TestWorld`, seeded;
4. **archetype sanity** — every playbook an archetype names must resolve; team
   shapes' `count` ranges parse; at least one team shape;
5. **tests** — `pytest templates/<slug>` (quiet, JSON summary of passed/failed);
6. **dry run** — `estimate_run()` on a minimal config using the template's
   archetype (`[[departments]] archetype = <name>`) over one business week on the
   fake backend: proves Layer A/B accept it and reports events/artifacts.

The report is `{"slug", "ok", "steps": [...], "provides": {...}}`.

## 3. The authoring agent (sidecar `src/sidecar/templates/harness.ts`)

The chat uses the Claude Agent SDK `query()` like the Explore chat, but with
**file tools** and the skill as its instructions:

- `cwd` = repo root; `systemPrompt` = a header describing the template contract
  (§1, §2) + the full text of `skills/author-playbook/SKILL.md` (read at runtime
  so the skill stays the single source of truth) + the reference patterns file
  path (`enterprise_sim/authoring/patterns.py`) to read for examples.
- `allowedTools`: `Read`, `Glob`, `Grep`, `Write`, `Edit`, `Bash`; `permissionMode:
  'bypassPermissions'` but with a `canUseTool` guard (`src/sidecar/templates/guard.ts`,
  unit-tested): writes/edits only under `templates/<slug>/`; reads anywhere in the
  repo; `Bash` only for commands starting with an allow-listed prefix
  (`uv run enterprise-sim templates validate`, `uv run enterprise-sim lint`,
  `uv run pytest templates/<slug>`, `uv run python -c`) — everything else is denied
  with a message the agent can read.
- The turn loop: the agent scaffolds (or the UI pre-scaffolds via `templates
  scaffold`), writes `plugin.py` + test, runs `templates validate`, reads the JSON
  report, iterates to green, and ends with a summary. Events stream to the UI
  with the existing `AgentEvent` kinds plus `{kind:'template', slug, validation}`
  emitted by the sidecar whenever a `templates validate` tool call completes
  (parsed from the tool result), so the panel's status pill updates live.
- Multi-turn (`resume` session id) so the user can say "make the review window
  longer" and the agent edits in place.

Ops (`src/sidecar/templates/index.ts`): `templatesList`, `templatesScaffold`,
`templatesValidate`, `templatesDelete`, `templatesRead` (`{slug}` → files + contents,
for the file viewer), `templatesChat` (stream) / `templatesCancelChat`,
`templatesCatalog`. `templatesChanged` is emitted after validate/delete so the
Runs form refreshes its catalog.

## 4. Renderer: the Templates view

- **Library** (left): cards for each template — name, kind, provides, validation
  pill (valid / invalid / unvalidated), updated-at. Actions: *Validate*, *Open chat*
  (resumes authoring on that template), *View files*, *Delete* (confirm).
- **New template**: name / slug / kind / one-paragraph domain description →
  scaffolds and opens the authoring chat with that description as the first
  message ("Author the `<slug>` template: <description>").
- **Authoring chat** (center): the same ChatPanel transcript UI (text, thinking,
  tool trace with expandable inputs/results), plus a **validation card** that
  renders the latest `templates validate` report step by step (lint diagnostics
  as a table). Model selector (Sonnet default; Opus for hard domains).
- **File viewer** (right): read-only view of `plugin.py` / test / `template.json`
  with copy.
- Using templates in runs: the Runs form's *Departments* and *Scenario instances*
  dropdowns list built-ins and **valid** templates (kind ≠ invalid), grouped; a
  template that is `unvalidated` is selectable with a warning.

## 5. Tests

Python: `discover_paths` loads a fixture template and registers it once;
`plugin_paths` merges config + env; `templates scaffold → validate` on the
fixture is green and fills `provides`; an intentionally broken template
reports the failing step without raising; a run with `[[departments]]
archetype = <template>` builds a world (golden-sized, one week).
Sidecar: the `canUseTool` guard (allow/deny matrix); `templatesList` parsing;
an end-to-end scaffold + validate through the Python CLI (skips without Python);
an agent turn (skips without `ANTHROPIC_API_KEY`).
