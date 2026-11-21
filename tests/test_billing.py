"""Test billing-cycle metering (billing.py, no Home Assistant)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.electricity_billing_nz.billing import (
    BillingMeter,
    Cycle,
    cycle_start,
    next_cycle_start,
    spread,
    to_kwh,
)
from custom_components.electricity_billing_nz.engine import build_tariff
from custom_components.electricity_billing_nz.networks import load_networks
from custom_components.electricity_billing_nz.tariff import (
    NZ_TZ,
    add_periods,
    nz_midnight,
    version_on,
)

from .conftest import ECOTRICITY, NETWORK_SNAPSHOT, entry_data
from .helpers import nz, template_export, template_import

TARIFF = build_tariff(
    load_networks(NETWORK_SNAPSHOT)["vector"],
    entry_data(),
    {"retailer_versions": [ECOTRICITY]},
)
# Vector 2026 ARHLC, ex GST.
OFFPEAK, PEAK, PEAK_CREDIT, LOSS = 0.0457, 0.1504, 0.0524, 1.0476


def utc(text: str) -> datetime:
    """Return the UTC instant of an NZ 'YYYY-MM-DD HH:MM'."""
    return nz(text).astimezone(UTC)


@pytest.mark.parametrize(
    ("day", "cycle_day", "expected"),
    [
        (date(2026, 11, 17), 1, date(2026, 11, 1)),
        (date(2026, 11, 14), 15, date(2026, 10, 15)),
        (date(2026, 11, 15), 15, date(2026, 11, 15)),
        (date(2026, 1, 10), 28, date(2025, 12, 28)),
        (date(2026, 3, 1), 28, date(2026, 2, 28)),
    ],
)
def test_cycle_start(day: date, cycle_day: int, expected: date) -> None:
    """A cycle starts on the cycle day, this month or last."""
    assert cycle_start(day, cycle_day) == expected


def test_next_cycle_start_rolls_the_year() -> None:
    """December's cycle ends in January."""
    assert next_cycle_start(date(2026, 12, 15)) == date(2027, 1, 15)
    assert next_cycle_start(date(2026, 2, 28)) == date(2026, 3, 28)


def test_spread_by_time() -> None:
    """Energy between readings is shared by the time in each period."""
    parts = list(spread(nz("2026-11-17 10:15"), nz("2026-11-17 11:15"), 1.0))
    assert parts == [
        (utc("2026-11-17 10:00"), pytest.approx(0.25)),
        (utc("2026-11-17 10:30"), pytest.approx(0.5)),
        (utc("2026-11-17 11:00"), pytest.approx(0.25)),
    ]


def test_spread_at_one_instant() -> None:
    """Energy reported without elapsed time goes to that instant's period."""
    at = nz("2026-11-17 10:40")
    assert list(spread(at, at, 0.3)) == [(utc("2026-11-17 10:30"), 0.3)]


def test_spread_over_the_repeated_hour() -> None:
    """When DST ends, each pass of the repeated hour is its own two periods."""
    since = datetime(2026, 4, 5, 1, 0, tzinfo=NZ_TZ)  # NZDT
    until = datetime(2026, 4, 5, 3, 0, tzinfo=NZ_TZ)  # NZST, three hours later
    parts = list(spread(since, until, 6.0))
    assert len(parts) == 6
    assert len({start for start, _ in parts}) == 6
    assert sum(kwh for _, kwh in parts) == pytest.approx(6.0)


@pytest.mark.parametrize(
    ("value", "unit", "kwh"),
    [(1500.0, "Wh", 1.5), (2.5, "kWh", 2.5), (0.002, "MWh", 2.0)],
)
def test_to_kwh(value: float, unit: str, kwh: float) -> None:
    """Readings in Wh, kWh and MWh are converted to kWh."""
    assert to_kwh(value, unit) == pytest.approx(kwh)


@pytest.mark.parametrize(("value", "unit"), [(1.0, "W"), (1.0, None), (1e400, "kWh")])
def test_to_kwh_rejects(value: float, unit: str | None) -> None:
    """Other units and non-finite readings are rejected."""
    with pytest.raises(ValueError, match=r"Unsupported|Not a reading"):
        to_kwh(value, unit)


def _meter(cycle_day: int = 1) -> BillingMeter:
    return BillingMeter(TARIFF, cycle_day)


def test_first_reading_is_the_baseline() -> None:
    """The first reading starts the meter without counting any energy."""
    meter = _meter()
    # It starts the cycle, so the bill changes.
    assert meter.add_reading("import", 120.0, nz("2026-11-17 10:00"))
    bill = meter.bill(nz("2026-11-17 10:05"))
    assert bill is not None
    assert bill.import_kwh == 0
    assert not bill.complete


def test_increase_is_metered_by_band() -> None:
    """Energy counts at once, in the band of the period it was used in."""
    meter = _meter()
    meter.add_reading("import", 100.0, nz("2026-07-15 16:30"))
    assert meter.add_reading("import", 102.0, nz("2026-07-15 17:30"))
    bill = meter.bill(nz("2026-07-15 17:30"))
    assert bill is not None
    assert bill.kwh == {
        "offpeak": [pytest.approx(1.0), 0.0],
        "peak": [pytest.approx(1.0), 0.0],
    }


def test_small_drop_is_ignored() -> None:
    """A dip of under 10% is noise: the higher baseline is kept."""
    meter = _meter()
    meter.add_reading("import", 5000.0, nz("2026-11-17 10:00"))
    assert not meter.add_reading("import", 4999.9, nz("2026-11-17 10:10"))
    assert not meter.add_reading("import", 5000.0, nz("2026-11-17 10:20"))
    meter.add_reading("import", 5000.5, nz("2026-11-17 10:30"))
    bill = meter.bill(nz("2026-11-17 10:30"))
    assert bill is not None
    assert bill.import_kwh == pytest.approx(0.5)


def test_long_gap_with_energy_is_a_slow_reading() -> None:
    """Energy reported after over an hour without a reading is flagged."""
    meter = _meter()
    meter.add_reading("export", 0.0, nz("2026-11-17 06:00"))
    meter.add_reading("export", 1.2, nz("2026-11-17 09:00"))
    assert meter.slow["export"].kwh == pytest.approx(1.2)
    assert meter.slow["export"].gap == timedelta(hours=3)


@pytest.mark.parametrize(
    ("kwh", "minutes"),
    [
        (0.1, 600),  # a fine meter's first step after an idle night
        (1.2, 60),  # an hourly meter: not over the limit
    ],
)
def test_short_gap_or_little_energy_is_not_slow(kwh: float, minutes: int) -> None:
    """Only a long gap with real energy in it is flagged."""
    meter = _meter()
    meter.add_reading("import", 10.0, nz("2026-11-17 00:00"))
    meter.add_reading(
        "import", 10.0 + kwh, nz("2026-11-17 00:00") + timedelta(minutes=minutes)
    )
    assert not meter.slow


def test_gap_over_a_restart_is_not_slow() -> None:
    """The first increase after loading covers the downtime: not flagged."""
    meter = _meter()
    meter.add_reading("import", 10.0, nz("2026-11-17 00:00"))
    loaded = BillingMeter(TARIFF, 1, meter.as_dict())
    loaded.add_reading("import", 12.0, nz("2026-11-17 05:00"))
    assert not loaded.slow
    loaded.add_reading("import", 14.0, nz("2026-11-17 08:00"))
    assert "import" in loaded.slow


def test_small_drop_after_a_restart_keeps_the_exemption() -> None:
    """A noisy first reading doesn't move the baseline, so it isn't used up."""
    meter = _meter()
    meter.add_reading("import", 5000.0, nz("2026-11-17 00:00"))
    loaded = BillingMeter(TARIFF, 1, meter.as_dict())
    assert not loaded.add_reading("import", 4999.9, nz("2026-11-17 06:00"))
    loaded.add_reading("import", 5003.0, nz("2026-11-17 06:05"))
    assert not loaded.slow


def test_exemption_ends_when_the_baseline_moves() -> None:
    """After an outage, the meter's own later gaps are flagged."""
    meter = _meter()
    meter.add_reading("import", 10.0, nz("2026-11-17 00:00"))
    meter.mark_unavailable("import")
    meter.add_reading("import", 10.0, nz("2026-11-17 00:05"))  # back, unchanged
    meter.add_reading("import", 13.0, nz("2026-11-17 06:00"))
    assert "import" in meter.slow


def test_reload_has_no_exemption() -> None:
    """Loaded on a reload, the first slow gap is the meter's own."""
    meter = _meter()
    meter.add_reading("import", 10.0, nz("2026-11-17 00:00"))
    loaded = BillingMeter(TARIFF, 1, meter.as_dict())
    loaded.forget_restart()
    loaded.add_reading("import", 13.0, nz("2026-11-17 03:00"))
    assert "import" in loaded.slow


def test_gap_while_unavailable_is_not_slow() -> None:
    """The increase after the meter was unavailable isn't flagged."""
    meter = _meter()
    meter.add_reading("import", 10.0, nz("2026-11-17 00:00"))
    meter.mark_unavailable("import")
    meter.add_reading("import", 12.0, nz("2026-11-17 03:00"))
    assert not meter.slow


def test_falling_reading_is_a_reset() -> None:
    """A meter that resets (e.g. a daily total) counts its new value."""
    meter = _meter()
    meter.add_reading("export", 8.0, nz("2026-11-17 23:50"))
    meter.add_reading("export", 0.4, nz("2026-11-18 00:10"))
    bill = meter.bill(nz("2026-11-18 00:10"))
    assert bill is not None
    assert bill.export_kwh == pytest.approx(0.4)


def test_settled_period_is_priced_like_the_price_sensors() -> None:
    """A settled period's cost is its kWh times the landed price then."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-07-15 17:00"))
    meter.add_reading("export", 0.0, nz("2026-07-15 17:00"))
    meter.add_reading("import", 2.0, nz("2026-07-15 17:30"))
    meter.add_reading("export", 1.0, nz("2026-07-15 17:30"))
    assert meter.settle(nz("2026-07-15 17:00"), 0.10)

    assert meter.import_cost == pytest.approx(2 * template_import(0.10, peak=True))
    assert meter.export_earnings == pytest.approx(template_export(0.10, peak=True))
    bill = meter.bill(nz("2026-07-15 17:35"))
    assert bill is not None
    assert bill.provisional_periods == 0
    peak = bill.charges["peak"]
    assert peak.import_kwh_lossy == pytest.approx(2 * LOSS)
    assert peak.energy == pytest.approx((0.10 + 0.0286) * LOSS * 2)
    assert peak.network == pytest.approx(PEAK * 2)
    assert peak.transmission == pytest.approx(0.0302 * 2)
    assert peak.levy == pytest.approx(0.002 * 2)
    assert peak.export_energy == pytest.approx(0.10 - 0.019)
    assert peak.export_credit == pytest.approx(PEAK_CREDIT)
    assert bill.import_energy_rate == pytest.approx(0.10 + 0.0286)
    assert bill.export_energy_rate == pytest.approx(0.10 - 0.019)


def test_settling_twice_counts_once() -> None:
    """The source's history repeats periods; each is priced once."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.add_reading("import", 1.0, nz("2026-11-17 10:30"))
    assert meter.settle(nz("2026-11-17 10:00"), 0.05)
    assert not meter.settle(nz("2026-11-17 10:00"), 0.05)
    assert meter.import_cost == pytest.approx(template_import(0.05, peak=False))


def _assert_reload_keeps_the_bill(meter: BillingMeter, now: datetime) -> None:
    """Assert the bill is the same after a save and load."""
    live = meter.bill(now)
    loaded = BillingMeter(TARIFF, meter.cycle_day, meter.as_dict()).bill(now)
    assert live is not None
    assert loaded is not None
    assert loaded.kwh == {band: pytest.approx(row) for band, row in live.kwh.items()}
    assert {b: asdict(c) for b, c in loaded.charges.items()} == {
        b: pytest.approx(asdict(c)) for b, c in live.charges.items()
    }
    assert loaded.provisional_periods == live.provisional_periods
    assert loaded.unsettled_periods == live.unsettled_periods
    assert loaded.unpriced_periods == live.unpriced_periods


def test_late_energy_joins_a_settled_period() -> None:
    """Energy spread back into a settled period adds to it at its price."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.add_reading("import", 1.0, nz("2026-11-17 10:15"))
    meter.settle(nz("2026-11-17 10:00"), 0.10)
    meter.set_live_spot(nz("2026-11-17 10:30"), 0.12)
    # Half of this lands in the 10:00 period, which is already final.
    meter.add_reading("import", 2.0, nz("2026-11-17 10:45"))
    now = nz("2026-11-17 10:45")
    bill = meter.bill(now)
    assert bill is not None
    assert bill.provisional_periods == 1  # only 10:30
    # Its settled price is already known; settling it again changes nothing.
    assert not meter.settle(nz("2026-11-17 10:00"), 0.10)
    assert meter.settle(nz("2026-11-17 10:30"), 0.10)

    bill = meter.bill(now)
    assert bill is not None
    assert bill.import_kwh == pytest.approx(2.0)
    assert bill.provisional_periods == 0
    assert meter.import_cost == pytest.approx(2 * template_import(0.10, peak=False))
    _assert_reload_keeps_the_bill(meter, now)


def test_late_energy_joins_an_unsettled_period_at_its_price() -> None:
    """A period made final unsettled keeps its provisional price for late energy."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.set_live_spot(nz("2026-11-17 10:00"), 0.20)
    meter.add_reading("import", 1.0, nz("2026-11-17 10:15"))
    meter.tick(nz("2026-11-19 10:30"))  # final, unsettled, at the live spot
    # A reading after a long gap spreads energy back into it: 15 of the
    # 2,900 minutes the second kWh was spread over fall in it.
    meter.add_reading("import", 2.0, nz("2026-11-19 10:35"))
    assert not meter.settle(nz("2026-11-17 10:00"), 0.05)

    now = nz("2026-11-19 11:00")
    bill = meter.bill(now)
    assert bill is not None
    assert bill.unsettled_periods == 1
    kwh = 1.0 + 15 / 2900
    assert meter.import_cost == pytest.approx(kwh * template_import(0.20, peak=False))
    _assert_reload_keeps_the_bill(meter, now)


def test_unsettled_period_counts_at_the_live_spot() -> None:
    """Until it settles, a period is provisional at its live spot."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.set_live_spot(nz("2026-11-17 10:00"), 0.20)
    meter.add_reading("import", 1.0, nz("2026-11-17 10:30"))

    bill = meter.bill(nz("2026-11-17 10:31"))
    assert bill is not None
    assert bill.provisional_periods == 1
    assert bill.import_energy_rate == pytest.approx(0.20 + 0.0286)
    assert meter.import_cost == 0  # lifetime totals count settled periods only

    meter.settle(nz("2026-11-17 10:00"), 0.05)
    bill = meter.bill(nz("2026-11-17 11:01"))
    assert bill is not None
    assert bill.provisional_periods == 0
    assert bill.import_energy_rate == pytest.approx(0.05 + 0.0286)


def test_period_without_a_settled_price_is_final_after_48_hours() -> None:
    """An overdue period keeps its provisional price and is counted as unsettled."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.set_live_spot(nz("2026-11-17 10:00"), 0.20)
    meter.add_reading("import", 1.0, nz("2026-11-17 10:30"))

    assert not meter.tick(nz("2026-11-19 09:00"))
    assert meter.tick(nz("2026-11-19 10:30"))
    bill = meter.bill(nz("2026-11-19 10:30"))
    assert bill is not None
    assert bill.unsettled_periods == 1
    assert bill.provisional_periods == 0
    assert meter.import_cost == pytest.approx(template_import(0.20, peak=False))


def test_without_any_spot_the_period_is_unpriced() -> None:
    """Energy with no spot at all is metered but can't be priced."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.add_reading("import", 1.0, nz("2026-11-17 10:30"))
    bill = meter.bill(nz("2026-11-17 10:31"))
    assert bill is not None
    assert bill.import_kwh == pytest.approx(1.0)
    assert bill.import_energy_rate is None

    meter.tick(nz("2026-11-19 11:00"))
    bill = meter.bill(nz("2026-11-19 11:00"))
    assert bill is not None
    assert bill.unpriced_periods == 1


def test_daily_charges_follow_the_versions_in_force() -> None:
    """Each day of the cycle is charged at the daily rates of that day."""
    meter = _meter(cycle_day=15)
    meter.add_reading("import", 0.0, nz("2026-03-14 23:00"))
    meter.tick(nz("2026-04-14 12:00"))
    bill = meter.bill(nz("2026-04-14 12:00"))
    assert bill is not None
    assert (bill.start, bill.end) == (date(2026, 3, 15), date(2026, 4, 15))
    assert bill.days == 31
    days = [date(2026, 3, 15) + timedelta(days=i) for i in range(31)]
    expected = sum(version_on(TARIFF.network, d).daily for d in days)
    assert bill.daily["network"] == pytest.approx(expected)
    # Vector's daily charge changed on 1 April, mid-cycle.
    assert version_on(TARIFF.network, days[0]).daily != (
        version_on(TARIFF.network, days[-1]).daily
    )
    assert bill.daily["metering"] == pytest.approx(31 * 0.35)
    assert bill.daily["retailer"] == pytest.approx(31 * 0.15)


def test_estimated_total() -> None:
    """The total is the daily charges and energy, less export, with GST."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-01 00:00"))
    meter.add_reading("import", 3.0, nz("2026-11-01 00:30"))
    meter.settle(nz("2026-11-01 00:00"), 0.10)
    bill = meter.bill(nz("2026-11-02 12:00"))
    assert bill is not None
    assert bill.complete  # read at the very start of the cycle
    energy = 3 * ((0.10 + 0.0286) * LOSS + OFFPEAK + 0.0302 + 0.002)
    daily = 2 * (0.90 + 0.35 + 0.15)
    assert bill.subtotal == pytest.approx(energy + daily)
    assert bill.total == pytest.approx((energy + daily) * 1.15)
    lines = bill.lines()
    assert lines["days"] == 2
    assert lines["total"] == round((energy + daily) * 1.15, 2)
    assert lines["network"]["offpeak"]["import_kwh"] == 3.0


def test_rollover_keeps_late_settlements_in_the_old_cycle() -> None:
    """A period from the old cycle that settles after the rollover stays there."""
    meter = _meter(cycle_day=15)
    meter.add_reading("import", 0.0, nz("2026-11-14 23:30"))
    meter.add_reading("import", 1.0, nz("2026-11-15 00:00"))
    assert meter.tick(nz("2026-11-15 00:00"))
    meter.settle(nz("2026-11-14 23:30"), 0.10)

    current = meter.bill(nz("2026-11-15 00:10"))
    previous = meter.bill(nz("2026-11-15 00:10"), previous=True)
    assert current is not None
    assert previous is not None
    assert current.start == date(2026, 11, 15)
    assert current.complete
    assert current.import_kwh == 0
    assert previous.start == date(2026, 10, 15)
    assert previous.import_kwh == pytest.approx(1.0)
    assert previous.charges["offpeak"].import_kwh == pytest.approx(1.0)


def test_only_two_cycles_are_kept() -> None:
    """Older cycles are dropped as new ones start."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-09-30 12:00"))
    for day in ("2026-10-01", "2026-11-01", "2026-12-01"):
        meter.tick(nz(f"{day} 00:00"))
    assert sorted(meter.cycles) == [date(2026, 11, 1), date(2026, 12, 1)]


def test_changed_meter_starts_from_a_baseline() -> None:
    """A reading from a different meter isn't compared with the new one."""
    meter = BillingMeter(TARIFF, 1, sources={"import": "meter_a"})
    meter.add_reading("import", 1200.0, nz("2026-11-17 10:00"))

    loaded = BillingMeter(TARIFF, 1, meter.as_dict(), sources={"import": "meter_b"})
    loaded.add_reading("import", 25000.0, nz("2026-11-17 10:30"))
    bill = loaded.bill(nz("2026-11-17 10:30"))
    assert bill is not None
    assert bill.import_kwh == 0


def test_no_cycle_before_the_first_reading() -> None:
    """Without a reading there's no cycle, so no bill."""
    meter = _meter()
    assert not meter.tick(nz("2026-11-17 10:00"))
    assert meter.bill(nz("2026-11-17 10:00")) is None


def test_partial_cycle_charges_days_from_the_first_reading() -> None:
    """Daily charges start when metering did, like the energy."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    bill = meter.bill(nz("2026-11-20 10:00"))
    assert bill is not None
    assert not bill.complete
    assert bill.days == 4


def test_storage_round_trip() -> None:
    """The meter's state survives a save and load."""
    meter = _meter(cycle_day=15)
    meter.add_reading("import", 0.0, nz("2026-11-14 22:00"))
    meter.add_reading("export", 5.0, nz("2026-11-17 10:00"))
    meter.add_reading("import", 2.0, nz("2026-11-17 10:30"))
    meter.set_live_spot(nz("2026-11-17 10:30"), 0.12)
    meter.settle(nz("2026-11-17 10:00"), 0.10)

    loaded = BillingMeter(TARIFF, 15, meter.as_dict())
    assert loaded.as_dict() == meter.as_dict()
    now = nz("2026-11-17 10:40")
    assert loaded.bill(now) == meter.bill(now)


def test_changed_cycle_day_moves_the_periods() -> None:
    """Periods move to the new cycle day's cycles, kWh and charges together."""
    meter = _meter(cycle_day=15)
    meter.add_reading("import", 0.0, nz("2026-11-14 22:00"))
    meter.add_reading("import", 1.0, nz("2026-11-14 22:30"))
    meter.settle(nz("2026-11-14 22:00"), 0.10)
    meter.add_reading("import", 1.0, nz("2026-11-17 10:00"))
    meter.set_live_spot(nz("2026-11-17 10:00"), 0.12)
    meter.add_reading("import", 3.0, nz("2026-11-17 10:30"))

    loaded = BillingMeter(TARIFF, 1, meter.as_dict())
    assert sorted(loaded.cycles) == [date(2026, 11, 1)]
    assert loaded.import_cost == meter.import_cost
    assert loaded.readings == meter.readings
    bill = loaded.bill(nz("2026-11-17 10:40"))
    assert bill is not None
    # Covered from when the old cycles were, not from the 1st.
    assert not bill.complete
    assert bill.days == 4
    assert bill.import_kwh == pytest.approx(3.0)
    assert bill.charges["offpeak"].import_kwh == pytest.approx(3.0)
    assert bill.provisional_periods == 1


def test_changed_cycle_day_drops_carried_totals() -> None:
    """Totals stored without periods can't move, so the new cycle starts over."""
    stored = _meter(cycle_day=15)
    stored.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    data = stored.as_dict()
    data["cycles"] = [_stored_1_1_cycle("2026-11-15", "2026-11-15T00:00:00+13:00")]

    loaded = BillingMeter(TARIFF, 1, data)
    assert loaded.cycles == {}
    loaded.add_reading("import", 1.0, nz("2026-11-17 10:30"))
    bill = loaded.bill(nz("2026-11-17 10:30"))
    assert bill is not None
    assert bill.start == date(2026, 11, 1)
    assert not bill.complete
    assert bill.import_kwh == pytest.approx(1.0)


def _stored_1_1_cycle(start: str, since: str, kwh: float = 0.0) -> dict:
    """Return a cycle as storage 1.1 kept it: totals, no periods."""
    return {
        "start": start,
        "since": since,
        "kwh": {"offpeak": [kwh, 0.0]},
        "charges": {"offpeak": {"import_kwh": kwh, "energy": 0.5}},
        "unsettled_periods": 0,
        "unpriced_periods": 0,
    }


def test_1_1_cycle_is_carried_over() -> None:
    """A 1.1 cycle keeps its totals; energy still pending isn't counted twice."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.add_reading("import", 1.0, nz("2026-11-17 10:30"))
    data = meter.as_dict()
    # 1.1 counted the pending 1 kWh in the cycle when it was read.
    data["cycles"] = [
        _stored_1_1_cycle("2026-11-01", "2026-11-01T00:00:00+13:00", kwh=3.0)
    ]

    loaded = BillingMeter(TARIFF, 1, data)
    bill = loaded.bill(nz("2026-11-17 10:40"))
    assert bill is not None
    assert bill.complete
    assert bill.import_kwh == pytest.approx(3.0)
    assert bill.charges["offpeak"].energy == pytest.approx(0.5)
    assert (
        BillingMeter(TARIFF, 1, loaded.as_dict()).bill(nz("2026-11-17 10:40")) == bill
    )


def test_rate_change_dated_into_the_cycle_reprices_it() -> None:
    """A retailer version added later reprices the cycle's final periods."""
    meter = _meter()
    meter.add_reading("import", 0.0, nz("2026-11-17 10:00"))
    meter.add_reading("import", 2.0, nz("2026-11-17 10:30"))
    meter.settle(nz("2026-11-17 10:00"), 0.10)
    cost = meter.import_cost

    raised = {**ECOTRICITY, "effective_from": "2026-11-10", "import_margin": 0.03}
    tariff = build_tariff(
        load_networks(NETWORK_SNAPSHOT)["vector"],
        entry_data(),
        {"retailer_versions": [ECOTRICITY, raised]},
    )
    loaded = BillingMeter(tariff, 1, meter.as_dict())
    bill = loaded.bill(nz("2026-11-17 10:40"))
    assert bill is not None
    assert bill.charges["offpeak"].energy == pytest.approx((0.10 + 0.03) * LOSS * 2)
    # The lifetime totals are what each period cost when it settled.
    assert loaded.import_cost == cost


def test_catch_up_creates_the_skipped_cycle() -> None:
    """Energy read after HA was down for a whole cycle still fills that cycle."""
    meter = _meter()
    last = nz("2026-10-20 12:00")
    meter.add_reading("import", 0.0, last)
    now = nz("2026-12-01 00:05")
    meter.tick(now)  # December starts before the meter reports
    assert sorted(meter.cycles) == [date(2026, 10, 1), date(2026, 12, 1)]

    meter.add_reading("import", 100.0, now)
    assert sorted(meter.cycles) == [date(2026, 11, 1), date(2026, 12, 1)]
    previous = meter.bill(now, previous=True)
    assert previous is not None
    assert previous.start == date(2026, 11, 1)
    november = nz("2026-12-01 00:00") - nz("2026-11-01 00:00")
    assert previous.import_kwh == pytest.approx(100.0 * (november / (now - last)))


def test_unchanged_reading_moves_the_baseline() -> None:
    """After a reading that hasn't changed, energy is spread from it, not before."""
    meter = _meter()
    meter.add_reading("import", 5.0, nz("2026-11-17 10:00"))
    assert not meter.add_reading("import", 5.0, nz("2026-11-17 12:00"))
    meter.add_reading("import", 6.0, nz("2026-11-17 12:30"))
    assert list(meter.pending) == [utc("2026-11-17 12:00")]


def test_cycle_is_complete_only_once_every_meter_was_read() -> None:
    """A meter first read after the cycle began leaves the cycle incomplete."""
    sources = {"import": "meter_a", "export": "meter_b"}
    meter = BillingMeter(TARIFF, 1, sources=sources)
    meter.add_reading("export", 0.0, nz("2026-10-31 23:00"))
    meter.tick(nz("2026-11-01 00:00"))
    meter.add_reading("import", 0.0, nz("2026-11-01 12:00"))
    bill = meter.bill(nz("2026-11-01 12:05"))
    assert bill is not None
    assert meter.since == nz("2026-11-01 12:00")
    assert not bill.complete

    # A meter added later moves the coverage on too.
    sources["export"] = "meter_c"
    loaded = BillingMeter(TARIFF, 1, meter.as_dict(), sources=sources)
    assert loaded.since is None
    loaded.add_reading("export", 0.0, nz("2026-11-02 08:00"))
    assert loaded.cycles[date(2026, 11, 1)].since == nz("2026-11-02 08:00")


def test_cycle_complete_when_metered_from_its_start() -> None:
    """A cycle is complete once the meters were read before it began."""
    start = date(2026, 11, 1)
    assert Cycle(start, nz_midnight(start)).complete
    assert not Cycle(start, add_periods(nz_midnight(start), 1)).complete
