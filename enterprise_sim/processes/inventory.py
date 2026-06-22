"""``impl`` escape hatch: a stateful inventory monitor (ARCHITECTURE §12.1, D25).

The declarative :class:`~enterprise_sim.authoring.sdk.Step` surface can express a
*timed sequence* of work, but not a **stateful watcher** that ticks the simulated
world forward day by day, draws demand, decrements stock, and fires only when a
threshold is crossed. That is exactly the kind of logic the §12.1 ``impl`` escape
hatch exists for, and the ``sell_merchandise`` playbook's ``inventory_monitor``
process binds this class via ``impl="enterprise_sim.processes.inventory:InventoryMonitor"``.

**Contract the engine trusts.** The process's ``declares`` block (``events=("LowStock",)``,
``effects=("mutate:stock_level",)``) is what the engine and the linter read; this
class is the realisation that conformance **I5** checks the run against dynamically.
Because there is no ``impl`` runner in the engine yet (see the test-kit scope note),
this class is unit-tested directly rather than driven by ``run_process`` — but it is
written to the same deterministic contract so it drops straight in when the runner
lands.

**Determinism (D26 / lint AST rule / conformance I8).** Nothing here reads the
wall clock or the module-level :mod:`random` conveniences. All variability comes
from a caller-seeded :class:`random.Random` instance, so a given ``(seed, config,
window)`` always yields byte-identical output — the property I6/I8 enforce.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class StockReading:
    """One day's view of a SKU's stock, and whether it tripped the reorder point.

    Attributes:
        day: The reading's day index from the monitoring window start (0-based).
        timestamp: The reading's wall-time within the window (window start + ``day``).
        sku: The SKU node id the reading is about (e.g. ``"sku:widget"``).
        units_sold: Units the seeded demand draw removed that day (``>= 0``).
        stock_level: Remaining on-hand stock after the day's demand (clamped at 0).
        reorder_point: The threshold below which a restock is needed.
        low: ``True`` iff ``stock_level <= reorder_point`` — i.e. ``LowStock`` fires.
    """

    day: int
    timestamp: datetime
    sku: str
    units_sold: int
    stock_level: int
    reorder_point: int
    low: bool


@dataclass(slots=True)
class InventoryMonitor:
    """A stateful watcher that emits ``LowStock`` when a SKU dips to its reorder point.

    The ``sell_merchandise`` ``inventory_monitor`` process's ``impl``. Seeded from
    the scenario root seed, it walks the monitoring window one working concept-day at
    a time, draws that day's demand from a seeded distribution, decrements on-hand
    stock, and — the first time stock falls to or below the reorder point — yields a
    low reading whose KG effect mutates ``stock_level`` and whose business event is
    ``LowStock``. It fires **once per dip** (it goes quiet until a restock lifts stock
    back above the reorder point), so a flat low shelf does not spam the event log.

    Attributes:
        sku: The SKU node id this monitor watches.
        initial_stock: On-hand units at the window start.
        reorder_point: Stock level at/below which ``LowStock`` fires.
        mean_daily_demand: Mean of the seeded daily-demand draw.
        seed: Sub-stream seed; combined with the SKU id for an independent RNG.
    """

    sku: str = "sku:widget"
    initial_stock: int = 60
    reorder_point: int = 10
    mean_daily_demand: float = 6.0
    seed: int = 1
    _rng: random.Random = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # A per-SKU sub-stream so two monitored SKUs in one scenario stay independent
        # yet reproducible. ``hash`` is salted per-process, so derive a stable int
        # from the SKU id ourselves rather than relying on ``hash(self.sku)``.
        salt = sum(ord(ch) for ch in self.sku)
        self._rng = random.Random(self.seed * 1_000 + salt)

    def _demand(self) -> int:
        """Draw one day's demand from the seeded distribution (never negative)."""
        # ``triangular`` over [0, 2*mean] centres on the mean with bounded tails — a
        # plausible daily-sales shape, and deterministic because ``_rng`` is seeded.
        drawn = self._rng.triangular(0.0, 2.0 * self.mean_daily_demand, self.mean_daily_demand)
        return max(0, round(drawn))

    def readings(self, start: datetime, days: int) -> Iterator[StockReading]:
        """Yield a :class:`StockReading` for each of ``days`` days from ``start``.

        Pure generator over the seeded demand process — the caller decides which
        readings become events. ``stock_level`` is clamped at zero (you cannot sell
        what you do not have); ``low`` marks readings at or below the reorder point.
        """
        stock = self.initial_stock
        for day in range(days):
            sold = min(stock, self._demand())
            stock -= sold
            yield StockReading(
                day=day,
                timestamp=start + timedelta(days=day),
                sku=self.sku,
                units_sold=sold,
                stock_level=stock,
                reorder_point=self.reorder_point,
                low=stock <= self.reorder_point,
            )

    def low_stock_events(self, start: datetime, days: int) -> list[StockReading]:
        """The readings that should emit ``LowStock`` — one per distinct dip.

        Edge-triggered, not level-triggered: a ``LowStock`` is reported the day stock
        first crosses to/below the reorder point, then suppressed until a reading
        recovers above it (a restock), so a long low shelf yields a single event
        rather than one per day.
        """
        events: list[StockReading] = []
        armed = True
        for reading in self.readings(start, days):
            if reading.low and armed:
                events.append(reading)
                armed = False
            elif not reading.low:
                armed = True
        return events
