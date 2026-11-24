"""The fact-attribute catalog: what a project's facts are about, per department kind.

An :class:`Attribute` names one fact a project carries (its launch date, its
datastore, its pipeline schedule, the metric definition it reports against), how
a first value is drawn, how a value changes, what a wrong-but-plausible value
looks like, and how a document states it. Values are plain text; sentences are
written with :meth:`Attribute.state` so the prose a live model or the ``synth``
backend writes always carries the value verbatim.

No value or template holds a Title-Case word pair (``Google Cloud``), so the
grounding detector never mistakes a fact value for an out-of-roster person.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

__all__ = ["Attribute", "Context", "attributes_for", "kind_of", "value_token"]


@dataclass(frozen=True, slots=True)
class Context:
    """What value generators may draw on for one project."""

    start: date
    end: date
    people: tuple[str, ...]  # display names of the project's department


Draw = Callable[[random.Random, Context], str]
Mutate = Callable[[random.Random, str, Context], str]


@dataclass(frozen=True, slots=True)
class Attribute:
    """One kind of fact a project carries."""

    name: str
    label: str  # a lower-case noun phrase: "launch date"
    draw: Draw
    mutate: Mutate
    current: tuple[str, ...]  # templates with {subject} and {value}
    update: str  # template with {subject}, {old} and {value}
    question: str  # "What is the launch date of {subject}?"
    volatile: float = 1.0  # multiplier on the change rate
    hedges: tuple[str, ...] = field(default=("I believe", "Last I heard,", "Tentatively,"))
    #: What kind of knowledge the fact is, for the knowledge layer: ``decision`` (a choice
    #: made: an owner, a vendor, a datastore), ``policy`` (a standing rule or cadence),
    #: ``target`` (a planned number or date) or ``status``. Not used in rendering.
    kind: str = "decision"

    def state(self, template: str, *, subject: str, value: str, old: str = "") -> str:
        return template.format(subject=subject, value=value, old=old)


# -- value helpers ------------------------------------------------------------ #


def _choice(values: Sequence[str]) -> tuple[Draw, Mutate]:
    def draw(rng: random.Random, ctx: Context) -> str:
        return rng.choice(list(values))

    def mutate(rng: random.Random, old: str, ctx: Context) -> str:
        options = [v for v in values if v != old]
        return rng.choice(options) if options else old

    return draw, mutate


#: Launch dates land 6 to 22 weeks after the ledger starts. Fixed (not tied to
#: the window end) so extending a world's window never redraws a past fact.
_DATE_SPAN_DAYS = (42, 154)


def _date_draw(rng: random.Random, ctx: Context) -> str:
    offset = rng.randint(*_DATE_SPAN_DAYS)
    return (ctx.start + timedelta(days=offset)).isoformat()


def _date_mutate(rng: random.Random, old: str, ctx: Context) -> str:
    slip = rng.choice((7, 14, 14, 21, 28, -7))
    return (date.fromisoformat(old) + timedelta(days=slip)).isoformat()


def _money(value: int) -> str:
    """``$250k`` / ``$1.2M``: no grouping separator, so the value is one token."""
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"${text}M"
    return f"${value // 1000}k"


def _money_value(text: str) -> int:
    body = text.lstrip("$")
    if body.endswith("M"):
        return round(float(body[:-1]) * 1_000_000)
    return int(body.rstrip("k")) * 1000


def _money_draw(rng: random.Random, ctx: Context) -> str:
    return _money(rng.choice((120, 180, 250, 400, 650, 900, 1200, 2000)) * 1000)


def _money_mutate(rng: random.Random, old: str, ctx: Context) -> str:
    base = _money_value(old)
    factor = rng.choice((0.8, 0.9, 1.15, 1.25, 1.5))
    return _money(max(10_000, round(base * factor / 10_000) * 10_000))


def _person_draw(rng: random.Random, ctx: Context) -> str:
    return rng.choice(list(ctx.people)) if ctx.people else "the project lead"


def _person_mutate(rng: random.Random, old: str, ctx: Context) -> str:
    options = [p for p in ctx.people if p != old]
    return rng.choice(options) if options else old


def _number(values: Sequence[int], unit: str) -> tuple[Draw, Mutate]:
    return _choice([f"{v:,}{unit}" for v in values])


# -- generic attributes (every project) -------------------------------------- #

_status_draw, _status_mutate = _choice(("on track", "at risk", "blocked", "behind schedule"))
_priority_draw, _priority_mutate = _choice(("P0", "P1", "P2", "P3"))
_cadence_draw, _cadence_mutate = _choice(("weekly", "every two weeks", "monthly"))

GENERIC: tuple[Attribute, ...] = (
    Attribute(
        name="target_date",
        kind="target",
        label="launch date",
        draw=_date_draw,
        mutate=_date_mutate,
        current=(
            "{subject} is targeting {value} for launch.",
            "The launch date for {subject} is {value}.",
        ),
        update="The launch date for {subject} moved from {old} to {value}.",
        question="What is the launch date of {subject}?",
        volatile=1.4,
    ),
    Attribute(
        name="budget",
        kind="target",
        label="approved budget",
        draw=_money_draw,
        mutate=_money_mutate,
        current=(
            "{subject} has an approved budget of {value}.",
            "The approved budget for {subject} is {value}.",
        ),
        update="The budget for {subject} was revised from {old} to {value}.",
        question="What is the approved budget of {subject}?",
        volatile=0.7,
    ),
    Attribute(
        name="owner",
        kind="decision",
        label="day-to-day owner",
        draw=_person_draw,
        mutate=_person_mutate,
        current=(
            "{value} is the day-to-day owner of {subject}.",
            "{value} owns {subject} day to day.",
        ),
        update="Day-to-day ownership of {subject} passed from {old} to {value}.",
        question="Who is the day-to-day owner of {subject}?",
        volatile=0.8,
    ),
    Attribute(
        name="status",
        kind="status",
        label="delivery status",
        draw=_status_draw,
        mutate=_status_mutate,
        current=("{subject} is currently {value}.", "Delivery status for {subject}: {value}."),
        update="{subject} went from {old} to {value}.",
        question="What is the delivery status of {subject}?",
        volatile=1.6,
    ),
    Attribute(
        name="priority",
        kind="status",
        label="roadmap priority",
        draw=_priority_draw,
        mutate=_priority_mutate,
        current=("{subject} is ranked {value} on the roadmap.",),
        update="{subject} was re-ranked from {old} to {value}.",
        question="What roadmap priority does {subject} have?",
        volatile=0.6,
    ),
    Attribute(
        name="review_cadence",
        kind="policy",
        label="steering review cadence",
        draw=_cadence_draw,
        mutate=_cadence_mutate,
        current=("Steering reviews for {subject} happen {value}.",),
        update="Steering reviews for {subject} changed from {old} to {value}.",
        question="How often are steering reviews held for {subject}?",
        volatile=0.4,
    ),
)

# -- per-department attributes ------------------------------------------------ #

_store = _choice(("PostgreSQL", "MySQL", "DynamoDB", "Cassandra", "CockroachDB", "MongoDB"))
_latency = _number((150, 200, 250, 300, 400, 500), " ms")
_api = _choice(("v1.2", "v2.0", "v2.3", "v3.1", "v4.0"))
_deploy = _choice(("daily", "twice a week", "weekly", "once per sprint"))
_flag = _choice(("checkout_v2", "new_onboarding", "fast_search", "bulk_export", "sso_login"))

ENGINEERING: tuple[Attribute, ...] = (
    Attribute(
        name="datastore",
        kind="decision",
        label="primary datastore",
        draw=_store[0],
        mutate=_store[1],
        current=("{subject} stores its records in {value}.",),
        update="{subject} is moving its storage from {old} to {value}.",
        question="Which datastore does {subject} use?",
        volatile=0.6,
    ),
    Attribute(
        name="latency_target",
        kind="policy",
        label="p99 latency target",
        draw=_latency[0],
        mutate=_latency[1],
        current=("The p99 latency budget for {subject} is {value}.",),
        update="The p99 latency budget for {subject} changed from {old} to {value}.",
        question="What is the p99 latency target of {subject}?",
    ),
    Attribute(
        name="api_version",
        kind="decision",
        label="public API version",
        draw=_api[0],
        mutate=_api[1],
        current=("{subject} serves clients through API {value}.",),
        update="{subject} moved clients from API {old} to API {value}.",
        question="Which API version does {subject} serve?",
        volatile=0.7,
    ),
    Attribute(
        name="deploy_cadence",
        kind="policy",
        label="deploy cadence",
        draw=_deploy[0],
        mutate=_deploy[1],
        current=("{subject} deploys {value}.",),
        update="{subject} now deploys {value} instead of {old}.",
        question="How often does {subject} deploy?",
    ),
    Attribute(
        name="feature_flag",
        kind="decision",
        label="launch feature flag",
        draw=_flag[0],
        mutate=_flag[1],
        current=("{subject} ships behind the {value} feature flag.",),
        update="{subject} renamed its feature flag from {old} to {value}.",
        question="Which feature flag gates {subject}?",
        volatile=0.4,
    ),
)

_supplier = _choice(("Northcrate", "Veldmark", "Hollisworth", "Quayline", "Tamberlin", "Ostrava"))
_reorder = _number((200, 350, 500, 800, 1200, 2000), " units")
_margin = _choice(("32%", "35%", "38%", "41%", "44%"))
_region = _choice(("the northern region", "the coastal region", "metro stores", "online only"))

RETAIL: tuple[Attribute, ...] = (
    Attribute(
        name="supplier",
        kind="decision",
        label="primary supplier",
        draw=_supplier[0],
        mutate=_supplier[1],
        current=("{value} is the primary supplier for {subject}.",),
        update="{subject} switched its primary supplier from {old} to {value}.",
        question="Who is the primary supplier for {subject}?",
    ),
    Attribute(
        name="reorder_point",
        kind="policy",
        label="reorder point",
        draw=_reorder[0],
        mutate=_reorder[1],
        current=("The reorder point for {subject} is {value}.",),
        update="The reorder point for {subject} was changed from {old} to {value}.",
        question="What is the reorder point for {subject}?",
    ),
    Attribute(
        name="margin_target",
        kind="target",
        label="gross margin target",
        draw=_margin[0],
        mutate=_margin[1],
        current=("The gross margin target for {subject} is {value}.",),
        update="The gross margin target for {subject} moved from {old} to {value}.",
        question="What is the gross margin target of {subject}?",
        volatile=0.6,
    ),
    Attribute(
        name="launch_region",
        kind="decision",
        label="launch region",
        draw=_region[0],
        mutate=_region[1],
        current=("{subject} launches first in {value}.",),
        update="{subject} now launches first in {value} rather than {old}.",
        question="Where does {subject} launch first?",
        volatile=0.6,
    ),
)

_schedule = _choice(
    ("hourly", "every 15 minutes", "daily at 02:00 UTC", "daily at 06:00 UTC", "twice daily")
)
_source = _choice(
    (
        "the orders database change stream",
        "the billing service export",
        "the clickstream topic",
        "the nightly crm extract",
        "partner sftp drops",
    )
)
_table = _choice(
    (
        "analytics.fct_orders",
        "analytics.dim_customers",
        "raw.events_v2",
        "mart.revenue_daily",
        "mart.subscriptions",
        "staging.payments",
    )
)
_sla = _choice(("by 06:00 UTC", "by 09:00 UTC", "within two hours", "within 30 minutes"))
_schema = _choice(("v3.0", "v3.2", "v4.0", "v4.1", "v5.0"))
_retention = _choice(("90 days", "13 months", "two years", "seven years"))

DATA_ENGINEERING: tuple[Attribute, ...] = (
    Attribute(
        name="pipeline_schedule",
        kind="policy",
        label="pipeline schedule",
        draw=_schedule[0],
        mutate=_schedule[1],
        current=("The {subject} pipeline runs {value}.", "{subject} is scheduled {value}."),
        update="The {subject} pipeline schedule changed from {old} to {value}.",
        question="How often does the {subject} pipeline run?",
        volatile=1.2,
    ),
    Attribute(
        name="source_system",
        kind="decision",
        label="source system",
        draw=_source[0],
        mutate=_source[1],
        current=("{subject} ingests from {value}.",),
        update="{subject} now ingests from {value} instead of {old}.",
        question="Which source does {subject} ingest from?",
        volatile=0.5,
    ),
    Attribute(
        name="target_table",
        kind="decision",
        label="output table",
        draw=_table[0],
        mutate=_table[1],
        current=("{subject} lands its output in {value}.",),
        update="{subject} moved its output table from {old} to {value}.",
        question="Which table does {subject} write its output to?",
        volatile=0.6,
    ),
    Attribute(
        name="freshness_sla",
        kind="policy",
        label="freshness SLA",
        draw=_sla[0],
        mutate=_sla[1],
        current=("The freshness SLA for {subject} is {value}.",),
        update="The freshness SLA for {subject} changed from {old} to {value}.",
        question="What is the freshness SLA of {subject}?",
    ),
    Attribute(
        name="schema_version",
        kind="decision",
        label="schema version",
        draw=_schema[0],
        mutate=_schema[1],
        current=("{subject} emits records on schema {value}.",),
        update="{subject} bumped its schema from {old} to {value}.",
        question="Which schema version does {subject} emit?",
        volatile=1.3,
    ),
    Attribute(
        name="retention",
        kind="policy",
        label="retention period",
        draw=_retention[0],
        mutate=_retention[1],
        current=("Data from {subject} is retained for {value}.",),
        update="Retention for {subject} changed from {old} to {value}.",
        question="How long is data from {subject} retained?",
        volatile=0.4,
    ),
)

_wau = _choice(
    (
        "accounts with at least one session in the trailing 7 days",
        "users with at least two sessions in the calendar week",
        "accounts with a billable action in the trailing 7 days",
        "users who opened the app on any day of the trailing 7 days",
    )
)
_tool = _choice(("Looker", "Tableau", "Superset", "Metabase", "Mode"))
_refresh = _choice(("hourly", "daily", "weekly"))
_kpi = _number((12_400, 15_800, 18_400, 21_000, 26_500), " weekly active accounts")
_attribution = _choice(("last-touch", "first-touch", "linear multi-touch", "time-decay"))

DATA_ANALYTICS: tuple[Attribute, ...] = (
    Attribute(
        name="metric_definition",
        kind="decision",
        label="weekly active users definition",
        draw=_wau[0],
        mutate=_wau[1],
        current=("{subject} defines weekly active users as {value}.",),
        update="{subject} changed its weekly active users definition from {old} to {value}.",
        question="How does {subject} define weekly active users?",
        volatile=0.9,
    ),
    Attribute(
        name="dashboard_tool",
        kind="decision",
        label="dashboard tool",
        draw=_tool[0],
        mutate=_tool[1],
        current=("The {subject} dashboards live in {value}.",),
        update="The {subject} dashboards moved from {old} to {value}.",
        question="Which tool hosts the {subject} dashboards?",
        volatile=0.5,
    ),
    Attribute(
        name="refresh_cadence",
        kind="policy",
        label="dashboard refresh cadence",
        draw=_refresh[0],
        mutate=_refresh[1],
        current=("{subject} dashboards refresh {value}.",),
        update="{subject} dashboards now refresh {value} instead of {old}.",
        question="How often do the {subject} dashboards refresh?",
    ),
    Attribute(
        name="north_star",
        kind="decision",
        label="latest north-star reading",
        draw=_kpi[0],
        mutate=_kpi[1],
        current=("The latest north-star reading for {subject} is {value}.",),
        update="The north-star reading for {subject} moved from {old} to {value}.",
        question="What is the latest north-star reading reported by {subject}?",
        volatile=1.6,
    ),
    Attribute(
        name="attribution_model",
        kind="decision",
        label="attribution model",
        draw=_attribution[0],
        mutate=_attribution[1],
        current=("{subject} reports conversions with {value} attribution.",),
        update="{subject} switched from {old} to {value} attribution.",
        question="Which attribution model does {subject} use?",
        volatile=0.5,
    ),
)

_control = _choice(("quarterly", "monthly", "annually"))
_vendor = _choice(("Vantor", "Kelpline", "Ostrich", "Brindle", "Corvane"))
_headcount = _number((2, 3, 4, 6, 8), " open roles")

OPERATIONS: tuple[Attribute, ...] = (
    Attribute(
        name="review_window",
        kind="policy",
        label="control review frequency",
        draw=_control[0],
        mutate=_control[1],
        current=("Controls for {subject} are reviewed {value}.",),
        update="Control reviews for {subject} changed from {old} to {value}.",
        question="How often are controls for {subject} reviewed?",
        volatile=0.5,
    ),
    Attribute(
        name="vendor",
        kind="decision",
        label="main vendor",
        draw=_vendor[0],
        mutate=_vendor[1],
        current=("{value} is the main vendor for {subject}.",),
        update="{subject} moved from {old} to {value} as its main vendor.",
        question="Who is the main vendor for {subject}?",
    ),
    Attribute(
        name="open_roles",
        kind="target",
        label="open headcount",
        draw=_headcount[0],
        mutate=_headcount[1],
        current=("{subject} has {value}.",),
        update="{subject} went from {old} to {value}.",
        question="How many open roles does {subject} have?",
        volatile=1.2,
    ),
)

#: Department archetype → its domain attributes (generic ones always apply).
_escalation = _choice(
    ("platform-infra", "data-platform", "payments-infra", "core-services", "edge-networking")
)
_window = _choice(
    (
        "weekdays 09:00-15:00 UTC",
        "weekdays 09:00-17:00 UTC",
        "Tue-Thu 10:00-16:00 UTC",
        "weekdays 13:00-18:00 UTC",
    )
)
_failover = _choice(("eu-west-1", "eu-central-1", "us-east-2", "us-west-2", "ap-southeast-1"))
_threshold = _number((250, 400, 600, 900, 1500), " ms p99")
_slo = _choice(("99.5%", "99.9%", "99.95%", "99.99%"))
_log_retention = _number((14, 30, 90, 180, 400), " days")

SRE: tuple[Attribute, ...] = (
    Attribute(
        name="oncall_primary",
        kind="decision",
        label="on-call primary",
        draw=_person_draw,
        mutate=_person_mutate,
        current=("{value} carries the pager for {subject} this rotation.",),
        update="The on-call primary for {subject} swapped from {old} to {value}.",
        question="Who is the on-call primary for {subject}?",
        volatile=1.4,
    ),
    Attribute(
        name="escalation_owner",
        kind="decision",
        label="escalation owner",
        draw=_escalation[0],
        mutate=_escalation[1],
        current=("Escalations for {subject} go to {value}.",),
        update="Escalations for {subject} moved from {old} to {value}.",
        question="Which team takes escalations for {subject}?",
        volatile=0.6,
    ),
    Attribute(
        name="deploy_window",
        kind="policy",
        label="deploy window",
        draw=_window[0],
        mutate=_window[1],
        current=("{subject} deploys only inside its window, {value}.",),
        update="The deploy window for {subject} changed from {old} to {value}.",
        question="When may {subject} deploy?",
    ),
    Attribute(
        name="change_freeze_end",
        kind="policy",
        label="change freeze end date",
        draw=_date_draw,
        mutate=_date_mutate,
        current=("The change freeze on {subject} lasts through {value}.",),
        update="The change freeze on {subject} now ends on {value} instead of {old}.",
        question="When does the change freeze on {subject} end?",
        volatile=0.8,
    ),
    Attribute(
        name="failover_region",
        kind="decision",
        label="failover region",
        draw=_failover[0],
        mutate=_failover[1],
        current=("{subject} fails over to {value}.",),
        update="{subject} now fails over to {value} instead of {old}.",
        question="Which region does {subject} fail over to?",
        volatile=0.5,
    ),
    Attribute(
        name="alert_threshold",
        kind="policy",
        label="paging alert threshold",
        draw=_threshold[0],
        mutate=_threshold[1],
        current=("{subject} pages when latency passes {value}.",),
        update="The paging threshold for {subject} moved from {old} to {value}.",
        question="At what latency does {subject} page the on-call?",
    ),
    Attribute(
        name="slo_target",
        kind="target",
        label="availability objective",
        draw=_slo[0],
        mutate=_slo[1],
        current=("{subject} holds a {value} availability objective.",),
        update="The availability objective for {subject} changed from {old} to {value}.",
        question="What availability objective does {subject} hold?",
        volatile=0.5,
    ),
    Attribute(
        name="log_retention",
        kind="policy",
        label="log retention",
        draw=_log_retention[0],
        mutate=_log_retention[1],
        current=("{subject} keeps its logs for {value}.",),
        update="Log retention for {subject} changed from {old} to {value}.",
        question="How long does {subject} keep its logs?",
        volatile=0.4,
    ),
)

_BY_ARCHETYPE: dict[str, tuple[Attribute, ...]] = {
    "engineering": ENGINEERING,
    "platform": ENGINEERING,
    "security": ENGINEERING,
    "retail": RETAIL,
    "supply_chain": RETAIL,
    "data_engineering": DATA_ENGINEERING,
    "data_analytics": DATA_ANALYTICS,
    "marketing": DATA_ANALYTICS,
    "finance": OPERATIONS,
    "people_ops": OPERATIONS,
    "legal": OPERATIONS,
    "support": OPERATIONS,
    "sales": OPERATIONS,
    "site_reliability": SRE,
}


def attributes_for(archetype: str) -> tuple[Attribute, ...]:
    """The attributes a project of ``archetype`` carries: domain ones first."""
    return _BY_ARCHETYPE.get(archetype, OPERATIONS) + GENERIC


def kind_of(attribute: str) -> str | None:
    """The knowledge kind of the attribute named ``attribute`` (``None`` if none has it)."""
    for group in (GENERIC, *_BY_ARCHETYPE.values()):
        for attr in group:
            if attr.name == attribute:
                return attr.kind
    return None


def value_token(value: str) -> str:
    """The value's most specific whitespace-free run (>= 3 characters).

    A test set that asks a persona to find a value declares a canonical string
    with no whitespace (the gym's discovered-knowledge rule); this is that
    string (``FactVersion.props["canonical"]``), and it occurs verbatim inside
    every statement of the value.
    """
    runs = [r.strip(".,;:") for r in value.split()]
    runs = [r for r in runs if len(r) >= 3]
    if not runs:
        return value
    return max(runs, key=lambda r: (sum(ch.isdigit() or ch in "._-:$%" for ch in r), len(r)))
