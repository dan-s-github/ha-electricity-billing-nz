"""
Landed price model for NZ spot pass-through plans.

Pure Python (no Home Assistant imports) so the sensors, the price_at service
and offline tools share one implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from zoneinfo import ZoneInfo

import holidays

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

NZ_TZ = ZoneInfo("Pacific/Auckland")
PERIOD = timedelta(minutes=30)
GST_RATE = 1.15
MINUTES_PER_DAY = 24 * 60
# The band treated as peak when a schedule doesn't list its peak bands.
DEFAULT_PEAK_BAND = "peak"
# Search horizon for the next band change. Vector has no peak Oct-Apr, so the
# next change can be up to seven months away.
BAND_CHANGE_HORIZON_DAYS = 400


class NoRatesError(LookupError):
    """
    Some rates needed for a price don't cover the requested time.

    Pricing without them would silently give a too-low price.
    """

    def __init__(self, at: datetime, missing: Sequence[str]) -> None:
        """Record the time and which rates ("network", "loss", ...) are missing."""
        self.at = at
        self.missing = tuple(missing)
        super().__init__(f"No {', '.join(self.missing)} rates cover {at.isoformat()}")


def _missing(**versions: object) -> list[str]:
    """Return the names of the versions that are None."""
    return [name for name, version in versions.items() if version is None]


class _Versioned(Protocol):
    @property
    def effective_from(self) -> date: ...


@dataclass(frozen=True)
class BandRule:
    """A set of half-hour windows that belong to one band."""

    band: str
    months: frozenset[int]
    # (start, end) in minutes after local midnight, end exclusive.
    windows: tuple[tuple[int, int], ...]
    # Monday = 0. None = every day.
    weekdays: frozenset[int] | None = None
    # None = regardless; True = public holidays only; False = not on holidays.
    holidays: bool | None = None


@dataclass(frozen=True)
class BandSchedule:
    """Time-of-use bands: the first matching rule wins, else the default."""

    name: str
    rules: tuple[BandRule, ...]
    default: str
    # Bands the peak binary sensor is on for.
    peak: frozenset[str] = frozenset({DEFAULT_PEAK_BAND})

    @property
    def bands(self) -> tuple[str, ...]:
        """Return every band this schedule can resolve to, default first."""
        names = [self.default]
        names.extend(r.band for r in self.rules if r.band not in names)
        return tuple(names)


@dataclass(frozen=True)
class NetworkVersion:
    """Distribution prices for one price category from one date (ex GST)."""

    effective_from: date
    daily: float
    import_rates: Mapping[str, float]
    export_credit: Mapping[str, float]
    bands: BandSchedule
    source: str | None = None


@dataclass(frozen=True)
class LossVersion:
    """Loss factor for one loss category from one date."""

    effective_from: date
    factor: float
    source: str | None = None


@dataclass(frozen=True)
class RetailerVersion:
    """Retailer plan charges from one date (ex GST, NZD)."""

    effective_from: date
    import_margin: float = 0.0
    export_margin: float = 0.0
    transmission: float = 0.0
    levy: float = 0.0
    metering_daily: float = 0.0
    retailer_daily: float = 0.0
    export_floor: float | None = None


@dataclass(frozen=True)
class Tariff:
    """All rate versions for one ICP, each tuple sorted by effective date."""

    network: tuple[NetworkVersion, ...]
    loss: tuple[LossVersion, ...]
    retailer: tuple[RetailerVersion, ...]
    gst: float = GST_RATE


@dataclass(frozen=True)
class PriceBreakdown:
    """A landed price and the components it was built from (all ex GST)."""

    price: float
    spot: float
    margin: float
    loss_factor: float
    network: float
    transmission: float
    levy: float
    export_credit: float
    gst: float
    tou_band: str
    network_version: date
    retailer_version: date

    @staticmethod
    def attribute_keys() -> frozenset[str]:
        """Return the keys as_attributes() produces."""
        return frozenset(f.name for f in fields(PriceBreakdown)) - {"price"}

    def as_attributes(self) -> dict[str, Any]:
        """Return the components as sensor attributes."""
        return {
            "spot": round(self.spot, 6),
            "margin": round(self.margin, 6),
            "loss_factor": self.loss_factor,
            "network": round(self.network, 6),
            "transmission": round(self.transmission, 6),
            "levy": round(self.levy, 6),
            "export_credit": round(self.export_credit, 6),
            "gst": self.gst,
            "tou_band": self.tou_band,
            "network_version": self.network_version.isoformat(),
            "retailer_version": self.retailer_version.isoformat(),
        }


def nz_midnight(day: date) -> datetime:
    """Return 00:00 Pacific/Auckland on the given day."""
    return datetime.combine(day, time.min, tzinfo=NZ_TZ)


def period_start(at: datetime) -> datetime:
    """
    Return the start of the trading period containing ``at``.

    NZ offsets are whole hours, so flooring in UTC lands on :00 or :30 local.
    Local wall-clock arithmetic would lose which pass of the repeated hour
    ``at`` is in when DST ends.
    """
    utc = at.astimezone(UTC)
    return utc.replace(
        minute=utc.minute - utc.minute % 30, second=0, microsecond=0
    ).astimezone(NZ_TZ)


def add_periods(at: datetime, count: int) -> datetime:
    """Return ``at`` moved by ``count`` trading periods, in absolute time."""
    return (at.astimezone(UTC) + count * PERIOD).astimezone(NZ_TZ)


def version_at[V: _Versioned](versions: Sequence[V], at: datetime) -> V | None:
    """Return the version in force at ``at`` (NZ date), or None."""
    return version_on(versions, at.astimezone(NZ_TZ).date())


def version_on[V: _Versioned](versions: Sequence[V], day: date) -> V | None:
    """Return the version in force on ``day``, or None."""
    current = None
    for version in versions:
        if version.effective_from <= day:
            current = version
        else:
            break
    return current


@lru_cache(maxsize=8)
def _nz_holidays(year: int) -> holidays.HolidayBase:
    return holidays.country_holidays("NZ", years=year)


def _rule_matches(rule: BandRule, local: datetime) -> bool:
    if local.month not in rule.months:
        return False
    if rule.weekdays is not None and local.weekday() not in rule.weekdays:
        return False
    if rule.holidays is not None:
        is_holiday = local.date() in _nz_holidays(local.year)
        if is_holiday != rule.holidays:
            return False
    minute = local.hour * 60 + local.minute
    return any(start <= minute < end for start, end in rule.windows)


def resolve_band(schedule: BandSchedule, at: datetime) -> str:
    """Return the band in force at ``at``, judged in NZ local time."""
    local = at.astimezone(NZ_TZ)
    for rule in schedule.rules:
        if _rule_matches(rule, local):
            return rule.band
    return schedule.default


def _candidate_edges(schedule: BandSchedule, day: date) -> Iterable[datetime]:
    """Yield every instant on ``day`` where the band could change."""
    yield nz_midnight(day)
    for rule in schedule.rules:
        if day.month not in rule.months:
            continue
        for start, end in rule.windows:
            for minute in (start, end):
                if minute >= MINUTES_PER_DAY:
                    continue  # midnight of the next day is covered there
                yield datetime.combine(
                    day, time(minute // 60, minute % 60), tzinfo=NZ_TZ
                )


def next_band_change(schedule: BandSchedule, after: datetime) -> datetime | None:
    """Return the first instant after ``after`` where the band differs."""
    current = resolve_band(schedule, after)
    day = after.astimezone(NZ_TZ).date()
    for offset in range(BAND_CHANGE_HORIZON_DAYS):
        edges = sorted(set(_candidate_edges(schedule, day + timedelta(days=offset))))
        for edge in edges:
            if edge > after and resolve_band(schedule, edge) != current:
                return edge
    return None


def all_bands(versions: Iterable[NetworkVersion]) -> list[str]:
    """Return every band any of the versions can resolve to, in first-seen order."""
    bands: list[str] = []
    for version in versions:
        bands.extend(b for b in version.bands.bands if b not in bands)
    return bands


def next_tariff_band_change(
    versions: Sequence[NetworkVersion], after: datetime
) -> datetime | None:
    """
    Return the next band change, including one caused by a later version.

    A new version can bring different windows or bands from its effective
    date, so the current schedule alone isn't enough.
    """
    network = version_at(versions, after)
    current = resolve_band(network.bands, after) if network else None
    start = after
    while True:
        day = start.astimezone(NZ_TZ).date()
        following = next((v for v in versions if v.effective_from > day), None)
        change = next_band_change(network.bands, start) if network else None
        if following is None:
            return change
        boundary = nz_midnight(following.effective_from)
        if change is not None and change < boundary:
            return change
        network = following
        if resolve_band(network.bands, boundary) != current:
            return boundary
        start = boundary


def trading_period_start(trading_period: int, at: datetime) -> datetime | None:
    """
    Return the start of trading period ``trading_period`` nearest to ``at``.

    ``at`` may be the period's start, its end, or a publish time just before
    or after it. Periods count from 1 at NZ midnight; DST days have 46 or 50
    of them. Returns None if no nearby day has that many periods.
    """
    day = at.astimezone(NZ_TZ).date()
    starts = []
    for offset in (-1, 0, 1):
        candidate = day + timedelta(days=offset)
        start = add_periods(nz_midnight(candidate), trading_period - 1)
        if start.astimezone(NZ_TZ).date() == candidate:
            starts.append(start)
    return min(starts, key=lambda s: abs(s - at), default=None)


def to_nzd_per_kwh(value: float, unit: str | None) -> float:
    """
    Convert a price in c/kWh, NZD/kWh or $/MWh to NZD/kWh.

    Raises ValueError for an unknown unit, or for NaN or infinity ("nan" and
    "inf" parse as floats but aren't prices).
    """
    if not math.isfinite(value):
        msg = f"Not a price: {value!r}"
        raise ValueError(msg)
    normalised = (unit or "").strip().lower().replace(" ", "")
    if normalised in ("c/kwh", "cents/kwh"):
        return value / 100
    if normalised in ("nzd/kwh", "$/kwh"):
        return value
    if normalised in ("nzd/mwh", "$/mwh"):
        return value / 1000
    msg = f"Unsupported price unit: {unit!r}"
    raise ValueError(msg)


class Rates(NamedTuple):
    """Every rate a price needs, as in force at one time."""

    network: NetworkVersion
    loss: LossVersion
    retailer: RetailerVersion
    band: str

    def daily_charge(self, gst: float) -> float:
        """Return the fixed daily charge in NZD from these rates."""
        return (
            self.network.daily
            + self.retailer.metering_daily
            + self.retailer.retailer_daily
        ) * gst


def _components(tariff: Tariff, at: datetime) -> Rates:
    network = version_at(tariff.network, at)
    loss = version_at(tariff.loss, at)
    retailer = version_at(tariff.retailer, at)
    if network is None or loss is None or retailer is None:
        raise NoRatesError(at, _missing(network=network, loss=loss, retailer=retailer))
    return Rates(
        network=network,
        loss=loss,
        retailer=retailer,
        band=resolve_band(network.bands, at),
    )


def rates_at(tariff: Tariff, at: datetime) -> Rates | None:
    """Return the rates in force at ``at``, or None if any is missing."""
    try:
        return _components(tariff, at)
    except NoRatesError:
        return None


def prices(
    tariff: Tariff, spot: float, at: datetime
) -> tuple[PriceBreakdown, PriceBreakdown]:
    """Return the import and export prices, looking up the rates once."""
    parts = _components(tariff, at)
    return _import(tariff, spot, parts), _export(tariff, spot, parts)


def import_price(tariff: Tariff, spot: float, at: datetime) -> PriceBreakdown:
    """
    Return the landed import price per kWh for the period starting ``at``.

    P_imp = ((S + m_imp) * L + N_imp + T + V) * G. Losses apply to energy and
    margin only. Raises NoRatesError when no network, loss or retailer
    version covers ``at``: a missing loss factor is not taken as 1.
    """
    return _import(tariff, spot, _components(tariff, at))


def export_price(tariff: Tariff, spot: float, at: datetime) -> PriceBreakdown:
    """
    Return the landed export price per kWh for the period starting ``at``.

    P_exp = max(floor, S - m_exp + C_exp) * G. Export has no losses, but
    like import it raises NoRatesError when any rate data is missing, so the
    two prices are always available together.
    """
    return _export(tariff, spot, _components(tariff, at))


def _import(tariff: Tariff, spot: float, parts: Rates) -> PriceBreakdown:
    network, loss, retailer, band = parts
    margin = retailer.import_margin
    rate = network.import_rates.get(band, 0.0)
    ex_gst = (
        (spot + margin) * loss.factor + rate + retailer.transmission + retailer.levy
    )
    return PriceBreakdown(
        price=ex_gst * tariff.gst,
        spot=spot,
        margin=margin,
        loss_factor=loss.factor,
        network=rate,
        transmission=retailer.transmission,
        levy=retailer.levy,
        export_credit=0.0,
        gst=tariff.gst,
        tou_band=band,
        network_version=network.effective_from,
        retailer_version=retailer.effective_from,
    )


def _export(tariff: Tariff, spot: float, parts: Rates) -> PriceBreakdown:
    network, _loss, retailer, band = parts
    margin = retailer.export_margin
    credit = network.export_credit.get(band, 0.0)
    ex_gst = spot - margin + credit
    if retailer.export_floor is not None:
        ex_gst = max(ex_gst, retailer.export_floor)
    return PriceBreakdown(
        price=ex_gst * tariff.gst,
        spot=spot,
        margin=margin,
        loss_factor=1.0,
        network=0.0,
        transmission=0.0,
        levy=0.0,
        export_credit=credit,
        gst=tariff.gst,
        tou_band=band,
        network_version=network.effective_from,
        retailer_version=retailer.effective_from,
    )


def daily_charge(tariff: Tariff, at: datetime) -> float:
    """
    Return the fixed daily charge in NZD, with GST as configured.

    Raises NoRatesError unless every rate is in force at ``at``, the same
    rule as the prices and the daily charge sensor.
    """
    return _components(tariff, at).daily_charge(tariff.gst)
