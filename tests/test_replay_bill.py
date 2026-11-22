"""Test the bill replay in tools/replay_bill.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from custom_components.electricity_billing_nz.tariff import NZ_TZ
from tools.replay_bill import (
    Gap,
    cycle_bounds,
    load_tariff,
    main,
    query_rows,
    replay,
    settled_prices,
)

from .conftest import ECOTRICITY, entry_data
from .helpers import nz

if TYPE_CHECKING:
    from pathlib import Path

ENTRY = {"data": entry_data(), "options": {"retailer_versions": [ECOTRICITY]}}


def _epoch(text: str) -> int:
    return int(nz(text).timestamp())


def _every_period(month: str, spot: float) -> dict[datetime, float]:
    start, end = cycle_bounds(month, 1)
    prices = {}
    period = start.astimezone(UTC) - timedelta(minutes=30)
    while period < end:
        prices[period] = spot
        period += timedelta(minutes=30)
    return prices


def test_cycle_bounds_follow_the_cycle_day() -> None:
    """The cycle starting in a month, in NZ time, rolling the year."""
    assert cycle_bounds("2026-12", 1) == (
        datetime(2026, 12, 1, tzinfo=NZ_TZ),
        datetime(2027, 1, 1, tzinfo=NZ_TZ),
    )
    assert cycle_bounds("2026-10", 15) == (
        datetime(2026, 10, 15, tzinfo=NZ_TZ),
        datetime(2026, 11, 15, tzinfo=NZ_TZ),
    )


def test_settled_prices_fill_from_history() -> None:
    """The settled series wins; its history fills only missing periods."""
    rows = [[0, "2026-10-01T00:00:00+13:00", 1, 5.0], [0, None, None, None]]
    history = [
        [
            0,
            (
                "[{'period_start': '2026-10-01T00:00:00+13:00', 'price': 9.0},"
                " {'period_start': '2026-10-01T00:30:00+13:00', 'price': 4.0}]"
            ),
        ]
    ]
    assert settled_prices(rows, history) == {
        nz("2026-10-01 00:00").astimezone(UTC): pytest.approx(0.05),
        nz("2026-10-01 00:30").astimezone(UTC): pytest.approx(0.04),
    }


def test_steady_power_is_billed_in_its_periods() -> None:
    """2 kW for an hour is 2 kWh, priced at the spot it was used at."""
    power = [[_epoch("2026-10-15 10:00"), 2000.0], [_epoch("2026-10-15 11:00"), 0.0]]
    result = replay(
        load_tariff(ENTRY, None), power, _every_period("2026-10", 0.05), "2026-10"
    )
    bill = result.bill
    assert not result.unsettled
    assert bill.import_kwh == pytest.approx(2.0)
    assert bill.export_kwh == 0
    # Metering began on the 15th: daily charges from then, as a partial cycle.
    assert bill.days == 17
    assert bill.complete is False


def test_signed_power_meters_export() -> None:
    """Negative power is export."""
    power = [[_epoch("2026-10-15 12:00"), -3000.0], [_epoch("2026-10-15 12:20"), 0]]
    bill = replay(
        load_tariff(ENTRY, None), power, _every_period("2026-10", 0.05), "2026-10"
    ).bill
    assert bill.export_kwh == pytest.approx(1.0)


def test_gap_stops_the_held_power_and_spreads_known_energy() -> None:
    """A recording gap isn't the last value held; its known energy is spread."""
    power = [[_epoch("2026-10-15 10:00"), 2000.0], [_epoch("2026-10-15 16:00"), 0]]
    gap = Gap(nz("2026-10-15 11:00"), nz("2026-10-15 16:00"), import_kwh=1.5)
    result = replay(
        load_tariff(ENTRY, None),
        power,
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=[gap],
    )
    # 2 kW for the hour before the gap, then the 1.5 kWh known for the gap.
    assert result.bill.import_kwh == pytest.approx(3.5)


def test_null_power_is_unavailable() -> None:
    """A null value stops the held power until the next reading."""
    power = [
        [_epoch("2026-10-15 10:00"), 2000.0],
        [_epoch("2026-10-15 10:30"), None],
        [_epoch("2026-10-15 13:00"), 0.0],
    ]
    result = replay(
        load_tariff(ENTRY, None), power, _every_period("2026-10", 0.05), "2026-10"
    )
    assert result.bill.import_kwh == pytest.approx(1.0)


def test_unsettled_periods_with_energy_take_the_latest_spot() -> None:
    """Periods without a settled spot are listed and priced at the latest one."""
    power = [[_epoch("2026-10-15 10:00"), 2000.0], [_epoch("2026-10-15 11:00"), 0]]
    prices = _every_period("2026-10", 0.05)
    del prices[nz("2026-10-15 10:30").astimezone(UTC)]
    result = replay(load_tariff(ENTRY, None), power, prices, "2026-10")
    assert result.unsettled == [nz("2026-10-15 10:30").astimezone(UTC)]
    assert result.bill.provisional_periods == 1
    assert result.bill.unpriced_periods == 0


def test_import_margin_override() -> None:
    """Every retailer version takes the given import margin."""
    tariff = load_tariff(ENTRY, 0.028)
    assert {v.import_margin for v in tariff.retailer} == {0.028}


def test_main_prints_the_bill(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The command line reads InfluxDB responses and prints the lines."""

    def response(columns: list[str], rows: list[list]) -> str:
        series = [{"name": "x", "columns": columns, "values": rows}]
        return json.dumps({"results": [{"series": series}]})

    (tmp_path / "entry.json").write_text(json.dumps(ENTRY))
    (tmp_path / "power.json").write_text(
        response(
            ["time", "value"],
            [[_epoch("2026-10-15 10:00"), 1000.0], [_epoch("2026-10-15 11:00"), 0]],
        )
    )
    (tmp_path / "settled.json").write_text(
        response(
            ["time", "timestamp_str", "trading_period", "value"],
            [[0, "2026-10-15T10:00:00+13:00", 21, 5.0]],
        )
    )

    assert main(["--month", "2026-10", "--data", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out[: out.rindex("}") + 1])["days"] == 17
    assert "import 1.00 kWh" in out
    assert "had no settled price" in err


def test_influxdb_error_is_reported(tmp_path: Path) -> None:
    """An error response isn't read as no data."""
    (tmp_path / "settled.json").write_text(
        json.dumps({"results": [{"statement_id": 0, "error": "bad query"}]})
    )
    with pytest.raises(ValueError, match="bad query"):
        query_rows(tmp_path / "settled.json")


def test_gap_after_the_last_reading_stops_the_held_power() -> None:
    """A gap running past the last reading isn't the last value held."""
    power = [[_epoch("2026-10-15 10:00"), 2000.0]]
    gap = Gap(nz("2026-10-15 11:00"), nz("2026-11-01 00:00"), import_kwh=1.5)
    result = replay(
        load_tariff(ENTRY, None),
        power,
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=[gap],
    )
    assert result.bill.import_kwh == pytest.approx(3.5)


def test_readings_inside_a_gap_are_ignored() -> None:
    """Only the gap's known energy counts for it, not stray readings."""
    power = [
        [_epoch("2026-10-15 10:00"), 0.0],
        [_epoch("2026-10-15 13:00"), 3000.0],
        [_epoch("2026-10-15 16:00"), 0.0],
    ]
    gap = Gap(nz("2026-10-15 11:00"), nz("2026-10-15 16:00"), import_kwh=1.5)
    result = replay(
        load_tariff(ENTRY, None),
        power,
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=[gap],
    )
    assert result.bill.import_kwh == pytest.approx(1.5)


def test_settled_trading_period_wins_and_naive_times_are_nz() -> None:
    """The settled state's trading period picks the period; the stamp the day."""
    rows = [[0, "2026-10-01T11:20:00", 21, 5.0]]  # published 11:20 for TP 21
    assert settled_prices(rows) == {
        nz("2026-10-01 10:00").astimezone(UTC): pytest.approx(0.05)
    }


def test_unsettled_period_takes_the_spot_before_it() -> None:
    """Not the end of the month's spot, but the one current at the time."""
    power = [[_epoch("2026-10-03 10:00"), 2000.0], [_epoch("2026-10-03 10:30"), 0]]
    prices = _every_period("2026-10", 0.05)
    del prices[nz("2026-10-03 10:00").astimezone(UTC)]
    prices[nz("2026-10-31 23:30").astimezone(UTC)] = 0.30
    bill = replay(load_tariff(ENTRY, None), power, prices, "2026-10").bill
    assert bill.import_energy_rate == pytest.approx(0.05 + ECOTRICITY["import_margin"])


def test_import_margin_needs_retailer_versions() -> None:
    """Overriding a margin the entry doesn't have is an error, not a no-op."""
    with pytest.raises(ValueError, match="retailer versions"):
        load_tariff({"data": entry_data(), "options": {}}, 0.028)
