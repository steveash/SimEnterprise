"""``enterprise-sim evals {generate,list,add,remove,score,sample,run}`` (EXPLORER_EVALS.md §2).

Thin JSON-speaking wrappers over :mod:`enterprise_sim.benchmark` and this
package, aware of the per-run ``evals/`` layout
(:mod:`enterprise_sim.evals.store`). Every subcommand prints one JSON document
to stdout (``evals run`` prints one JSON Lines event per question, per
``docs/EXPLORER.md`` §3's streaming-command contract) and a one-line human
summary to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.benchmark.score import Predictions, score
from enterprise_sim.evals.proposals import validate_proposals
from enterprise_sim.evals.runner import RUNNERS, RunnerUnavailable, report_to_dict, run_eval
from enterprise_sim.evals.sample import sample as sample_ids
from enterprise_sim.evals.store import (
    append_questions,
    questions_path,
    read_questions,
    remove_questions,
)


def _counts_by(pairs: list[QAPair], attr: str) -> dict[str, int]:
    return dict(sorted(Counter(getattr(p, attr) for p in pairs).items()))


def _critic_client(args: argparse.Namespace) -> Any:
    """The LLM client for ``--critic llm`` (cached in the world); ``None`` = structural-only."""
    if getattr(args, "critic", "structural") != "llm":
        return None
    from enterprise_sim.core.llm import LLMConfig, build_client
    from enterprise_sim.worlds.file import cache_dir

    return build_client(
        LLMConfig(
            backend=args.critic_backend,
            model=args.critic_model,
            cache_dir=str(cache_dir(args.world)),
        )
    )


def _critique(args: argparse.Namespace, pairs: list[QAPair]) -> list[QAPair]:
    from enterprise_sim.benchmark.generate import load_world_kg
    from enterprise_sim.evals.critic import critique_pairs

    return critique_pairs(pairs, load_world_kg(args.world), client=_critic_client(args))


def _confidence_summary(pairs: list[QAPair]) -> dict[str, int]:
    bands = {"verified": 0, "high": 0, "medium": 0, "low": 0, "unreviewed": 0}
    for p in pairs:
        if p.verified:
            bands["verified"] += 1
        elif p.confidence is None:
            bands["unreviewed"] += 1
        elif p.confidence >= 0.75:
            bands["high"] += 1
        elif p.confidence >= 0.4:
            bands["medium"] += 1
        else:
            bands["low"] += 1
    return bands


def _cmd_evals_generate(args: argparse.Namespace) -> int:
    """Derive the question set from the gold KG, critique it, and write it.

    ``--force`` regenerates over an existing set but never loses human work:
    proposed questions are kept, and a human-verified pair wins over the
    freshly generated pair with the same id.
    """
    from enterprise_sim.benchmark.generate import generate

    path = questions_path(args.world)
    if path.exists() and not args.force:
        payload = {"ok": False, "error": f"{path} already exists (pass --force to overwrite)"}
        print(json.dumps(payload, sort_keys=True))
        print(f"enterprise-sim evals generate: {payload['error']}", file=sys.stderr)
        return 2

    existing = list(read_questions(args.world)) if path.exists() else []
    verified = {p.id: p for p in existing if p.verified}
    generated = [verified.get(p.id, p) for p in generate(str(args.world))]
    generated_ids = {p.id for p in generated}
    kept = [
        p for p in existing if p.id not in generated_ids and (p.source != "generated" or p.verified)
    ]
    pairs = _critique(args, [*generated, *kept])

    from enterprise_sim.evals.store import write_questions

    write_questions(args.world, Benchmark.of(pairs))
    payload = {
        "ok": True,
        "count": len(pairs),
        "by_reasoning_type": _counts_by(pairs, "reasoning_type"),
        "confidence": _confidence_summary(pairs),
        "kept": len(kept),
        "path": str(path),
    }
    print(json.dumps(payload, sort_keys=True))
    print(f"enterprise-sim evals generate: {len(pairs)} question(s) -> {path}", file=sys.stderr)
    return 0


def _cmd_evals_critique(args: argparse.Namespace) -> int:
    """Re-review the existing question set in place (human-verified pairs are untouched)."""
    pairs = list(read_questions(args.world))
    if not pairs:
        print(json.dumps({"ok": False, "error": "no question set — run `evals generate` first"}))
        return 2
    reviewed = _critique(args, pairs)
    from enterprise_sim.evals.store import write_questions

    write_questions(args.world, Benchmark.of(reviewed))
    payload = {"ok": True, "count": len(reviewed), "confidence": _confidence_summary(reviewed)}
    print(json.dumps(payload, sort_keys=True))
    print(f"enterprise-sim evals critique: {payload['confidence']}", file=sys.stderr)
    return 0


def _cmd_evals_verify(args: argparse.Namespace) -> int:
    """A human confirms (or corrects) one question's expected answer.

    The pair keeps its id (so past executions still line up with it), gains
    ``verified``, and its confidence becomes 1.0. With ``--expected-ids`` the
    answer is replaced; every id must be a node in the world's KG.
    """
    import dataclasses

    from enterprise_sim.evals.proposals import load_node_ids
    from enterprise_sim.evals.store import write_questions

    pairs = list(read_questions(args.world))
    index = next((i for i, p in enumerate(pairs) if p.id == args.id), None)
    if index is None:
        print(json.dumps({"ok": False, "error": f"no such question: {args.id}"}))
        return 2
    pair = pairs[index]
    note = "verified by a human"
    updates: dict[str, Any] = {}
    if args.expected_ids is not None:
        ids = tuple(dict.fromkeys(args.expected_ids))
        unknown = sorted(set(ids) - load_node_ids(args.world))
        if unknown:
            print(json.dumps({"ok": False, "error": f"not nodes in this world: {unknown[:5]}"}))
            return 2
        if set(ids) != set(pair.expected_ids):
            note = f"corrected by a human (was {len(pair.expected_ids)} item(s))"
        updates["expected_ids"] = tuple(sorted(ids))
        if pair.reasoning_type == "aggregation":
            updates["expected_label"] = str(len(ids))
    if args.label is not None:
        updates["expected_label"] = args.label or None
    pairs[index] = dataclasses.replace(
        pair, **updates, verified=True, confidence=1.0, critic="human", critique=note
    )
    write_questions(args.world, Benchmark.of(pairs))
    print(json.dumps({"ok": True, "question": pairs[index].to_dict()}, sort_keys=True))
    print(f"enterprise-sim evals verify: {args.id}: {note}", file=sys.stderr)
    return 0


def _cmd_evals_list(args: argparse.Namespace) -> int:
    benchmark = read_questions(args.world)
    pairs = list(benchmark)
    payload = {
        "questions": [p.to_dict() for p in pairs],
        "counts": {
            "reasoning_type": _counts_by(pairs, "reasoning_type"),
            "qtype": _counts_by(pairs, "qtype"),
            "difficulty": _counts_by(pairs, "difficulty"),
            "source": _counts_by(pairs, "source"),
        },
    }
    print(json.dumps(payload, sort_keys=True))
    print(f"enterprise-sim evals list: {len(pairs)} question(s) in {args.world}", file=sys.stderr)
    return 0


def _load_raw_proposals(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("proposals", [data])
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of proposals (or {{'proposals': [...]}})")
    return [dict(item) for item in data]


def _cmd_evals_add(args: argparse.Namespace) -> int:
    try:
        raw = _load_raw_proposals(args.file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        print(f"enterprise-sim evals add: {exc}", file=sys.stderr)
        return 2

    accepted, rejected = validate_proposals(args.world, raw)
    if accepted:
        append_questions(args.world, accepted)
    payload = {
        "added": [p.to_dict() for p in accepted],
        "rejected": [r.to_dict() for r in rejected],
    }
    print(json.dumps(payload, sort_keys=True))
    print(
        f"enterprise-sim evals add: {len(accepted)} added, {len(rejected)} rejected",
        file=sys.stderr,
    )
    return 0


def _cmd_evals_remove(args: argparse.Namespace) -> int:
    _, removed = remove_questions(args.world, args.ids)
    not_removed = [i for i in args.ids if i not in removed]
    payload = {"removed": removed, "not_removed": not_removed}
    print(json.dumps(payload, sort_keys=True))
    print(
        f"enterprise-sim evals remove: {len(removed)} removed, {len(not_removed)} skipped "
        "(only proposed questions may be removed)",
        file=sys.stderr,
    )
    return 0


def _cmd_evals_score(args: argparse.Namespace) -> int:
    benchmark = read_questions(args.world)
    if args.ids:
        wanted = set(args.ids)
        benchmark = Benchmark.of(p for p in benchmark if p.id in wanted)
    predictions = Predictions.read_jsonl(args.pred)
    report = score(benchmark, predictions)
    print(json.dumps(report_to_dict(report), sort_keys=True))
    print(
        f"enterprise-sim evals score: {report.overall.count} question(s), "
        f"macro-F1={report.overall.macro_f1:.3f}",
        file=sys.stderr,
    )
    return 0


def _cmd_evals_sample(args: argparse.Namespace) -> int:
    benchmark = read_questions(args.world)
    ids = sample_ids(
        list(benchmark), fraction=args.fraction, seed=args.seed, stratify=args.stratify
    )
    print(json.dumps({"ids": ids, "count": len(ids)}, sort_keys=True))
    print(
        f"enterprise-sim evals sample: {len(ids)}/{len(benchmark)} question(s) "
        f"(fraction={args.fraction}, seed={args.seed}, stratify={args.stratify})",
        file=sys.stderr,
    )
    return 0


def _select_pairs(args: argparse.Namespace, benchmark: Benchmark) -> list[QAPair]:
    if args.ids:
        wanted = set(args.ids)
        return [p for p in benchmark if p.id in wanted]
    if args.fraction is not None:
        ids = set(
            sample_ids(
                list(benchmark), fraction=args.fraction, seed=args.seed, stratify=args.stratify
            )
        )
        return [p for p in benchmark if p.id in ids]
    return list(benchmark)


def _cmd_evals_run(args: argparse.Namespace) -> int:
    benchmark = read_questions(args.world)
    pairs = _select_pairs(args, benchmark)

    try:
        stream = run_eval(
            args.world, pairs, runner=args.runner, model=args.model, backend=args.backend
        )
        rows: dict[str, tuple[str, ...]] = {}
        report_payload: dict[str, Any] | None = None
        for event in stream:
            print(json.dumps(event, sort_keys=True), flush=True)
            if event["kind"] == "question":
                rows[event["id"]] = tuple(event["predicted_ids"])
            elif event["kind"] == "done":
                report_payload = event["report"]
    except RunnerUnavailable as exc:
        print(json.dumps({"kind": "error", "error": str(exc)}))
        print(f"enterprise-sim evals run: {exc}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    Predictions.from_mapping(rows).write_jsonl(args.out / "predictions.jsonl")
    (args.out / "results.json").write_text(
        json.dumps(report_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"enterprise-sim evals run --runner {args.runner}: {len(pairs)} question(s) -> {args.out}",
        file=sys.stderr,
    )
    return 0


def _cmd_evals(args: argparse.Namespace) -> int:
    args.evals_parser.print_help()
    return 0 if args.evals_command is None else 2


def _add_world_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--world", required=True, type=Path, metavar="DIR", help="the world directory"
    )


def _add_critic_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--critic",
        choices=["structural", "llm"],
        default="structural",
        help="structural (keyless graph checks) or llm (graph checks + a model's review)",
    )
    parser.add_argument(
        "--critic-backend",
        dest="critic_backend",
        default="claude_cli",
        choices=["anthropic_api", "bedrock", "claude_cli"],
        help="[llm critic] backend (default: claude_cli, your Claude login)",
    )
    parser.add_argument(
        "--critic-model",
        dest="critic_model",
        default="claude-sonnet-4-6",
        metavar="MODEL",
        help="[llm critic] model",
    )


def add_evals_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire ``enterprise-sim evals {generate,list,add,remove,score,sample,run}``."""
    evals_parser = subparsers.add_parser(
        "evals",
        help="per-run KG-QA eval question set: generate/list/add/remove/score/sample/run",
        description=(
            "Manage a run's eval question set (docs/EXPLORER_EVALS.md): the "
            "per-run <run>/evals/questions.jsonl, deterministically derived "
            "from the gold KG plus any AI-proposed questions added on top."
        ),
    )
    evals_subparsers = evals_parser.add_subparsers(
        dest="evals_command", metavar="{generate,critique,verify,list,add,remove,score,sample,run}"
    )
    evals_parser.set_defaults(func=_cmd_evals, evals_parser=evals_parser)

    generate_parser = evals_subparsers.add_parser(
        "generate", help="derive and write <run>/evals/questions.jsonl from the run's gold KG"
    )
    _add_world_arg(generate_parser)
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate over an existing set (proposed + human-verified questions are kept)",
    )
    _add_critic_args(generate_parser)
    generate_parser.set_defaults(func=_cmd_evals_generate)

    critique_parser = evals_subparsers.add_parser(
        "critique", help="re-review the question set's expected answers (confidence + notes)"
    )
    _add_world_arg(critique_parser)
    _add_critic_args(critique_parser)
    critique_parser.set_defaults(func=_cmd_evals_critique)

    verify_parser = evals_subparsers.add_parser(
        "verify", help="mark one question's expected answer human-verified (optionally corrected)"
    )
    _add_world_arg(verify_parser)
    verify_parser.add_argument("--id", required=True, metavar="ID", help="the question id")
    verify_parser.add_argument(
        "--expected-ids",
        dest="expected_ids",
        nargs="*",
        default=None,
        metavar="NODE_ID",
        help="replace the expected answer with these node ids",
    )
    verify_parser.add_argument(
        "--label", default=None, metavar="TEXT", help="replace the human-readable answer label"
    )
    verify_parser.set_defaults(func=_cmd_evals_verify)

    list_parser = evals_subparsers.add_parser("list", help="list the run's eval questions")
    _add_world_arg(list_parser)
    list_parser.set_defaults(func=_cmd_evals_list)

    add_parser = evals_subparsers.add_parser(
        "add", help="validate and append AI-proposed questions from a JSON file"
    )
    _add_world_arg(add_parser)
    add_parser.add_argument(
        "--file",
        required=True,
        type=Path,
        metavar="PROPOSALS.json",
        help="a JSON list of proposals",
    )
    add_parser.set_defaults(func=_cmd_evals_add)

    remove_parser = evals_subparsers.add_parser(
        "remove", help="remove proposed questions by id (generated questions cannot be removed)"
    )
    _add_world_arg(remove_parser)
    remove_parser.add_argument(
        "--ids", required=True, nargs="+", metavar="ID", help="question ids to remove"
    )
    remove_parser.set_defaults(func=_cmd_evals_remove)

    score_parser = evals_subparsers.add_parser(
        "score", help="score a predictions JSONL against the run's question set"
    )
    _add_world_arg(score_parser)
    score_parser.add_argument(
        "--pred", required=True, type=Path, metavar="PRED.jsonl", help="predictions JSONL"
    )
    score_parser.add_argument(
        "--ids", nargs="+", default=None, metavar="ID", help="score only these question ids"
    )
    score_parser.set_defaults(func=_cmd_evals_score)

    sample_parser = evals_subparsers.add_parser(
        "sample", help="deterministically sample a fraction of the run's question ids"
    )
    _add_world_arg(sample_parser)
    sample_parser.add_argument(
        "--fraction", required=True, type=float, metavar="F", help="0 < F <= 1"
    )
    sample_parser.add_argument("--seed", required=True, type=int, metavar="S", help="PRNG seed")
    sample_parser.add_argument(
        "--stratify", action="store_true", help="sample independently per reasoning_type"
    )
    sample_parser.set_defaults(func=_cmd_evals_sample)

    run_parser = evals_subparsers.add_parser(
        "run", help="answer a selection of the run's questions with a runner, streaming progress"
    )
    _add_world_arg(run_parser)
    run_parser.add_argument(
        "--runner", required=True, choices=list(RUNNERS), help="which runner to use"
    )
    run_parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="output directory"
    )
    run_parser.add_argument(
        "--ids", nargs="+", default=None, metavar="ID", help="answer only these ids"
    )
    run_parser.add_argument(
        "--fraction", type=float, default=None, metavar="F", help="sample a fraction"
    )
    run_parser.add_argument("--seed", type=int, default=0, metavar="S", help="sample PRNG seed")
    run_parser.add_argument(
        "--stratify", action="store_true", help="stratify the --fraction sample"
    )
    run_parser.add_argument(
        "--model", default=None, metavar="MODEL", help="[graph] the Claude model"
    )
    run_parser.add_argument(
        "--backend",
        default="anthropic_api",
        choices=["anthropic_api", "bedrock", "claude_cli"],
        help="[rag] LLM backend for the answer step (default: anthropic_api)",
    )
    run_parser.set_defaults(func=_cmd_evals_run)


__all__ = ["add_evals_parser"]
