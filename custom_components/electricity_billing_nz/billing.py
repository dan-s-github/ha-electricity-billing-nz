"""
Billing-cycle metering: energy per trading period, priced when it settles.

Pure Python (no Home Assistant imports), like tariff.py. Energy readings are
spread over the trading periods they cover, and each period is priced at
its own settled spot with the rates in force at its start. Until then it
counts at the live spot (or the latest known one) and is provisional; after
PROVISIONAL_LIMIT it is final at that price.

Cycles keep their final periods' energy and spot, not their charges, so a
rate change dated into a cycle reprices it when the entry reloads.
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

# 2: cycles keep their periods. Version 1 cycles kept only totals, which
# are carried over as they are; BillingMeter reads both.
STORAGE_VERSION = 2
# A period still without a settled price after this is final at its
# provisional price.
PROVISIONAL_LIMIT = timedelta(hours=48)
# Band for energy in a period no network version covers.
UNKNOWN_BAND = "unknown"
# A reading this far below the last one is a meter reset; a smaller drop is
# noise and ignored. Home Assistant's statistics use the same 10% rule.
RESET_FRACTION = 0.9
# A meter that reports at least SLOW_METER_KWH after more than
# SLOW_METER_GAP without a reading can't say which half-hours the energy was
# used in: it's spread evenly over the gap, and priced at the wrong spots.
SLOW_METER_GAP = timedelta(hours=1)
SLOW_METER_KWH = 0.5
# Energy units the meters accept, as multipliers to kWh.
ENERGY_UNITS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
# Power units the meters accept, as multipliers to kW.
POWER_UNITS = {"W": 0.001, "kW": 1.0, "MW": 1000.0}


def meter_kind(unit: str | None) -> Literal["energy", "power"] | None:
    """Return whether a meter's unit is one of energy or power, or None."""
    unit = (unit or "").strip()
    if unit in ENERGY_UNITS:
        return "energy"
    if unit in POWER_UNITS:
        return "power"
    return None


def read_meter(
    value: str | float, unit: str | None
) -> tuple[Literal["energy", "power"], float] | None:
    """Return a meter's value as energy in kWh or power in kW, or None."""
    try:
        if (kind := meter_kind(unit)) == "energy":
            return kind, to_kwh(float(value), unit)
        if kind == "power":
            return kind, to_kw(float(value), unit)
    except ValueError:
        pass
    return None


def _scale(value: float, unit: str | None, units: Mapping[str, float]) -> float:
    if not math.isfinite(value):
        msg = f"Not a reading: {value!r}"
        raise ValueError(msg)
    scale = units.get((unit or "").strip())
    if scale is None:
        msg = f"Unsupported unit: {unit!r}"
        raise ValueError(msg)
    return value * scale


def to_kwh(value: float, unit: str | None) -> float:
    """Convert an energy reading in Wh, kWh or MWh to kWh; raises ValueError."""
    return _scale(value, unit, ENERGY_UNITS)


def to_kw(value: float, unit: str | None) -> float:
    """Convert a power reading in W, kW or MW to kW; raises ValueError."""
    return _scale(value, unit, POWER_UNITS)


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
class Totals:
    """Metered kWh and priced charges per band."""

    # [import, export] kWh per band.
    kwh: dict[str, list[float]] = field(default_factory=dict)
    charges: dict[str, Charges] = field(default_factory=dict)
    # Periods made final without a settled price.
    unsettled_periods: int = 0
    # Final periods without a spot or rate data; their energy is metered
    # but not priced.
    unpriced_periods: int = 0

    def copy(self) -> Totals:
        """Return a copy that can be added to without changing this one."""
        return Totals.from_dict(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        """Return the totals for storage."""
        return {
            "kwh": self.kwh,
            "charges": {band: asdict(c) for band, c in self.charges.items()},
            "unsettled_periods": self.unsettled_periods,
            "unpriced_periods": self.unpriced_periods,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Totals:
        """Return stored totals."""
        return cls(
            kwh={band: list(v) for band, v in data["kwh"].items()},
            charges={band: Charges(**c) for band, c in data["charges"].items()},
            unsettled_periods=data["unsettled_periods"],
            unpriced_periods=data["unpriced_periods"],
        )


@dataclass
class Period:
    """A final trading period: its energy and the spot it was priced at."""

    import_kwh: float = 0.0
    export_kwh: float = 0.0
    # None when no spot was known when it was made final.
    spot: float | None = None
    settled: bool = True


@dataclass
class Cycle:
    """Energy and charges for one billing cycle."""

    start: date
    # When metering covered this cycle from: its start, or when every meter
    # had a first reading if billing was set up (or a meter added) during it.
    since: datetime
    # Final periods, keyed by period start in UTC.
    periods: dict[datetime, Period] = field(default_factory=dict)
    # Totals from storage version 1, which kept no periods; they can't be
    # repriced.
    carried: Totals | None = None
    # The periods priced at the meter's tariff, with any carried totals.
    totals: Totals = field(default_factory=Totals, compare=False, repr=False)

    @property
    def complete(self) -> bool:
        """Return True if metering covered the whole cycle."""
        return self.since <= nz_midnight(self.start)

    def as_dict(self) -> dict[str, Any]:
        """Return the cycle for storage."""
        data: dict[str, Any] = {
            "start": self.start.isoformat(),
            "since": self.since.isoformat(),
            "periods": [
                [key.isoformat(), p.import_kwh, p.export_kwh, p.spot, p.settled]
                for key, p in sorted(self.periods.items())
            ],
        }
        if self.carried is not None:
            data["carried"] = self.carried.as_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Cycle:
        """Return a stored cycle, from storage version 2 or 1."""
        start = date.fromisoformat(data["start"])
        since = datetime.fromisoformat(data["since"])
        if "periods" not in data:
            return cls(start, since, carried=Totals.from_dict(data))
        return cls(
            start,
            since,
            periods={
                datetime.fromisoformat(key): Period(imp, exp, spot, settled)
                for key, imp, exp, spot, settled in data["periods"]
            },
            carried=Totals.from_dict(data["carried"]) if "carried" in data else None,
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


@dataclass
class SlowReading:
    """Energy a meter reported only after a long gap without readings."""

    kwh: float
    gap: timedelta


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
    tariff: Tariff, start: datetime, usage: Pending | Period, spot: float
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


def _tally(
    tariff: Tariff,
    totals: Totals,
    start: datetime,
    usage: Pending | Period,
    spot: float | None,
) -> tuple[float, float] | None:
    """
    Add a period's kWh, and its charges if it can be priced.

    Returns the import cost and export earnings including GST, or None if
    the period couldn't be priced.
    """
    row = totals.kwh.setdefault(_band(tariff, start), [0.0, 0.0])
    row[0] += usage.import_kwh
    row[1] += usage.export_kwh
    if spot is None:
        return None
    try:
        band, charges, cost, earnings = _price_period(tariff, start, usage, spot)
    except NoRatesError:
        return None
    totals.charges.setdefault(band, Charges()).add(charges)
    return cost, earnings


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
        Without it, only the import meter is expected.
        """
        self.tariff = tariff
        self.cycle_day = cycle_day
        self.sources: dict[Side, str] = dict(sources or {})
        self.sides: tuple[Side, ...] = tuple(self.sources) or ("import",)
        # Energy meters' last readings, in kWh.
        self.readings: dict[Side, Reading] = {}
        # Power sensors' last readings, in kW: each holds until the next.
        self.power: dict[Side, Reading] = {}
        # Sides whose next reading follows a gap that isn't the meter's
        # fault: a restart, or the meter being unavailable. An energy meter's
        # gap isn't flagged as slow; a power sensor's isn't integrated, as
        # its power then is unknown.
        self._exempt: set[Side] = set()
        # Long gaps found by add_reading, for the caller to report.
        self.slow: dict[Side, SlowReading] = {}
        # When every meter had a first reading (metering began, or restarted):
        # cycles are complete only if it was before their start.
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
        self.readings, self.power = (
            {
                side: Reading(r["value"], datetime.fromisoformat(r["at"]))
                for side, r in data.get(key, {}).items()
                if stored_sources.get(side) == self.sources.get(side)
            }
            for key in ("readings", "power")
        )
        self._exempt = set(self.readings) | set(self.power)
        # A meter without a stored reading (new or changed) moves it on.
        if data.get("since") and all(self._has_baseline(s) for s in self.sides):
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

        stored = data.get("cycles", [])
        cycles = [Cycle.from_dict(c) for c in stored]
        # Version 1 cycles counted pending energy when it was read; it's
        # counted from the pending periods now.
        from_v1 = [c for c, raw in zip(cycles, stored, strict=True) if "kwh" in raw]
        for key, usage in self.pending.items():
            cycle = next((c for c in from_v1 if self._holds(c, key)), None)
            if cycle is not None and cycle.carried is not None:
                row = cycle.carried.kwh.get(_band(self.tariff, key))
                if row is not None:
                    row[0] -= usage.import_kwh
                    row[1] -= usage.export_kwh
        if data.get("cycle_day") == self.cycle_day:
            self.cycles = {c.start: c for c in cycles}
        elif cycles and all(c.carried is None for c in cycles):
            self._rebucket(cycles)
        elif self.since is not None:
            # Totals can't be split into other cycles: meter the new ones
            # from the last reading on.
            self.since = max(
                r.at for r in (*self.readings.values(), *self.power.values())
            )
        for cycle in self.cycles.values():
            self._retally(cycle)

    def _rebucket(self, cycles: list[Cycle]) -> None:
        """Move periods from another cycle day's cycles into this day's."""
        covered = min(c.since for c in cycles)
        for old in cycles:
            for key, period in old.periods.items():
                start = cycle_start(key.astimezone(NZ_TZ).date(), self.cycle_day)
                cycle = self.cycles.get(start)
                if cycle is None:
                    since = max(nz_midnight(start), covered)
                    cycle = self.cycles[start] = Cycle(start, since)
                cycle.periods[key] = period
        for old_start in sorted(self.cycles)[:-2]:
            del self.cycles[old_start]

    def _retally(self, cycle: Cycle) -> None:
        """Price a cycle's periods at the meter's tariff."""
        cycle.totals = cycle.carried.copy() if cycle.carried else Totals()
        for key, period in sorted(cycle.periods.items()):
            self._add_final(cycle.totals, key, period)

    def _add_final(
        self, totals: Totals, key: datetime, period: Period
    ) -> tuple[float, float] | None:
        """Add a final period to a cycle's totals; return its cost and earnings."""
        priced = _tally(self.tariff, totals, key, period, period.spot)
        if priced is None:
            totals.unpriced_periods += 1
        elif not period.settled:
            totals.unsettled_periods += 1
        return priced

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
            "power": {
                side: {"value": r.value, "at": r.at.isoformat()}
                for side, r in self.power.items()
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

    def _holds(self, cycle: Cycle, key: datetime) -> bool:
        """Return True if the period starting at ``key`` is in ``cycle``."""
        local = key.astimezone(NZ_TZ).date()
        return cycle.start <= local < next_cycle_start(cycle.start)

    def _cycle_for(self, at: datetime) -> Cycle | None:
        """
        Return the kept cycle containing ``at``, creating it if it's due.

        A cycle is created if it's the newest, or the one just before the
        newest (energy read late, after a restart that skipped a cycle).
        """
        start = cycle_start(at.astimezone(NZ_TZ).date(), self.cycle_day)
        if start in self.cycles:
            return self.cycles[start]
        if self.cycles:
            newest = max(self.cycles)
            before_newest = cycle_start(newest - timedelta(days=1), self.cycle_day)
            if start < before_newest:
                return None  # older than the kept cycles
        # Covered from its start if metering began before it.
        cycle = Cycle(start=start, since=max(nz_midnight(start), self.since or at))
        self.cycles[start] = cycle
        for old in sorted(self.cycles)[:-2]:
            del self.cycles[old]
        return cycle

    def add_reading(self, side: Side, value: float, at: datetime) -> bool:
        """
        Count a meter reading in kWh; return True if the bill may have changed.

        That's when energy was added, or a meter's first reading was taken.
        The increase since the last reading is spread over the periods in
        between. A reading well below the last is a meter reset, so all of it
        is new; a small drop is ignored, keeping the higher baseline.
        """
        self.slow.pop(side, None)
        last = self.readings.get(side)
        if last is None:
            self.readings[side] = Reading(value, at)
            return self._first_reading(at)
        if value >= last.value:
            used = value - last.value
        elif value < last.value * RESET_FRACTION:
            used = value
        else:
            return False
        # The exemption lasts until the baseline moves on.
        exempt = side in self._exempt
        self._exempt.discard(side)
        if not exempt and used >= SLOW_METER_KWH and at - last.at > SLOW_METER_GAP:
            self.slow[side] = SlowReading(used, at - last.at)
        # An unchanged reading moves the baseline on, so the next increase
        # isn't spread back over time when nothing was used.
        self.readings[side] = Reading(value, at)
        if used <= 0:
            return False
        self._add_energy(side, last.at, at, used)
        return True

    def add_power(self, side: Side, kw: float, at: datetime) -> bool:
        """
        Count a power reading in kW; return True if the bill may have changed.

        The last reading's power held until this one: a sensor that reports
        only changes can be idle for hours and still be placed exactly.
        Negative power counts as none.
        """
        last = self.power.get(side)
        if last is not None and at < last.at:
            # Stamped before the last advance: the new power holds from then,
            # and is known from then, so an exemption ends here too.
            self.power[side] = Reading(max(kw, 0.0), last.at)
            self._exempt.discard(side)
            return False
        self.power[side] = Reading(max(kw, 0.0), at)
        if last is None:
            return self._first_reading(at)
        if side in self._exempt:
            # The power since the last reading is unknown: start afresh.
            self._exempt.discard(side)
            return False
        return self._integrate(side, last, at)

    def advance(self, now: datetime) -> bool:
        """
        Count the power sensors' held power up to ``now``.

        Called before periods settle and at each period boundary, so energy
        from a steady power lands in its periods before they're priced.
        """
        changed = False
        for side, last in list(self.power.items()):
            if side in self._exempt or now <= last.at:
                continue
            self.power[side] = Reading(last.value, now)
            changed |= self._integrate(side, last, now)
        return changed

    def _integrate(self, side: Side, last: Reading, until: datetime) -> bool:
        kwh = last.value * (until - last.at).total_seconds() / 3600
        if kwh <= 0:
            return False
        self._add_energy(side, last.at, until, kwh)
        return True

    def _has_baseline(self, side: Side) -> bool:
        return side in self.readings or side in self.power

    def _first_reading(self, at: datetime) -> bool:
        """Make sure the cycle exists; note when every meter has a baseline."""
        cycle = self._cycle_for(at)
        if self.since is None and all(self._has_baseline(s) for s in self.sides):
            self.since = at
            if cycle is not None:
                cycle.since = max(cycle.since, at)
        return True

    def _add_energy(
        self, side: Side, since: datetime, until: datetime, used: float
    ) -> None:
        """Share energy used from ``since`` to ``until`` over its periods."""
        for start, kwh in spread(since, until, used):
            imp, exp = (kwh, 0.0) if side == "import" else (0.0, kwh)
            cycle = self._cycle_for(start)
            if cycle is not None and (final := cycle.periods.get(start)) is not None:
                # Read late, into a period already made final: it joins that
                # period at its price, which won't be settled again.
                self._add_to_final(cycle, start, final, imp, exp)
                continue
            usage = self.pending.setdefault(start, Pending())
            usage.import_kwh += imp
            usage.export_kwh += exp

    def forget_restart(self) -> None:
        """Drop the restart exemption: loaded on a reload, nothing was down."""
        self._exempt.clear()

    def mark_unavailable(self, side: Side, now: datetime) -> bool:
        """
        Note the meter went unavailable at ``now``; return True if energy was added.

        A power sensor's held power counts up to now; its gap from then on
        isn't counted. An energy meter's next gap isn't flagged as slow.
        """
        changed = False
        if side in self.power and side not in self._exempt:
            last = self.power[side]
            if now > last.at:
                self.power[side] = Reading(last.value, now)
                changed = self._integrate(side, last, now)
        if self._has_baseline(side):
            self._exempt.add(side)
        return changed

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
        self, start: datetime, usage: Pending, spot: float | None, *, settled: bool
    ) -> bool:
        if usage.import_kwh <= 0 and usage.export_kwh <= 0:
            return False
        key = start.astimezone(UTC)
        period = Period(usage.import_kwh, usage.export_kwh, spot, settled)
        if (cycle := self._cycle_for(key)) is not None:
            if (final := cycle.periods.get(key)) is not None:
                # add_reading sends energy for final periods to them, so
                # this is only a safeguard: add to the period, don't replace it.
                self._add_to_final(
                    cycle, key, final, usage.import_kwh, usage.export_kwh
                )
                return True
            cycle.periods[key] = period
            priced = self._add_final(cycle.totals, key, period)
        else:
            # Too old for a kept cycle: it still counts in the lifetime totals.
            priced = _tally(self.tariff, Totals(), key, period, spot)
        self._count_lifetime(priced)
        return True

    def _count_lifetime(self, priced: tuple[float, float] | None) -> None:
        """Add a priced period's cost and earnings to the lifetime totals."""
        if priced is not None:
            self.import_cost += priced[0]
            self.export_earnings += priced[1]

    def _add_to_final(
        self,
        cycle: Cycle,
        key: datetime,
        final: Period,
        import_kwh: float,
        export_kwh: float,
    ) -> None:
        """Add late energy to a final period, at the spot it was priced at."""
        final.import_kwh += import_kwh
        final.export_kwh += export_kwh
        late = Period(import_kwh, export_kwh, final.spot, final.settled)
        # The period is already counted as unsettled or unpriced if it is.
        self._count_lifetime(_tally(self.tariff, cycle.totals, key, late, final.spot))

    def tick(self, now: datetime) -> bool:
        """
        Start a new cycle when it's due and finalise overdue periods.

        Returns True if anything changed.
        """
        changed = False
        start = cycle_start(now.astimezone(NZ_TZ).date(), self.cycle_day)
        # Without a reading there's nothing to meter a cycle from yet.
        if (self.readings or self.power) and start not in self.cycles:
            self._cycle_for(now)
            changed = True
        cutoff = now.astimezone(UTC) - PROVISIONAL_LIMIT
        for key in [k for k in self.pending if k < cutoff]:
            usage = self.pending.pop(key)
            spot = usage.live_spot if usage.live_spot is not None else self.latest_spot
            self._finalise(key, usage, spot, settled=False)
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

        totals = cycle.totals.copy()
        provisional = 0
        for start, usage in sorted(self.pending.items()):
            if not self._holds(cycle, start) or (
                usage.import_kwh <= 0 and usage.export_kwh <= 0
            ):
                continue
            spot = usage.live_spot if usage.live_spot is not None else self.latest_spot
            if _tally(self.tariff, totals, start, usage, spot) is not None:
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
            kwh=totals.kwh,
            charges=totals.charges,
            provisional_periods=provisional,
            unsettled_periods=totals.unsettled_periods,
            unpriced_periods=totals.unpriced_periods,
            gst=self.tariff.gst,
        )
