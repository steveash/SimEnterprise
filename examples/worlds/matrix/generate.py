"""Generate the benchmark world matrix: one config per cell (simx-cc).

Full factorial over the priority families' axes (docs/../ diversity matrix):

    family      coding | data_eng | analysis
    clean       clean  | messy
    structure   docs   | data          (data: the family's scenarios at scale 0.05)
    size        small  | large         (large: more departments, 2..3 squads, enterprise band)
    churn       low    | high          (high: facts change often, 4 versions, turnover,
                                        transfers, a six-month window, busier chat;
                                        messy x high adds retractions)

Large worlds also render decks, e-mail threads and meeting invites ([formats]);
every world has team-chat threads ([chat]); coding and data-engineering worlds
carry repository artifacts for their engineering projects ([code]).

= 48 cells, plus special department mixes (``SPECIALS``). Every config is synth
(deterministic, fact-bearing), names entities by id in fact lines, and has its
own seed derived from the cell name. Re-run to regenerate; the output is
byte-stable:

    uv run python examples/worlds/matrix/generate.py
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

FAMILIES = {
    "coding": {
        "vertical": "software",
        "small": ["engineering"],
        "large": ["engineering", "security", "support", "data_engineering"],
        "data": ["support_ops", "product_growth"],
        "what": "a software company whose primary team ships product",
        "code": True,
    },
    "data_eng": {
        "vertical": "data platform",
        "small": ["data_engineering", "engineering"],
        "large": ["data_engineering", "data_analytics", "engineering", "security"],
        "data": ["pipeline_ops", "product_growth", "support_ops"],
        "what": "a data-platform company whose primary team runs the pipelines",
        "code": True,
    },
    "analysis": {
        "vertical": "analytics",
        "small": ["data_analytics", "marketing"],
        "large": ["data_analytics", "marketing", "sales", "finance", "data_engineering"],
        "data": [
            "product_growth",
            "sales_pipeline",
            "support_ops",
            "subscription_finance",
            "marketing_attribution",
        ],
        "what": "a company whose primary team answers the business's questions",
    },
}

SPECIALS = {
    "special-professional-services": {
        "vertical": "professional services",
        "departments": ["legal", "finance", "people_ops"],
        "data": [],
        "what": "a document-only advisory firm (legal, finance, people)",
        "privacy": 0.3,
    },
    "special-retail-chain": {
        "vertical": "retail",
        "departments": ["retail", "finance", "marketing", "support"],
        "data": ["sales_pipeline"],
        "what": "a retail chain with merchandising, finance, marketing and support",
    },
    "special-security-led": {
        "vertical": "security",
        "departments": ["security", "engineering", "legal"],
        "data": [],
        "what": "a security vendor where security leads and legal reviews",
    },
    "special-sales-led": {
        "vertical": "sales",
        "departments": ["sales", "marketing", "support", "finance"],
        "data": ["sales_pipeline", "marketing_attribution"],
        "what": "a sales-led company with go-to-market functions only",
        "privacy": 0.1,
    },
    "special-support-led": {
        "vertical": "customer service",
        "departments": ["support", "engineering", "data_analytics"],
        "data": ["support_ops"],
        "what": "a support-heavy company whose tickets drive engineering",
        "privacy": 0.1,
    },
    "special-all-departments": {
        "vertical": "software",
        "departments": [
            "engineering",
            "data_engineering",
            "data_analytics",
            "security",
            "finance",
            "people_ops",
            "support",
            "sales",
            "marketing",
            "legal",
            "retail",
        ],
        "data": ["sales_pipeline", "support_ops"],
        "what": "every department kind at once, one squad each",
        "privacy": 0.15,
    },
}

_FIRST = (
    "Amber",
    "Basalt",
    "Cobalt",
    "Dune",
    "Ember",
    "Fjord",
    "Garnet",
    "Harbor",
    "Indigo",
    "Juniper",
    "Kestrel",
    "Lumen",
    "Marlin",
    "Nimbus",
    "Onyx",
    "Pike",
    "Quarry",
    "Rowan",
    "Saffron",
    "Tundra",
    "Umber",
    "Vale",
    "Willow",
    "Yarrow",
    "Zephyr",
    "Alder",
    "Birch",
    "Cedar",
    "Delta",
    "Elm",
)
_SECOND = ("Works", "Systems", "Labs", "Analytics", "Group", "Digital", "Partners", "Data")


def _seed(name: str) -> int:
    return int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 100_000


def _company(name: str, used: set[str]) -> str:
    """A fictional company name for ``name``: hashed, then the next free one."""
    h = int(hashlib.sha256(f"co:{name}".encode()).hexdigest(), 16)
    total = len(_FIRST) * len(_SECOND)
    for step in range(total):
        i = (h + step) % total
        candidate = f"{_FIRST[i % len(_FIRST)]} {_SECOND[i // len(_FIRST)]}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError("company name bank exhausted")


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def _config(
    name: str, cell: dict[str, str], spec: dict[str, object], used: set[str], tier: str = "S"
) -> str:
    messy = cell["clean"] == "messy"
    high = cell["churn"] == "high"
    large = cell["size"] == "large"
    data = cell["structure"] == "data"
    departments = spec["departments"] if "departments" in spec else spec[cell["size"]]  # type: ignore[index]
    lines = [
        f"# Matrix world {name}: {spec['what']}.",
        "# Cell: " + ", ".join(f"{k}={v}" for k, v in cell.items()) + ".",
        "# Generated by examples/worlds/matrix/generate.py; edit the generator, not this file.",
        f"seed = {_seed(name)}",
        'worlds_root = "worlds"',
        "",
        "[company]",
        f'name = "{_company(name, used)}"',
        f"vertical = {json.dumps(spec['vertical'])}",
        f'size = "{"enterprise" if large else "small"}"',
        "",
        "[simulation]",
        "period_start = 2026-01-05",
        f"period_end = {'2026-06-26' if high else '2026-03-27'}",
        "",
        "[model]",
        *(
            ['backend = "claude_cli"', 'name = "us.anthropic.claude-haiku-5-5"']
            if tier == "L"
            else ['backend = "anthropic_api"', 'name = "claude-haiku-4-5"']
        ),
        'offline_backend = "synth"',
        "",
    ]
    if tier == "L":
        lines += ["[scale]", "max_concurrency = 16", ""]
    for dept in departments:  # type: ignore[attr-defined]
        lines += ["[[departments]]", f'archetype = "{dept}"']
    lines += [
        "",
        "[facts]",
        "per_project = 8",
        f"change_rate = {0.6 if high else 0.15}",
        f"max_versions = {4 if high else 2}",
        f"stale_rate = {0.3 if messy else 0.0}",
        f"conflict_rate = {0.1 if messy else 0.0}",
        f"hedge_rate = {0.15 if messy else 0.0}",
        f"cross_team = {0.8 if large else 0.3}",
        "entity_ids = true",
    ]
    if messy and high:
        lines.append("retraction_rate = 0.3")
    lines += ["", "[chat]", f"rate = {0.4 if high else 0.1}"]
    if spec.get("privacy"):
        lines += ["", "[privacy]", f"sensitive_rate = {spec['privacy']}"]
    if spec.get("code"):
        lines += ["", "[code]", "enabled = true"]
    if large:
        lines += ["", "[formats]", "decks = true", "email = true", "invites = true"]
    lines += ["", "[org]", "project_names = true"]
    if large or high:
        lines += [
            f'squads_per_team = "{"2..3" if large else "1..1"}"',
            f"turnover_rate = {0.2 if high else 0.0}",
            f"transfer_rate = {0.1 if high else 0.0}",
        ]
    if messy:
        lines += [
            "",
            "[messiness]",
            "typo_rate = 0.02",
            "alias_rate = 0.3",
            "date_format_mix = 0.4",
            "duplicate_rate = 0.08",
            "drop_header_rate = 0.1",
            "filler_rate = 0.15",
        ]
    scenarios = list(spec["data"]) if data else []  # type: ignore[call-overload]
    if scenarios:
        lines += [
            "",
            "[data]",
            f"scenarios = {_toml_list(scenarios)}",
            f"scale_factor = {_full_scale(scenarios) if tier == 'L' else 0.05}",
            f"iterations = {2 if tier == 'L' else 1}",
            f"rows_per_partition = {2_000_000 if tier == 'L' else 50000}",
        ]
        if messy:
            lines += [
                "",
                "[data.messiness]",
                "null_rate = 0.03",
                "duplicate_rate = 0.02",
                "label_drift_rate = 0.1",
                "outlier_rate = 0.01",
                "schema_drift = true",
            ]
    return "\n".join(lines) + "\n"


def cells() -> list[tuple[str, dict[str, str], dict[str, object]]]:
    out: list[tuple[str, dict[str, str], dict[str, object]]] = []
    axes = (("clean", "messy"), ("docs", "data"), ("small", "large"), ("low", "high"))
    for family, spec in FAMILIES.items():
        for clean, structure, size, churn in itertools.product(*axes):
            cell = {
                "family": family,
                "clean": clean,
                "structure": structure,
                "size": size,
                "churn": churn,
            }
            name = f"{family.replace('_', '')}-{clean}-{structure}-{size}-{churn}"
            out.append((name, cell, spec))
    for name, spec in SPECIALS.items():
        cell = {
            "family": "special",
            "clean": "messy",
            "structure": "data" if spec["data"] else "docs",
            "size": "large",
            "churn": "high",
        }
        out.append((name, cell, spec))
    return out


def _full_scale(scenarios: list[str]) -> float:
    """The L tier's data scale: about half a million rows or more per world."""
    return 1.2 if len(scenarios) >= 5 else 2.0


def main() -> None:
    """Write the S tier (synth, small data) here and the L tier into ``../matrix-l/``.

    The L tier renders the same companies and seeds with live prose (Haiku 5.5
    through the claude_cli backend) and full-scale data, so every S world has an
    L twin; rebuilding an L world from its export's replay file makes no calls.
    """
    for tier, out, suffix in (("S", HERE, ""), ("L", HERE.parent / "matrix-l", "-l")):
        out.mkdir(exist_ok=True)
        manifest = []
        used: set[str] = set()
        for name, cell, spec in cells():
            text = _config(name, cell, spec, used, tier)
            if tier == "L":
                text = text.replace(
                    f"# Matrix world {name}:", f"# Matrix world {name}{suffix} (L tier):", 1
                )
            (out / f"{name}{suffix}.toml").write_text(text, encoding="utf-8")
            manifest.append(
                {"name": f"{name}{suffix}", "config": f"{name}{suffix}.toml", "tier": tier, **cell}
            )
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        print(f"{tier}: {len(manifest)} configs")


if __name__ == "__main__":
    main()
