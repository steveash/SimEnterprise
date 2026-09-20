"""``enterprise-sim templates {list,scaffold,validate,delete,catalog}``.

(EXPLORER_TEMPLATES.md §2.2.)

Every subcommand prints exactly one JSON document to stdout (the sidecar
contract, ``docs/EXPLORER.md`` §3) and a one-line human summary to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from enterprise_sim.templates.catalog import build_catalog
from enterprise_sim.templates.scaffold import scaffold
from enterprise_sim.templates.store import (
    TemplateEntry,
    TemplateKind,
    delete_template,
    list_templates,
    templates_dir,
)
from enterprise_sim.templates.validate import validate as validate_template


def _resolve_dir(args: argparse.Namespace) -> Path:
    return templates_dir(getattr(args, "dir", None))


def _add_dir_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="templates root (default: $GRAPH_EXPLORER_TEMPLATES_DIR, else <cwd>/templates)",
    )


def _entry_json(entry: TemplateEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "slug": entry.slug,
        "loadable": entry.loadable,
        "error": entry.error,
    }
    if entry.template is not None:
        payload.update(entry.template.model_dump(mode="json"))
    return payload


def _cmd_templates_list(args: argparse.Namespace) -> int:
    root = _resolve_dir(args)
    entries = list_templates(root)
    payload = [_entry_json(e) for e in entries]
    print(json.dumps(payload, sort_keys=True))
    print(
        f"enterprise-sim templates list: {len(entries)} template(s) under {root}", file=sys.stderr
    )
    return 0


def _cmd_templates_scaffold(args: argparse.Namespace) -> int:
    root = _resolve_dir(args)
    try:
        template = scaffold(
            root,
            slug=args.slug,
            kind=args.kind,
            name=args.name,
            description=args.description or "",
        )
    except (ValueError, FileExistsError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        print(f"enterprise-sim templates scaffold: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(template.model_dump(mode="json"), sort_keys=True))
    print(f"enterprise-sim templates scaffold: wrote {args.slug} to {root}", file=sys.stderr)
    return 0


def _cmd_templates_validate(args: argparse.Namespace) -> int:
    root = _resolve_dir(args)
    report = validate_template(root, args.slug)
    print(json.dumps(report, sort_keys=True))
    status = "valid" if report["ok"] else "invalid"
    print(f"enterprise-sim templates validate: {args.slug} is {status}", file=sys.stderr)
    return 0 if report["ok"] else 1


def _cmd_templates_delete(args: argparse.Namespace) -> int:
    root = _resolve_dir(args)
    try:
        delete_template(root, args.slug)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        print(f"enterprise-sim templates delete: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "slug": args.slug}))
    print(f"enterprise-sim templates delete: removed {args.slug} from {root}", file=sys.stderr)
    return 0


def _cmd_templates_catalog(args: argparse.Namespace) -> int:
    root = _resolve_dir(args)
    catalog = build_catalog(root)
    print(json.dumps(catalog, sort_keys=True))
    counts = {kind: len(items) for kind, items in catalog.items()}
    print(f"enterprise-sim templates catalog: {counts}", file=sys.stderr)
    return 0


def _cmd_templates(args: argparse.Namespace) -> int:
    args.templates_parser.print_help()
    return 0 if args.templates_command is None else 2


def add_templates_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire ``enterprise-sim templates {list,scaffold,validate,delete,catalog}``."""
    templates_parser = subparsers.add_parser(
        "templates",
        help="author/validate external department & playbook templates",
        description=(
            "Manage external plugin templates that live outside the package "
            "(EXPLORER_TEMPLATES.md): list, scaffold a new one, validate it, "
            "delete it, or fetch the built-in + template catalog."
        ),
    )
    templates_subparsers = templates_parser.add_subparsers(
        dest="templates_command", metavar="{list,scaffold,validate,delete,catalog}"
    )
    templates_parser.set_defaults(func=_cmd_templates, templates_parser=templates_parser)

    list_parser = templates_subparsers.add_parser(
        "list", help="list every template.json under --dir"
    )
    _add_dir_arg(list_parser)
    list_parser.set_defaults(func=_cmd_templates_list)

    scaffold_parser = templates_subparsers.add_parser(
        "scaffold", help="write a new template's skeleton"
    )
    _add_dir_arg(scaffold_parser)
    scaffold_parser.add_argument(
        "--slug", required=True, help="template slug (lowercase_with_underscores)"
    )
    scaffold_parser.add_argument(
        "--kind",
        required=True,
        choices=[k.value for k in TemplateKind],
        help="what the template registers",
    )
    scaffold_parser.add_argument("--name", required=True, help="human-readable display name")
    scaffold_parser.add_argument(
        "--description", default="", help="one-paragraph domain description"
    )
    scaffold_parser.set_defaults(func=_cmd_templates_scaffold)

    validate_parser = templates_subparsers.add_parser(
        "validate", help="run the six-step validation loop over a template"
    )
    _add_dir_arg(validate_parser)
    validate_parser.add_argument("--slug", required=True, help="template slug to validate")
    validate_parser.set_defaults(func=_cmd_templates_validate)

    delete_parser = templates_subparsers.add_parser("delete", help="delete a template")
    _add_dir_arg(delete_parser)
    delete_parser.add_argument("--slug", required=True, help="template slug to delete")
    delete_parser.set_defaults(func=_cmd_templates_delete)

    catalog_parser = templates_subparsers.add_parser(
        "catalog", help="built-in + template archetypes/playbooks/processes as one JSON catalog"
    )
    _add_dir_arg(catalog_parser)
    catalog_parser.set_defaults(func=_cmd_templates_catalog)


__all__ = ["add_templates_parser"]
