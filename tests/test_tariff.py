"""Test the pure price model."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.electricity_billing_nz.networks import load_networks
from custom_components.electricity_billing_nz.tariff import (
    NZ_TZ,
    BandRule,
    BandSchedule,
    NoRatesError,
    RetailerVersion,
    Tariff,
    add_periods,
    all_bands,
    daily_charge,
    export_price,
    import_price,
    next_band_change,
    next_tariff_band_change,
    period_start,
    prices,
    rates_at,
    resolve_band,
    to_nzd_per_kwh,
    trading_period_start,
    version_on,
)

from .helpers import template_export, template_import

VECTOR = load_networks()["vector"]
ARHLC = VECTOR.categories["ARHLC"]
# Versions by date, so adding a later year doesn't move them.
ARHLC_2025 = version_on(ARHLC, date(2025, 4, 1))
ARHLC_2026 = version_on(ARHLC, date(2026, 4, 1))
BANDS = ARHLC_2026.bands
BANDS_2025 = ARHLC_2025.bands
PLAN = RetailerVersion(
    effective_from=date(2000, 1, 1),
    import_margin=0.0286,
    export_margin=0.019,
    transmission=0.0302,
    levy=0.002,
    metering_daily=0.35,
    retailer_daily=0.15,
)
TARIFF = Tariff(network=ARHLC, loss=VECTOR.loss_categories["VECA1"], retailer=(PLAN,))


def nz(text: str) -> datetime:
    """Return a Pacific/Auckland datetime from 'YYYY-MM-DD HH:MM'."""
    return datetime.fromisoformat(text).replace(tzinfo=NZ_TZ)


@pytest.mark.parametrize(
    ("at", "band"),
    [
        ("2026-07-15 07:00", "peak"),  # winter morning peak starts
        ("2026-07-15 06:30", "offpeak"),
        ("2026-07-15 10:30", "peak"),
        ("2026-07-15 11:00", "offpeak"),
        ("2026-07-15 16:30", "offpeak"),  # the 16:59 period is off-peak
        ("2026-07-15 17:00", "peak"),
        ("2026-07-15 21:30", "peak"),
        ("2026-07-15 22:00", "offpeak"),
        ("2026-05-04 08:00", "offpeak"),  # May has no morning peak
        ("2026-05-04 18:00", "peak"),
        ("2026-09-30 21:30", "peak"),
        ("2026-10-01 18:00", "offpeak"),  # Oct-Apr has no peak
        ("2027-01-01 18:00", "offpeak"),
        ("2026-06-01 18:00", "peak"),  # King's Birthday: peak includes holidays
        ("2026-07-11 18:00", "peak"),  # weekends too
    ],
)
def test_resolve_band(at: str, band: str) -> None:
    """Bands follow Vector's 2026 windows in NZ local time."""
    assert resolve_band(BANDS, nz(at)) == band


@pytest.mark.parametrize("spot", [-0.05, 0.0, 0.007, 0.12, 0.16, 0.5])
@pytest.mark.parametrize(
    "at",
    ["2026-07-15 17:00", "2026-07-15 12:00", "2026-11-16 18:00", "2026-05-04 07:30"],
)
def test_matches_templates(spot: float, at: str) -> None:
    """The model reproduces the margin-aware templates to 1e-9."""
    start = nz(at)
    peak = resolve_band(BANDS, start) == "peak"
    assert import_price(TARIFF, spot, start).price == pytest.approx(
        template_import(spot, peak=peak), abs=1e-9
    )
    assert export_price(TARIFF, spot, start).price == pytest.approx(
        template_export(spot, peak=peak), abs=1e-9
    )


def test_import_breakdown() -> None:
    """The breakdown carries every component, ex GST."""
    result = import_price(TARIFF, 0.12, nz("2026-07-15 17:00"))
    assert result.tou_band == "peak"
    assert result.margin == 0.0286
    assert result.loss_factor == 1.0476
    assert result.network == 0.1504
    assert result.transmission == 0.0302
    assert result.levy == 0.002
    assert result.gst == 1.15
    assert result.network_version == date(2026, 4, 1)
    assert result.retailer_version == date(2000, 1, 1)


def test_export_can_go_negative_without_floor() -> None:
    """Negative spot gives a negative export price by default."""
    assert export_price(TARIFF, -0.10, nz("2026-11-16 12:00")).price < 0


def test_export_floor() -> None:
    """A plan floor is applied ex GST, before GST."""
    floored = Tariff(
        network=TARIFF.network,
        loss=TARIFF.loss,
        retailer=(
            RetailerVersion(date(2000, 1, 1), export_margin=0.019, export_floor=0.0),
        ),
    )
    assert export_price(floored, -0.10, nz("2026-11-16 12:00")).price == 0.0


def test_ex_gst() -> None:
    """GST 1.0 gives ex-GST prices."""
    ex_gst = Tariff(network=TARIFF.network, loss=TARIFF.loss, retailer=(PLAN,), gst=1.0)
    start = nz("2026-07-15 17:00")
    assert import_price(ex_gst, 0.12, start).price * 1.15 == pytest.approx(
        import_price(TARIFF, 0.12, start).price
    )


def test_version_switch_at_nz_midnight() -> None:
    """A version dated 1 April applies from 00:00 NZ time, not UTC."""
    newer = RetailerVersion(date(2027, 4, 1), import_margin=0.05)
    tariff = Tariff(network=TARIFF.network, loss=TARIFF.loss, retailer=(PLAN, newer))
    assert import_price(tariff, 0.1, nz("2027-03-31 23:30")).margin == 0.0286
    assert import_price(tariff, 0.1, nz("2027-04-01 00:00")).margin == 0.05


def test_before_first_network_version() -> None:
    """Before the bundled data starts there's no price, not a too-low one."""
    at = nz("2025-01-01 12:00")
    with pytest.raises(NoRatesError):
        import_price(TARIFF, 0.1, at)
    with pytest.raises(NoRatesError):
        export_price(TARIFF, 0.1, at)
    with pytest.raises(NoRatesError):
        daily_charge(TARIFF, at)


def test_daily_charge() -> None:
    """Network, metering and retailer daily charges, with GST."""
    assert daily_charge(TARIFF, nz("2026-11-15 12:00")) == pytest.approx(
        (0.90 + 0.35 + 0.15) * 1.15
    )


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("2026-07-15 12:00", "2026-07-15 17:00"),
        ("2026-07-15 17:00", "2026-07-15 22:00"),
        ("2026-07-15 22:00", "2026-07-16 07:00"),
        ("2026-09-30 21:00", "2026-09-30 22:00"),
        ("2026-11-15 12:00", "2027-05-01 17:00"),  # no peak until May
    ],
)
def test_next_band_change(after: str, expected: str) -> None:
    """The next change is the next window edge where the band differs."""
    assert next_band_change(BANDS, nz(after)) == nz(expected)


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        ("2026-07-15 17:29", "2026-07-15 17:00"),
        ("2026-07-15 17:30", "2026-07-15 17:30"),
        ("2026-04-05 02:45", "2026-04-05 02:30"),  # DST ends this morning
        ("2026-09-27 03:15", "2026-09-27 03:00"),  # DST starts this morning
    ],
)
def test_period_start(at: str, expected: str) -> None:
    """Periods start on the half hour in NZ time, across DST changes."""
    assert period_start(nz(at)) == nz(expected)


@pytest.mark.parametrize(
    ("day", "count"), [(date(2026, 9, 27), 46), (date(2026, 4, 5), 50)]
)
def test_dst_day_period_count(day: date, count: int) -> None:
    """DST days have 46 and 50 trading periods, each with a band."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=NZ_TZ)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=NZ_TZ)
    periods = []
    at = start
    while at < end:
        periods.append(resolve_band(BANDS, at))
        at = (at.astimezone(UTC) + timedelta(minutes=30)).astimezone(NZ_TZ)
    assert len(periods) == count


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (12.0, "c/kWh", 0.12),
        (0.12, "NZD/kWh", 0.12),
        (120.0, "$/MWh", 0.12),
        (120.0, "NZD/MWh", 0.12),
    ],
)
def test_to_nzd_per_kwh(value: float, unit: str, expected: float) -> None:
    """Source units convert to NZD/kWh."""
    assert to_nzd_per_kwh(value, unit) == pytest.approx(expected)


def test_to_nzd_per_kwh_rejects_unknown_unit() -> None:
    """An unknown unit is an error, not a silent guess."""
    with pytest.raises(ValueError, match="Unsupported"):
        to_nzd_per_kwh(1.0, "kWh")


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # DST ends 2026-04-05 03:00 NZDT; 02:00-03:00 local happens twice.
        ("2026-04-04T13:10:00", "2026-04-04T13:00:00"),  # first 02:00 (NZDT)
        ("2026-04-04T14:10:00", "2026-04-04T14:00:00"),  # second 02:00 (NZST)
        ("2026-04-04T14:40:00", "2026-04-04T14:30:00"),
    ],
)
def test_period_start_repeated_hour(utc: str, expected: str) -> None:
    """Both passes of the repeated hour floor to their own period."""
    at = datetime.fromisoformat(utc).replace(tzinfo=UTC)
    # Compare in UTC: Python never equates an ambiguous local time across zones.
    assert period_start(at).astimezone(UTC) == datetime.fromisoformat(expected).replace(
        tzinfo=UTC
    )


def test_add_periods_through_repeated_hour() -> None:
    """Stepping a period is absolute time, even across DST ending."""
    at = datetime(2026, 4, 4, 13, 30, tzinfo=UTC)  # 02:30 NZDT, first pass
    steps = [add_periods(at, n).astimezone(UTC).hour * 60 for n in range(1, 4)]
    assert steps == [14 * 60, 14 * 60, 15 * 60]
    assert add_periods(at, 2) - add_periods(at, 1) == timedelta(minutes=30)


SHOULDER = BandSchedule(
    name="shoulder",
    rules=(
        BandRule(
            band="shoulder", months=frozenset(range(1, 13)), windows=((7 * 60, 9 * 60),)
        ),
    ),
    default="offpeak",
)
NEXT_YEAR = replace(
    ARHLC_2026,
    effective_from=date(2027, 4, 1),
    import_rates={"offpeak": 0.05, "shoulder": 0.08},
    export_credit={},
    bands=SHOULDER,
)


def test_all_bands_across_versions() -> None:
    """Bands from every version are listed once."""
    assert all_bands((*ARHLC, NEXT_YEAR)) == ["offpeak", "peak", "shoulder"]


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        # The current schedule has no change before May; the new version's
        # 07:00 shoulder comes first.
        ("2026-11-15 12:00", "2027-04-01 07:00"),
        # Peak is in force when the version starts: the band changes then.
        ("2026-07-15 17:00", "2026-07-15 22:00"),
    ],
)
def test_next_band_change_sees_later_version(after: str, expected: str) -> None:
    """A later version's schedule is used from its effective date."""
    versions = (*ARHLC, NEXT_YEAR)
    assert next_tariff_band_change(versions, nz(after)) == nz(expected)


def test_next_band_change_at_version_start() -> None:
    """A version that starts in a different band is itself a change."""
    peak_all_day = BandSchedule(name="peak", rules=(), default="peak")
    versions = (*ARHLC, replace(NEXT_YEAR, bands=peak_all_day))
    assert next_tariff_band_change(versions, nz("2026-11-15 12:00")) == nz(
        "2027-04-01 00:00"
    )


def test_next_band_change_single_version() -> None:
    """With one version it matches the schedule's own next change."""
    assert next_tariff_band_change((ARHLC_2026,), nz("2026-07-15 12:00")) == nz(
        "2026-07-15 17:00"
    )


def test_next_band_change_across_2025_and_2026() -> None:
    """From Oct 2025 (no summer peak) the next change is in the 2026 schedule."""
    assert next_tariff_band_change(ARHLC, nz("2025-10-01 12:00")) == nz(
        "2026-05-01 17:00"
    )


@pytest.mark.parametrize(
    ("trading_period", "at", "expected"),
    [
        (34, "2026-07-15 16:30", "2026-07-15 16:30"),  # timestamp is the start
        (34, "2026-07-15 17:00", "2026-07-15 16:30"),  # ...or the end
        (34, "2026-07-15 17:10", "2026-07-15 16:30"),  # ...or publish time
        (48, "2026-07-16 00:00", "2026-07-15 23:30"),  # end is next day
        (35, "2026-07-15 17:10", "2026-07-15 17:00"),
        (35, "2026-07-15 16:59:50", "2026-07-15 17:00"),  # published just before
        (1, "2026-07-15 23:59", "2026-07-16 00:00"),  # tomorrow's first period
        (50, "2026-04-05 23:59", "2026-04-05 23:30"),  # 50 periods as DST ends
        (46, "2026-09-27 23:59", "2026-09-27 23:30"),  # 46 as DST starts
    ],
)
def test_trading_period_start(trading_period: int, at: str, expected: str) -> None:
    """Trading periods count from NZ midnight, in absolute time."""
    assert trading_period_start(trading_period, nz(at)) == nz(expected)


def test_trading_period_beyond_the_day() -> None:
    """Period 50 doesn't exist on a normal day."""
    assert trading_period_start(50, nz("2026-07-15 12:00")) is None


def test_prices_matches_separate_functions() -> None:
    """prices() looks the rates up once but gives the same answer."""
    at = nz("2026-07-15 17:00")
    assert prices(TARIFF, 0.12, at) == (
        import_price(TARIFF, 0.12, at),
        export_price(TARIFF, 0.12, at),
    )


@pytest.mark.parametrize(
    ("loss", "retailer", "missing"),
    [
        (False, True, ("loss",)),
        (True, False, ("retailer",)),
        (False, False, ("loss", "retailer")),
    ],
)
def test_missing_rates_are_named(
    *, loss: bool, retailer: bool, missing: tuple[str, ...]
) -> None:
    """Network data alone isn't enough: a missing loss factor isn't taken as 1."""
    later = date(2027, 1, 1)
    tariff = Tariff(
        network=TARIFF.network,
        loss=TARIFF.loss if loss else (replace(TARIFF.loss[0], effective_from=later),),
        retailer=(PLAN,) if retailer else (replace(PLAN, effective_from=later),),
    )
    at = nz("2026-07-15 17:00")
    assert rates_at(tariff, at) is None
    with pytest.raises(NoRatesError) as err:
        prices(tariff, 0.12, at)
    assert err.value.missing == missing
    assert rates_at(TARIFF, at) is not None


@pytest.mark.parametrize(
    ("at", "band"),
    [
        ("2025-07-15 07:00", "peak"),  # Tuesday, winter
        ("2025-07-15 11:00", "offpeak"),
        ("2025-07-15 17:00", "peak"),
        ("2025-07-15 21:00", "offpeak"),  # 2025 peak ends at 21:00, not 22:00
        ("2025-07-12 18:00", "offpeak"),  # Saturday: weekends are off-peak
        ("2025-06-02 18:00", "peak"),  # King's Birthday: holidays are peak
        ("2025-04-01 08:00", "peak"),  # April is winter in 2025
        # Vector's summer peak (Oct-Mar) costs the off-peak rate.
        ("2025-11-18 08:00", "offpeak"),
        ("2026-03-31 18:00", "offpeak"),
    ],
)
def test_resolve_band_2025(at: str, band: str) -> None:
    """Bands follow Vector's 2025 windows: weekdays only, Apr-Sep."""
    assert resolve_band(BANDS_2025, nz(at)) == band


@pytest.mark.parametrize(
    ("at", "network", "loss", "credit", "daily"),
    [
        ("2025-07-15 18:00", 0.1575, 1.0533, 0.0, 0.75),  # winter peak
        ("2025-11-18 18:00", 0.0444, 1.0533, 0.0, 0.75),  # summer peak = off-peak
        ("2026-03-31 23:30", 0.0444, 1.0533, 0.0, 0.75),  # last 2025 period
        ("2026-04-01 00:00", 0.0457, 1.0476, 0.0, 0.90),  # 2026 from NZ midnight
    ],
)
def test_vector_2025_prices(
    *, at: str, network: float, loss: float, credit: float, daily: float
) -> None:
    """2025 prices use that year's rates, loss factor and zero export credit."""
    imp, exp = prices(TARIFF, 0.12, nz(at))
    assert imp.network == network
    assert imp.loss_factor == loss
    assert exp.export_credit == credit
    assert imp.price == pytest.approx(template_import(0.12, network=network, loss=loss))
    assert exp.price == pytest.approx((0.12 - 0.019 + credit) * 1.15)
    assert daily_charge(TARIFF, nz(at)) == pytest.approx((daily + 0.35 + 0.15) * 1.15)
