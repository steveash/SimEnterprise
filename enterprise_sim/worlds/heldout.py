"""Held-out world variants from a secret seed file kept outside every repository.

``enterprise-sim world heldout --seed-file FILE --out DIR [CONFIG ...]`` renders one
held-out variant of each world family (by default every ``examples/worlds/*.toml``)
into ``DIR``, for the coordinator's held-out store.

Invariant: neither the seed file nor any output of this command lives inside a git
repository. :func:`outside_worktrees` is the one check every path argument goes through
(the seed file, the output directory, and each world directory written). It refuses a
path when the path or any ancestor directory holds ``.git`` (a checkout's directory or
a linked worktree's file), or is itself a git directory (a bare or common git
directory: ``HEAD``, ``objects/`` and ``refs/``). The check runs before anything is read
or written.

The seed file is the held-out secret of decision record 0015: exactly one
line ``spindle-heldout-seed/1 <64 lowercase hex digits>`` with at least eight distinct
digits, written from the OS random source by ``new-heldout-seed``. One seed file thus
serves both the dataset builder and this generator; the derivations are separated by
their labels.

Derivation: for family ``F`` (the config's file stem) the world seed and the company
name come from HMAC-SHA256(secret, label + F), so

* the same seed file renders byte-identical worlds (a world is a pure function of
  config, seed and code);
* a different seed file changes the world seed, hence every seeded draw: people, org
  shape, fact values and their change dates, assertions, noise and data rows; and the
  company name, so a held-out world cannot be matched to its public twin by name;
* one family's derived seed reveals nothing about another family's or the secret.

Each fork's world directory gets a ``.heldout`` marker, ``{"family", "base_config_digest"}``:
the family (the config file's stem, UTF-8) and the config digest of the public world
it is a variant of (the ``config_digest`` its release states). ``world export`` copies
it into ``export.json`` as ``heldout``, so an importer can find the base release, check
that the fork's config equals the base's except ``seed`` and ``company.name``, and
recompute both from the secret. A world that is not a fork has no marker and no field.

Nothing derived is printed: the summary names each family's world id and directory.
The derived seed is inside each world's ``.world`` (it has to be, to rebuild it); that
file lives in the held-out store, never in a repository.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from enterprise_sim.core.config import WorldConfig

__all__ = [
    "HELDOUT_MARKER",
    "HeldoutError",
    "add_heldout_parser",
    "default_families",
    "heldout_config",
    "outside_worktrees",
    "read_secret",
]

HELDOUT_MARKER = ".heldout"
_SEED_TAG = "spindle-heldout-seed/1"
_SEED_LINE = re.compile(rf"{re.escape(_SEED_TAG)} ([0-9a-f]{{64}})")
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
        if (directory / ".git").exists() or _is_git_dir(directory):
            raise HeldoutError(
                f"{role} {resolved} is inside a git worktree ({directory}); "
                "held-out seeds and worlds must live outside every repository"
            )
    return resolved


def _is_git_dir(directory: Path) -> bool:
    """A bare or common git directory: ``HEAD`` beside ``objects/`` and ``refs/``."""
    return (
        (directory / "HEAD").is_file()
        and (directory / "objects").is_dir()
        and (directory / "refs").is_dir()
    )


def read_secret(seed_file: Path) -> bytes:
    """The secret of ``seed_file``: its 64 hex digits (record 0015's seed-file format).

    The file lives outside every repository and holds exactly one line
    ``spindle-heldout-seed/1 <64 lowercase hex digits>`` with at least eight distinct
    digits, so a hand-typed phrase, a date or a repeated pattern is refused. The
    error never quotes the file's content.
    """
    path = outside_worktrees(seed_file, role="seed file")
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise HeldoutError(f"cannot read seed file {path}: {type(exc).__name__}") from exc
    lines = text.splitlines()
    match = _SEED_LINE.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None or len(set(match.group(1))) < 8:
        raise HeldoutError(
            f"seed file {path} is not one line '{_SEED_TAG} <64 lowercase hex digits>' "
            "with at least eight distinct digits (write one with new-heldout-seed)"
        )
    return match.group(1).encode("ascii")


def _draw(secret: bytes, label: str, family: str) -> int:
    digest = hmac.new(secret, f"{label}\x00{family}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")


def heldout_config(config: WorldConfig, secret: bytes, family: str, out: Path) -> WorldConfig:
    """``config`` with the family's derived seed and company name, rooted at ``out``."""
    # The full 64 bits: SimEnterprise hashes the root seed into 64-bit sub-streams
    # (core.config.seed.derive_subseed), so a wider root would add no strength.
    seed = _draw(secret, "seed", family)
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
        base = load_config(path)
        config = heldout_config(base, secret, family, root)
        backend = render_backend_for(config, live=live)
        digest = compute_config_digest(config, backend=backend)
        world_id = compute_world_id(config, digest=digest)
        world_dir = outside_worktrees(root / world_id, role="world directory")
        if read_world(world_dir) is None:
            world_dir.mkdir(parents=True, exist_ok=True)
            write_world(world_dir, build_world_file(config, backend=backend, live=live))
        marker = {
            "family": family,
            "base_config_digest": compute_config_digest(base, backend=backend),
        }
        (world_dir / HELDOUT_MARKER).write_text(
            json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
        )
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


def build_heldout_from_export(
    base_export: Path, family: str, seed_file: Path, out: Path, *, live: bool = False
) -> dict[str, Any]:
    """One fork of a released world, from its export's own ``config.json``.

    The fork's config is the base's ``config.json`` with ``seed`` and ``company.name``
    replaced, recorded verbatim (``build_world_file(stored_config=...)``), so its
    exported ``config.json`` differs from the base's in exactly those two fields,
    whatever SimEnterprise version built the base. ``base_config_digest`` is the
    base's ``export.json`` ``config_digest``.
    """
    import copy

    from enterprise_sim.assembly import build_world_file, compute_config_digest, compute_world_id
    from enterprise_sim.assembly.runner import render_backend_for
    from enterprise_sim.jobs.progress import JsonlSink
    from enterprise_sim.jobs.worker import run_job
    from enterprise_sim.worlds.file import progress_log_path, read_world, write_world

    secret = read_secret(seed_file)
    root = outside_worktrees(out, role="output directory")
    try:
        base = json.loads((base_export / "config.json").read_text(encoding="utf-8"))
        manifest = json.loads((base_export / "export.json").read_text(encoding="utf-8"))
        base_digest = str(manifest["config_digest"])
    except (OSError, ValueError, KeyError) as exc:
        raise HeldoutError(f"{base_export} is not a SimEnterprise export: {exc}") from exc
    try:
        parent = WorldConfig.model_validate(base)
    except ValueError as exc:
        raise HeldoutError(f"{base_export}: its config does not load today: {exc}") from exc
    derived = heldout_config(parent, secret, family, root)
    stored = copy.deepcopy(base)
    stored["seed"] = derived.seed
    stored["company"] = {**stored["company"], "name": derived.company.name}
    config = WorldConfig.model_validate({**stored, "worlds_root": str(root)})
    backend = render_backend_for(config, live=live)
    digest = compute_config_digest(config, backend=backend)
    world_id = compute_world_id(config, digest=digest)
    world_dir = outside_worktrees(root / world_id, role="world directory")
    root.mkdir(parents=True, exist_ok=True)
    if read_world(world_dir) is None:
        world_dir.mkdir(parents=True, exist_ok=True)
        write_world(
            world_dir,
            build_world_file(config, backend=backend, live=live, stored_config=stored),
        )
    marker = {"family": family, "base_config_digest": base_digest}
    (world_dir / HELDOUT_MARKER).write_text(json.dumps(marker, sort_keys=True) + "\n", "utf-8")
    sink = JsonlSink(None, also_to=progress_log_path(world_dir))
    try:
        state = run_job(world_dir, progress=sink)
    finally:
        sink.close()
    return {
        "family": family,
        "world_id": world_id,
        "world_dir": str(world_dir),
        "status": state.status,
    }


def _cmd_world_heldout(args: argparse.Namespace) -> int:
    configs = [Path(c) for c in args.configs] or default_families()
    try:
        if args.base_export is not None:
            if args.configs or not args.family:
                raise HeldoutError("--base-export takes --family and no CONFIG")
            summary = [
                build_heldout_from_export(
                    args.base_export, args.family, args.seed_file, args.out, live=args.live
                )
            ]
        else:
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
    parser.add_argument(
        "--base-export",
        type=Path,
        help="fork a released world from its export's config.json (needs --family)",
    )
    parser.add_argument("--family", help="the family name of --base-export (its config stem)")
    parser.add_argument("--live", action="store_true", help="render prose with the real provider")
    parser.set_defaults(func=_cmd_world_heldout)
