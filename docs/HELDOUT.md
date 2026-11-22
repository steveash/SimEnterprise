# Held-out worlds

`enterprise-sim world heldout --seed-file FILE --out DIR [CONFIG ...]` renders one held-out
variant of each world family (default: every `examples/worlds/*.toml`) into `DIR`. The
coordinator runs it into the held-out store; neither the seed file nor the worlds enter a
repository.

**Invariant.** Neither the seed file nor any output lives inside a git repository. One check,
`enterprise_sim.worlds.heldout.outside_worktrees`, guards every path argument (the seed file,
the output directory, each world directory). It refuses a path when it or any ancestor holds
`.git`, whether that is a checkout directory or a linked-worktree file. It also refuses a path
inside a bare or common git directory (`HEAD`, `objects/`, `refs/`). The refusal happens before
anything is read or written. `tests/test_heldout.py` holds the table: 3 path arguments x 3
repository kinds.

**Seed file.** The seed file is the held-out secret of decision record 0015: exactly one line,
`spindle-heldout-seed/1 <64 lowercase hex digits>`, with at least eight distinct digits. It is
written from the OS random source by `new-heldout-seed`. One seed file serves both the dataset
builder and this generator, and labels separate the two derivations. Any other content is refused,
and the error never quotes it.

**Derivation.** The secret is the seed file's 64 hex digits. For family `F` (the config's
file stem), the world seed is the first 8 bytes of `HMAC-SHA256(secret, "seed" || F)`, big-endian (the full 64 bits). The company name is a
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

**Strength residual.** Decision record 0015 asks for 128 unguessable bits behind a
held-out corpus. The secret has 256 bits and the world seed 64. Every RNG stream
SimEnterprise draws from is seeded by a 64-bit sub-seed (`derive_subseed`, BLAKE2b with
an 8-byte digest), so a wider root seed would add no strength. Recovering one stream
costs at most about 2^64 work, and reveals only that stream. Widening the sub-seeds
to 128 bits would change the bytes of every world ever built. It is not done.
