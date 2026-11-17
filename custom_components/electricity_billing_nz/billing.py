"""
Billing-cycle metering: energy per trading period, priced when it settles.

Pure Python (no Home Assistant imports), like tariff.py. Energy readings are
spread over the trading periods they cover, and each period is priced at
its own settled spot with the rates in force at its start. Until then it
counts at the live spot (or the latest known one) and is provisional; after
PROVISIONAL_LIMIT it is final at that price.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal

from .tariff import (
    NZ_TZ,
    NoRatesError,
    add_periods,
    nz_midnight,
    period_start,
    prices,
    resolve_band,
    version_at,
    version_on,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from .tariff import Tariff

type Side = Literal["import", "export"]

STORAGE_VERSION = 1
# A period still without a settled price after this is final at its
# provisional price.
PROVISIONAL_LIMIT = timedelta(hours=48)
# Band for energy in a period no network version covers.
UNKNOWN_BAND = "unknown"
# A reading this far below the last one is a meter reset; a smaller drop is
# noise and ignored. Home Assistant's statistics use the same 10% rule.
RESET_FRACTION = 0.9
# Energy units the meters accept, as multipliers to kWh.
ENERGY_UNITS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def to_kwh(value: float, unit: str | None) -> float:
    """Convert an energy reading in Wh, kWh or MWh to kWh; raises ValueError."""
    if not math.isfinite(value):
        msg = f"Not a reading: {value!r}"
        raise ValueError(msg)
    scale = ENERGY_UNITS.get((unit or "").strip())
    if scale is None:
        msg = f"Unsupported energy unit: {unit!r}"
        raise ValueError(msg)
    return value * scale


def cycle_start(day: date, cycle_day: int) -> date:
    """Return the start of the billing cycle containing ``day``."""
    if day.day >= cycle_day:
        return day.replace(day=cycle_day)
    last_month = day.replace(day=1) - timedelta(days=1)
    return last_month.replace(day=cycle_day)


def next_cycle_start(start: date) -> date:
    """Return the start of the cycle after the one starting on ``start``."""
    if start.month == 12:  # noqa: PLR2004
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def spread(
    since: datetime, until: datetime, kwh: float
) -> Iterator[tuple[datetime, float]]:
    """
    Yield (period start, kWh) for energy used evenly from ``since`` to ``until``.

    Period starts are in UTC. Energy reported at one instant goes to that
    instant's period.
    """
    since, until = since.astimezone(UTC), until.astimezone(UTC)
    if until <= since:
        yield period_start(until).astimezone(UTC), kwh
        return
    total = (until - since).total_seconds()
    start = period_start(since).astimezone(UTC)
    while start < until:
        end = add_periods(start, 1).astimezone(UTC)
        overlap = (min(end, until) - max(start, since)).total_seconds()
        yield start, kwh * overlap / total
        start = end


@dataclass
class Charges:
    """Priced energy for one band, ex GST, NZD."""

    import_kwh: float = 0.0
    # Import kWh times the loss factor, as billed.
    import_kwh_lossy: float = 0.0
    # (spot + margin) x losses: the retailer's energy line.
    energy: float = 0.0
    network: float = 0.0
    transmission: float = 0.0
    levy: float = 0.0
    export_kwh: float = 0.0
    # Spot less margin (floored): the export energy line, without the credit.
    export_energy: float = 0.0
    export_credit: float = 0.0

    def add(self, other: Charges) -> None:
        """Add another set of charges to this one."""
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))


@dataclass
class Cycle:
    """Energy and charges for one billing cycle."""

    start: date
    # When metering covered this cycle from: its start, or the first reading
    # if billing was set up during it.
    since: datetime
    # Metered kWh per band, counted when read: [import, export].
    kwh: dict[str, list[float]] = field(default_factory=dict)
    # Priced (settled or final) charges per band.
    charges: dict[str, Charges] = field(default_factory=dict)
    # Periods made final without a settled price.
    unsettled_periods: int = 0
    # Periods no rate data covered; their energy is metered but not priced.
    unpriced_periods: int = 0

    @property
    def complete(self) -> bool:
        """Return True if metering covered the whole cycle."""
        return self.since <= nz_midnight(self.start)

    def add_kwh(self, band: str, side: Side, kwh: float) -> None:
        """Count metered energy in a band."""
        totals = self.kwh.setdefault(band, [0.0, 0.0])
        totals[0 if side == "import" else 1] += kwh

    def as_dict(self) -> dict[str, Any]:
        """Return the cycle for storage."""
        return {
            "start": self.start.isoformat(),
            "since": self.since.isoformat(),
            "kwh": self.kwh,
            "charges": {band: asdict(c) for band, c in self.charges.items()},
            "unsettled_periods": self.unsettled_periods,
            "unpriced_periods": self.unpriced_periods,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Cycle:
        """Return a stored cycle."""
        return cls(
            start=date.fromisoformat(data["start"]),
            since=datetime.fromisoformat(data["since"]),
            kwh={band: list(v) for band, v in data["kwh"].items()},
            charges={band: Charges(**c) for band, c in data["charges"].items()},
            unsettled_periods=data["unsettled_periods"],
            unpriced_periods=data["unpriced_periods"],
        )


@dataclass
class Pending:
    """Energy in one trading period that has no settled price yet."""

    import_kwh: float = 0.0
    export_kwh: float = 0.0
    # The live spot reported for this period, NZD/kWh.
    live_spot: float | None = None


@dataclass
class Reading:
    """The last usable reading of an energy meter."""

    value: float
    at: datetime


@dataclass(frozen=True)
class Bill:
    """A billing cycle's estimate, as invoice lines (ex GST unless noted)."""

    start: date
    end: date
    complete: bool
    days: int
    daily: dict[str, float]
    # Per band: metered kWh, and the priced charges (with provisional ones).
    kwh: dict[str, list[float]]
    charges: dict[str, Charges]
    provisional_periods: int
    unsettled_periods: int
    unpriced_periods: int
    gst: float

    @cached_property
    def priced(self) -> Charges:
        """Return the charges summed over the bands."""
        total = Charges()
        for charges in self.charges.values():
            total.add(charges)
        return total

    @property
    def import_kwh(self) -> float:
        """Return the metered import kWh."""
        return sum(v[0] for v in self.kwh.values())

    @property
    def export_kwh(self) -> float:
        """Return the metered export kWh."""
        return sum(v[1] for v in self.kwh.values())

    @property
    def import_energy_rate(self) -> float | None:
        """Return the average energy rate per kWh with losses, as billed."""
        priced = self.priced
        if priced.import_kwh_lossy <= 0:
            return None
        return priced.energy / priced.import_kwh_lossy

    @property
    def export_energy_rate(self) -> float | None:
        """Return the average export rate per kWh, without the network credit."""
        priced = self.priced
        if priced.export_kwh <= 0:
            return None
        return priced.export_energy / priced.export_kwh

    @property
    def subtotal(self) -> float:
        """Return the estimate ex GST: charges less export earnings."""
        priced = self.priced
        return (
            sum(self.daily.values())
            + priced.energy
            + priced.network
            + priced.transmission
            + priced.levy
            - priced.export_energy
            - priced.export_credit
        )

    @property
    def total(self) -> float:
        """Return the estimate with GST (when the entry includes GST)."""
        return self.subtotal * self.gst

    def lines(self) -> dict[str, Any]:
        """Return the invoice lines, rounded, for sensor attributes."""
        priced = self.priced

        def money(value: float) -> float:
            return round(value, 2)

        def kwh(value: float) -> float:
            return round(value, 3)

        return {
            "days": self.days,
            "daily_charges": {k: money(v) for k, v in self.daily.items()},
            "network": {
                band: {
                    "import_kwh": kwh(c.import_kwh),
                    "import": money(c.network),
                    "export_kwh": kwh(c.export_kwh),
                    "export_credit": money(c.export_credit),
                }
                for band, c in self.charges.items()
            },
            "energy": {
                "kwh_with_losses": kwh(priced.import_kwh_lossy),
                "amount": money(priced.energy),
            },
            "transmission": money(priced.transmission),
            "levy": money(priced.levy),
            "export": {
                "kwh": kwh(priced.export_kwh),
                "amount": money(priced.export_energy),
            },
            "subtotal": money(self.subtotal),
            "gst": money(self.total - self.subtotal),
            "total": money(self.total),
        }


def _band(tariff: Tariff, start: datetime) -> str:
    network = version_at(tariff.network, start)
    return resolve_band(network.bands, start) if network else UNKNOWN_BAND


def _price_period(
    tariff: Tariff, start: datetime, usage: Pending, spot: float
) -> tuple[str, Charges, float, float]:
    """
    Price one period's energy at ``spot``; raises NoRatesError.

    Returns the band, the charges ex GST, and the import cost and export
    earnings including GST (as the price sensors report them).
    """
    imp, exp = prices(tariff, spot, start)
    gst = imp.gst
    export_total = exp.price / gst * usage.export_kwh
    export_credit = exp.export_credit * usage.export_kwh
    charges = Charges(
        import_kwh=usage.import_kwh,
        import_kwh_lossy=usage.import_kwh * imp.loss_factor,
        energy=(imp.spot + imp.margin) * imp.loss_factor * usage.import_kwh,
        network=imp.network * usage.import_kwh,
        transmission=imp.transmission * usage.import_kwh,
        levy=imp.levy * usage.import_kwh,
        export_kwh=usage.export_kwh,
        export_energy=export_total - export_credit,
        export_credit=export_credit,
    )
    return (
        imp.tou_band,
        charges,
        imp.price * usage.import_kwh,
        exp.price * usage.export_kwh,
    )


class BillingMeter:
    """Meter energy per trading period and price it into billing cycles."""

    def __init__(
        self,
        tariff: Tariff,
        cycle_day: int,
        data: Mapping[str, Any] | None = None,
        sources: Mapping[Side, str] | None = None,
    ) -> None:
        """
        Start empty, or from stored data.

        ``sources`` names the meter read for each side. A stored reading from
        another meter is dropped, so the new meter starts from a baseline.
        """
        self.tariff = tariff
        self.cycle_day = cycle_day
        self.sources: dict[Side, str] = dict(sources or {})
        self.readings: dict[Side, Reading] = {}
        # When metering began (or restarted): cycles are complete only if it
        # began before their start.
        self.since: datetime | None = None
        # Keyed by cycle start; only the current and previous cycles are kept.
        self.cycles: dict[date, Cycle] = {}
        # Keyed by period start in UTC.
        self.pending: dict[datetime, Pending] = {}
        self.import_cost = 0.0
        self.export_earnings = 0.0
        self.latest_spot: float | None = None
        if data:
            self._load(data)

    def _load(self, data: Mapping[str, Any]) -> None:
        stored_sources = data.get("sources", {})
        self.readings = {
            side: Reading(r["value"], datetime.fromisoformat(r["at"]))
            for side, r in data.get("readings", {}).items()
            if stored_sources.get(side) == self.sources.get(side)
        }
        if data.get("since") and "import" in self.readings:
            self.since = datetime.fromisoformat(data["since"])
        self.pending = {
            datetime.fromisoformat(p["start"]): Pending(
                p["import_kwh"], p["export_kwh"], p.get("live_spot")
            )
            for p in data.get("pending", [])
        }
        self.import_cost = data.get("import_cost", 0.0)
        self.export_earnings = data.get("export_earnings", 0.0)
        self.latest_spot = data.get("latest_spot")
        # Cycles from another cycle day don't line up with this one's. The
        # new ones are metered from the last reading on.
        if data.get("cycle_day") == self.cycle_day:
            cycles = (Cycle.from_dict(c) for c in data.get("cycles", []))
            self.cycles = {c.start: c for c in cycles}
        elif self.since is not None:
            self.since = max(r.at for r in self.readings.values())

    def as_dict(self) -> dict[str, Any]:
        """Return the meter's state for storage."""
        return {
            "cycle_day": self.cycle_day,
            "sources": self.sources,
            "since": self.since.isoformat() if self.since else None,
            "readings": {
                side: {"value": r.value, "at": r.at.isoformat()}
                for side, r in self.readings.items()
            },
            "cycles": [c.as_dict() for c in self.cycles.values()],
            "pending": [
                {"start": start.isoformat(), **asdict(p)}
                for start, p in sorted(self.pending.items())
            ],
            "import_cost": self.import_cost,
            "export_earnings": self.export_earnings,
            "latest_spot": self.latest_spot,
        }

    def _cycle_for(self, at: datetime) -> Cycle | None:
        """Return the kept cycle containing ``at``, creating the current one."""
        start = cycle_start(at.astimezone(NZ_TZ).date(), self.cycle_day)
        if start in self.cycles:
            return self.cycles[start]
        if self.cycles and start < max(self.cycles):
            return None  # older than the kept cycles
        # Covered from its start if metering began before it.
        cycle = Cycle(start=start, since=max(nz_midnight(start), self.since or at))
        self.cycles[start] = cycle
        for old in sorted(self.cycles)[:-2]:
            del self.cycles[old]
        return cycle

    def add_reading(self, side: Side, value: float, at: datetime) -> bool:
        """
        Count a meter reading in kWh; return True if any energy was added.

        The increase since the last reading is spread over the periods in
        between. A reading well below the last is a meter reset, so all of it
        is new; a small drop is ignored, keeping the higher baseline.
        """
        last = self.readings.get(side)
        if last is None:
            # The first reading is the baseline; make sure the cycle exists.
            if self.since is None:
                self.since = at
            self._cycle_for(at)
            self.readings[side] = Reading(value, at)
            return False
        if value >= last.value:
            used = value - last.value
        elif value < last.value * RESET_FRACTION:
            used = value
        else:
            return False
        self.readings[side] = Reading(value, at)
        if used <= 0:
            return False
        for start, kwh in spread(last.at, at, used):
            cycle = self._cycle_for(start)
            if cycle is not None:
                cycle.add_kwh(_band(self.tariff, start), side, kwh)
            usage = self.pending.setdefault(start, Pending())
            if side == "import":
                usage.import_kwh += kwh
            else:
                usage.export_kwh += kwh
        return True

    def set_live_spot(self, start: datetime, spot: float) -> None:
        """Record the live spot for a period, its price until it settles."""
        key = start.astimezone(UTC)
        if (usage := self.pending.get(key)) is not None:
            usage.live_spot = spot
        else:
            # Energy for this period may arrive later in the period.
            self.pending[key] = Pending(live_spot=spot)
        self.latest_spot = spot

    def settle(self, start: datetime, spot: float) -> bool:
        """Price a period at its settled spot; return True if it had energy."""
        usage = self.pending.pop(start.astimezone(UTC), None)
        if usage is None:
            return False
        return self._finalise(start, usage, spot, settled=True)

    def _finalise(
        self, start: datetime, usage: Pending, spot: float, *, settled: bool
    ) -> bool:
        if usage.import_kwh <= 0 and usage.export_kwh <= 0:
            return False
        cycle = self._cycle_for(start)
        try:
            band, charges, cost, earnings = _price_period(
                self.tariff, start, usage, spot
            )
        except NoRatesError:
            if cycle is not None:
                cycle.unpriced_periods += 1
            return True
        self.import_cost += cost
        self.export_earnings += earnings
        if cycle is not None:
            cycle.charges.setdefault(band, Charges()).add(charges)
            if not settled:
                cycle.unsettled_periods += 1
        return True

    def tick(self, now: datetime) -> bool:
        """
        Start a new cycle when it's due and finalise overdue periods.

        Returns True if anything changed.
        """
        changed = False
        start = cycle_start(now.astimezone(NZ_TZ).date(), self.cycle_day)
        # Without a reading there's nothing to meter a cycle from yet.
        if self.since is not None and start not in self.cycles:
            self._cycle_for(now)
            changed = True
        cutoff = now.astimezone(UTC) - PROVISIONAL_LIMIT
        for key in [k for k in self.pending if k < cutoff]:
            usage = self.pending.pop(key)
            spot = usage.live_spot if usage.live_spot is not None else self.latest_spot
            if spot is not None:
                self._finalise(key, usage, spot, settled=False)
            elif (cycle := self._cycle_for(key)) is not None and (
                usage.import_kwh > 0 or usage.export_kwh > 0
            ):
                cycle.unpriced_periods += 1
            changed = True
        return changed

    def bill(self, now: datetime, *, previous: bool = False) -> Bill | None:
        """
        Return the current (or previous) cycle's estimate, or None.

        Pending periods count at their provisional price.
        """
        current = cycle_start(now.astimezone(NZ_TZ).date(), self.cycle_day)
        if previous:
            earlier = [s for s in self.cycles if s < current]
            key = max(earlier) if earlier else None
        else:
            key = current if current in self.cycles else None
        if key is None:
            return None
        cycle = self.cycles[key]
        end = next_cycle_start(cycle.start)

        charges = {band: Charges(**asdict(c)) for band, c in cycle.charges.items()}
        provisional = 0
        for key, usage in self.pending.items():
            local = key.astimezone(NZ_TZ).date()
            if not cycle.start <= local < end:
                continue
            spot = usage.live_spot if usage.live_spot is not None else self.latest_spot
            if spot is None or (usage.import_kwh <= 0 and usage.export_kwh <= 0):
                continue
            try:
                band, priced, _, _ = _price_period(self.tariff, key, usage, spot)
            except NoRatesError:
                continue
            charges.setdefault(band, Charges()).add(priced)
            provisional += 1

        last_day = min(now.astimezone(NZ_TZ).date(), end - timedelta(days=1))
        daily = {"network": 0.0, "metering": 0.0, "retailer": 0.0}
        days = 0
        # From when metering began, so a partial cycle's charges match its energy.
        day = max(cycle.start, cycle.since.astimezone(NZ_TZ).date())
        while day <= last_day:
            network = version_on(self.tariff.network, day)
            retailer = version_on(self.tariff.retailer, day)
            if network is not None and retailer is not None:
                daily["network"] += network.daily
                daily["metering"] += retailer.metering_daily
                daily["retailer"] += retailer.retailer_daily
                days += 1
            day += timedelta(days=1)

        return Bill(
            start=cycle.start,
            end=end,
            complete=cycle.complete,
            days=days,
            daily=daily,
            kwh={band: list(v) for band, v in cycle.kwh.items()},
            charges=charges,
            provisional_periods=provisional,
            unsettled_periods=cycle.unsettled_periods,
            unpriced_periods=cycle.unpriced_periods,
            gst=self.tariff.gst,
        )
