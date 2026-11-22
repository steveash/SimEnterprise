# Held-out worlds

`enterprise-sim world heldout --seed-file FILE --out DIR [CONFIG ...]` renders one held-out
variant of each world family (default: every `examples/worlds/*.toml`) into `DIR`. The
coordinator runs it into the held-out store; neither the seed file nor the worlds enter a
repository.

**Invariant.** Neither the seed file nor any output lives inside a git worktree. One check,
`enterprise_sim.worlds.heldout.outside_worktrees`, guards every path argument (the seed file,
the output directory, each world directory). It refuses a path when it or any ancestor holds
`.git`, whether that is a checkout directory or a linked-worktree file. The refusal happens before
anything is read or written. `tests/test_heldout.py` holds the table over every entry point.

**Derivation.** The secret is the seed file's bytes, at least 16. For family `F` (the config's
file stem), the world seed is `HMAC-SHA256(secret, "seed" || F)` mod 2^31. The company name is a
word pair picked by `HMAC-SHA256(secret, "name" || F)`, so a held-out world cannot be matched to
its public twin by name. Nothing derived is printed. The derived seed is stored in each world's
`.world`, inside the store, so the world can be rebuilt.

**What the seed changes.** Every seeded draw changes:
- people and org shape;
- every fact value and its change dates;
- assertions (stale, conflicting, hedged), surface noise, chat, and data rows.

No fact is fixed by the config. Measured on coding-team-clean over 40 seeds:
- **Free-valued attributes rarely repeat.** Owner gave 56 distinct values in 59 draws; target date
  gave 50 in 63.
- **Closed-vocabulary attributes repeat by chance.** These draw from banks of 3-8 values (status,
  priority, cadences, datastores, budgets). Two seeds give the same value with probability about
  1/k. In five held-out worlds, 3 of 11 shared facts kept the public value.

If a held-out set needs every fact to differ from the public twin, that needs a draw that avoids
the twin's values. It is not implemented.

**Determinism.** The same seed file renders byte-identical worlds (`artifacts/`, `kg/`,
`evals/`, `organization/`, `data/`). A different seed file gives a different seed, name, world id
and fact set.

Live prose is available with `--live`; it goes through the call governor like every live build.
