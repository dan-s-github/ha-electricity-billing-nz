"""Test the bill replay in tools/replay_bill.py."""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from typing import TYPE_CHECKING

import pytest

from custom_components.electricity_billing_nz.tariff import NZ_TZ
from tools import replay_bill
from tools.replay_bill import (
    Gap,
    cycle_bounds,
    emi_months,
    emi_prices,
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


def test_first_unsettled_period_takes_the_spot_before_the_cycle() -> None:
    """A settled spot from before the cycle prices its first unsettled periods."""
    power = [[_epoch("2026-10-01 00:00"), 2000.0], [_epoch("2026-10-01 00:30"), 0]]
    prices = _every_period("2026-10", 0.05)
    del prices[nz("2026-09-30 23:30").astimezone(UTC)]
    del prices[nz("2026-10-01 00:00").astimezone(UTC)]
    prices[nz("2026-09-30 22:00").astimezone(UTC)] = 0.07
    bill = replay(load_tariff(ENTRY, None), power, prices, "2026-10").bill
    assert bill.unpriced_periods == 0
    assert bill.import_energy_rate == pytest.approx(0.07 + ECOTRICITY["import_margin"])


def test_unsettled_energy_without_any_earlier_spot_is_an_error() -> None:
    """Energy that no settled spot can price fails rather than being dropped."""
    power = [[_epoch("2026-10-01 00:00"), 2000.0], [_epoch("2026-10-01 00:30"), 0]]
    prices = _every_period("2026-10", 0.05)
    del prices[nz("2026-09-30 23:30").astimezone(UTC)]
    del prices[nz("2026-10-01 00:00").astimezone(UTC)]
    with pytest.raises(ValueError, match="No settled price"):
        replay(load_tariff(ENTRY, None), power, prices, "2026-10")


def _replay_gaps(gaps: list[Gap]) -> replay_bill.Replay:
    return replay(
        load_tariff(ENTRY, None),
        [[_epoch("2026-10-15 10:00"), 0.0]],
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=gaps,
    )


def test_overlapping_gaps_with_energy_are_rejected() -> None:
    """Overlapping gaps would count their known energy twice."""
    gaps = [
        Gap(nz("2026-10-15 11:00"), nz("2026-10-15 20:00"), import_kwh=1.5),
        Gap(nz("2026-10-15 12:00"), nz("2026-10-15 13:00")),  # no energy
        Gap(nz("2026-10-15 15:00"), nz("2026-10-15 18:00"), import_kwh=1.0),
    ]
    with pytest.raises(ValueError, match="overlap"):
        _replay_gaps(gaps)


def test_overlapping_gaps_without_energy_are_allowed() -> None:
    """Gaps only marking the sensor unavailable can overlap, as can other months'."""
    gaps = [
        Gap(nz("2026-10-15 11:00"), nz("2026-10-15 16:00"), import_kwh=1.5),
        Gap(nz("2026-10-15 15:00"), nz("2026-10-15 18:00")),
        Gap(nz("2026-12-01 00:00"), nz("2026-12-03 00:00"), import_kwh=1.0),
        Gap(nz("2026-12-02 00:00"), nz("2026-12-04 00:00"), import_kwh=1.0),
    ]
    assert _replay_gaps(gaps).bill.import_kwh == pytest.approx(1.5)


def test_import_margin_needs_retailer_versions() -> None:
    """Overriding a margin the entry doesn't have is an error, not a no-op."""
    with pytest.raises(ValueError, match="retailer versions"):
        load_tariff({"data": entry_data(), "options": {}}, 0.028)


def test_only_the_cycle_is_replayed_from_a_long_export() -> None:
    """Months of earlier readings don't push the cycle out of the meter."""
    power = [
        [_epoch(f"2026-{month:02d}-15 10:00"), 1000.0] for month in range(1, 10)
    ] + [[_epoch("2026-10-15 10:00"), 0.0]]
    result = replay(
        load_tariff(ENTRY, None), power, _every_period("2026-08", 0.05), "2026-08"
    )
    # 1 kW held all August, from the reading on 15 July.
    assert result.bill.import_kwh == pytest.approx(31 * 24)
    assert result.bill.complete


def test_gaps_outside_the_cycle_are_ignored() -> None:
    """A gap in another month neither stops nor fills this cycle."""
    power = [[_epoch("2026-10-15 10:00"), 1000.0], [_epoch("2026-10-15 11:00"), 0]]
    elsewhere = Gap(nz("2026-12-01 00:00"), nz("2026-12-02 00:00"), import_kwh=50)
    result = replay(
        load_tariff(ENTRY, None),
        power,
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=[elsewhere],
    )
    assert result.bill.import_kwh == pytest.approx(1.0)


def test_missing_emi_month_is_joined_from_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A month EMI hasn't published as one file is built from its days."""
    calls = []

    def download(url: str) -> bytes:
        calls.append(url)
        if "ByMonth" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        day = url.rsplit("/", 1)[1][:8]
        return (
            "TradingDate,TradingPeriod,PointOfConnection,DollarsPerMegawattHour\n"
            f"{day[:4]}-{day[4:6]}-{day[6:]},1,ROS0221,50\n"
        ).encode()

    monkeypatch.setattr(replay_bill, "_download", download)
    text = replay_bill.fetch_emi("202602", tmp_path).decode()
    assert text.count("TradingDate") == 1
    assert text.count("ROS0221") == 28
    # Cached: no second download.
    replay_bill.fetch_emi("202602", tmp_path)
    assert len(calls) == 1 + 28


def test_partial_emi_month_isnt_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the days needed are fetched; an incomplete month isn't cached."""
    calls = []

    def download(url: str) -> bytes:
        calls.append(url)
        day = url.rsplit("/", 1)[1][:8]
        if "ByMonth" in url or day >= "20261120":
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        return f"h\n{day},1,ROS0221,50\n".encode()

    monkeypatch.setattr(replay_bill, "_download", download)
    text = replay_bill.fetch_emi("202611", tmp_path, until=date(2026, 11, 22))
    assert text.decode().count("ROS0221") == 19  # 1 to 19 November
    assert len(calls) == 1 + 21  # the month, then 1 to 21 November
    assert not list(tmp_path.iterdir())


def test_emi_prices_on_a_daylight_saving_day() -> None:
    """5 April 2026 has 50 periods; rows that don't fit are skipped."""
    rows = [
        {
            "TradingDate": "2026-04-05",
            "TradingPeriod": "50",
            "PointOfConnection": "ROS0221",
            "DollarsPerMegawattHour": "100",
        },
        {
            "TradingDate": "2026-04-05",
            "TradingPeriod": "51",
            "PointOfConnection": "ROS0221",
            "DollarsPerMegawattHour": "100",
        },
        {
            "TradingDate": "2026-04-05",
            "TradingPeriod": "1",
            "PointOfConnection": "ROS0221",
            "DollarsPerMegawattHour": "",
        },
        {
            "TradingDate": "2026-04-05",
            "TradingPeriod": "1",
            "PointOfConnection": "OTA2201",
            "DollarsPerMegawattHour": "9",
        },
    ]
    assert emi_prices(rows, "ROS0221") == {
        nz("2026-04-05 23:30").astimezone(UTC): pytest.approx(0.1)
    }


def test_emi_months_cover_the_cycle_only() -> None:
    """No month before the cycle's start is downloaded."""
    assert emi_months(*cycle_bounds("2026-10", 1)) == ["202610"]
    assert emi_months(*cycle_bounds("2026-10", 15)) == ["202610", "202611"]


def test_main_prices_with_emi(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """--emi reads the node's final prices from the cached month."""
    (tmp_path / "entry.json").write_text(json.dumps(ENTRY))
    (tmp_path / "power.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "series": [
                            {
                                "values": [
                                    [_epoch("2026-10-15 10:00"), 1000.0],
                                    [_epoch("2026-10-15 11:00"), 0],
                                ]
                            }
                        ]
                    }
                ]
            }
        )
    )
    (tmp_path / "emi").mkdir()
    (tmp_path / "emi" / "202610_FinalEnergyPrices.csv").write_text(
        "TradingDate,TradingPeriod,PointOfConnection,DollarsPerMegawattHour\n"
        + "".join(f"2026-10-15,{tp},ROS0221,50\n" for tp in range(1, 49))
    )
    assert (
        main(["--month", "2026-10", "--data", str(tmp_path), "--emi", "ROS0221"]) == 0
    )
    assert "import 1.00 kWh" in capsys.readouterr().out
    with pytest.raises(ValueError, match="No EMI final prices"):
        main(["--month", "2026-10", "--data", str(tmp_path), "--emi", "ROS221"])


def test_reading_in_a_gap_before_the_cycle_isnt_the_opening_power() -> None:
    """A gap ending before the cycle still hides the readings inside it."""
    power = [
        [_epoch("2026-09-30 19:00"), 0.0],
        [_epoch("2026-09-30 21:00"), 2000.0],
        [_epoch("2026-10-01 06:00"), 0.0],
    ]
    gap = Gap(nz("2026-09-30 20:00"), nz("2026-09-30 23:00"))
    result = replay(
        load_tariff(ENTRY, None),
        power,
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=[gap],
    )
    assert result.bill.import_kwh == 0


def test_gap_between_the_last_reading_and_the_cycle_stops_the_opening() -> None:
    """Power from before a gap isn't carried past it into the cycle."""
    power = [[_epoch("2026-09-30 10:00"), 2000.0], [_epoch("2026-10-01 06:00"), 0.0]]
    gap = Gap(nz("2026-09-30 12:00"), nz("2026-09-30 20:00"))
    result = replay(
        load_tariff(ENTRY, None),
        power,
        _every_period("2026-10", 0.05),
        "2026-10",
        gaps=[gap],
    )
    assert result.bill.import_kwh == 0
