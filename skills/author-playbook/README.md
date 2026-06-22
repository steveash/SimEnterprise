# author-playbook (skill)

Agent skill for authoring playbooks and processes (ARCHITECTURE.md §14, D24):
the model, the six triggers, the authoring + validation loop, and the three
cross-vertical reference patterns.

See [`SKILL.md`](SKILL.md) for the full skill. It covers:

1. **Model primer** — process vs playbook, the six triggers (when to use each),
   selectors, steps, `declares`.
2. **Authoring workflow** — domain & goal → entities/roles → processes → steps →
   activations + triggering graph → expectations.
3. **The validation loop** — `enterprise-sim lint` (Tier 1) → the `run_process` /
   `run_playbook` test kit + built-in conformance suite (Tier 2) → `enterprise-sim
   eval` (Tier 3).
4. **Pattern library** — `build_software`, `sell_merchandise`,
   `run_clinical_study` as copy-adaptable recipes.
5. **Acceptance checklist & anti-patterns.**

Authoritative API: `enterprise_sim/authoring/{sdk,patterns,lint,testkit}.py`.
