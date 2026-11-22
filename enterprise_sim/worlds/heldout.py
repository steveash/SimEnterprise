"""Held-out world variants from a secret seed file kept outside every repository.

``enterprise-sim world heldout --seed-file FILE --out DIR [CONFIG ...]`` renders one
held-out variant of each world family (by default every ``examples/worlds/*.toml``)
into ``DIR``, for the coordinator's held-out store.

Invariant: neither the seed file nor any output of this command lives inside a git
worktree. :func:`outside_worktrees` is the one check every path argument goes through
(the seed file, the output directory, and each world directory written); a path whose
directory or any ancestor holds ``.git`` is refused before anything is read or written.

Derivation: the secret is the seed file's bytes. For family ``F`` (the config's file
stem) the world seed and the company name come from HMAC-SHA256(secret, label + F), so

* the same seed file renders byte-identical worlds (a world is a pure function of
  config, seed and code);
* a different seed file changes the world seed, hence every seeded draw: people, org
  shape, fact values and their change dates, assertions, noise and data rows; and the
  company name, so a held-out world cannot be matched to its public twin by name;
* one family's derived seed reveals nothing about another family's or the secret.

Nothing derived is printed: the summary names each family's world id and directory.
The derived seed is inside each world's ``.world`` (it has to be, to rebuild it); that
file lives in the held-out store, never in a repository.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from enterprise_sim.core.config import WorldConfig

__all__ = [
    "HeldoutError",
    "add_heldout_parser",
    "default_families",
    "heldout_config",
    "outside_worktrees",
    "read_secret",
]

_MIN_SECRET_BYTES = 16
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "worlds"

# Company names for held-out variants: a word pair per family, drawn by the HMAC.
_FIRST = (
    "Alder", "Basalt", "Cedar", "Delta", "Ember", "Fjord", "Garnet", "Harbor", "Indigo",
    "Juniper", "Kestrel", "Lumen", "Marlin", "Nimbus", "Onyx", "Pinion", "Quarry", "Rowan",
    "Saffron", "Tundra", "Umber", "Vesper", "Willow", "Yarrow", "Zephyr", "Ashgrove",
    "Bramble", "Cobalt", "Driftwood", "Elmstead", "Foxglove", "Granite",
)  # fmt: skip
_SECOND = (
    "Works", "Systems", "Group", "Labs", "Partners", "Holdings", "Digital", "Analytics",
    "Collective", "Industries", "Ventures", "Software", "Data", "Logistics", "Advisory",
    "Goods",
)  # fmt: skip


class HeldoutError(ValueError):
    """A held-out run that must not proceed (a path inside a worktree, a weak seed file)."""


def outside_worktrees(path: Path, *, role: str) -> Path:
    """Return ``path`` resolved, or raise if it or any ancestor directory holds ``.git``.

    The single check for every path this command reads its secret from or writes to.
    ``.git`` may be a directory (a checkout) or a file (a linked worktree); both count.
    """
    resolved = path.expanduser().resolve()
    for directory in (resolved, *resolved.parents):
        if (directory / ".git").exists():
            raise HeldoutError(
                f"{role} {resolved} is inside a git worktree ({directory}); "
                "held-out seeds and worlds must live outside every repository"
            )
    return resolved


def read_secret(seed_file: Path) -> bytes:
    """The secret bytes of ``seed_file`` (outside every worktree, at least 16 bytes)."""
    path = outside_worktrees(seed_file, role="seed file")
    try:
        secret = path.read_bytes().strip()
    except OSError as exc:
        raise HeldoutError(f"cannot read seed file {path}: {exc}") from exc
    if len(secret) < _MIN_SECRET_BYTES:
        raise HeldoutError(f"seed file {path} holds under {_MIN_SECRET_BYTES} bytes")
    return secret


def _draw(secret: bytes, label: str, family: str) -> int:
    digest = hmac.new(secret, f"{label}\x00{family}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")


def heldout_config(config: WorldConfig, secret: bytes, family: str, out: Path) -> WorldConfig:
    """``config`` with the family's derived seed and company name, rooted at ``out``."""
    seed = _draw(secret, "seed", family) % (2**31)
    pick = _draw(secret, "name", family)
    name = f"{_FIRST[pick % len(_FIRST)]} {_SECOND[(pick // len(_FIRST)) % len(_SECOND)]}"
    company = config.company.model_copy(update={"name": name})
    return config.model_copy(update={"seed": seed, "company": company, "worlds_root": out})


def default_families() -> list[Path]:
    """Every top-level example world config (one family per file)."""
    return sorted(_EXAMPLES.glob("*.toml"))


def build_heldout(
    configs: Sequence[Path], seed_file: Path, out: Path, *, live: bool = False
) -> list[dict[str, Any]]:
    """Render one held-out variant per config into ``out``; return a summary per family."""
    from enterprise_sim.assembly import build_world_file, compute_config_digest, compute_world_id
    from enterprise_sim.assembly.runner import render_backend_for
    from enterprise_sim.core.config import load_config
    from enterprise_sim.jobs.progress import JsonlSink
    from enterprise_sim.jobs.worker import run_job
    from enterprise_sim.worlds.file import progress_log_path, read_world, write_world

    secret = read_secret(seed_file)
    root = outside_worktrees(out, role="output directory")
    families = [Path(c).stem for c in configs]
    if len(set(families)) != len(families):
        raise HeldoutError("two configs share a family name (file stem)")
    root.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for path, family in zip(configs, families, strict=True):
        config = heldout_config(load_config(path), secret, family, root)
        backend = render_backend_for(config, live=live)
        digest = compute_config_digest(config, backend=backend)
        world_id = compute_world_id(config, digest=digest)
        world_dir = outside_worktrees(root / world_id, role="world directory")
        if read_world(world_dir) is None:
            world_dir.mkdir(parents=True, exist_ok=True)
            write_world(world_dir, build_world_file(config, backend=backend, live=live))
        sink = JsonlSink(None, also_to=progress_log_path(world_dir))
        try:
            state = run_job(world_dir, progress=sink)
        finally:
            sink.close()
        summary.append(
            {"family": family, "world_id": world_id, "world_dir": str(world_dir),
             "status": state.status}
        )  # fmt: skip
    return summary


def _cmd_world_heldout(args: argparse.Namespace) -> int:
    configs = [Path(c) for c in args.configs] or default_families()
    try:
        summary = build_heldout(configs, args.seed_file, args.out, live=args.live)
    except HeldoutError as exc:
        print(f"enterprise-sim world heldout: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"worlds": summary}, indent=1))
    return 0 if all(s["status"] == "done" for s in summary) else 1


def add_heldout_parser(world_subparsers: Any) -> None:
    """Register ``world heldout --seed-file FILE --out DIR [CONFIG ...]``."""
    parser = world_subparsers.add_parser(
        "heldout",
        help="render held-out variants of each world family from a secret seed file "
        "kept outside every repository",
    )
    parser.add_argument(
        "configs", nargs="*", metavar="CONFIG", help="family configs (default: examples/worlds)"
    )
    parser.add_argument(
        "--seed-file", required=True, type=Path, help="secret bytes, outside every worktree"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="held-out store, outside every worktree"
    )
    parser.add_argument("--live", action="store_true", help="render prose with the real provider")
    parser.set_defaults(func=_cmd_world_heldout)
